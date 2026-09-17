"""Binance USDⓈ-M 선물 서명 클라이언트 — live 자동매매(binance_trade.Trader)가 쓴다.

키는 계정별로 백오피스 '내 API 키' 에 넣고 DB 에 암호화해 둔다 (선물 거래 권한만, 출금 권한 없이). 헤지 모드 + 격리 마진 전제라
모든 주문에 positionSide(LONG/SHORT)를 보낸다. 손절 같은 조건부 주문은 2025-12-09 부터 알고 주문 API(/fapi/v1/algoOrder)로만
받는다 — /fapi/v1/order 에 STOP_MARKET 을 내면 -4120 으로 거부된다.
"""

import hashlib
import hmac
import logging
import math
import time
import urllib.parse
from decimal import Decimal

import requests

from .config import BINANCE_FAPI

log = logging.getLogger("binance")

POS_SIDE = {"long": "LONG", "short": "SHORT"}
OPEN_SIDE = {"long": "BUY", "short": "SELL"}
CLOSE_SIDE = {"long": "SELL", "short": "BUY"}
IGNORABLE = {"-4059", "-4046"}        # 이미 헤지 모드 / 이미 격리 — 바꿀 게 없다는 응답


class BrokerError(Exception):
    """Binance 오류. code 는 거래소 코드 문자열('-2019' 잔고 부족 등) 또는 내부 사유."""

    def __init__(self, code, message=""):
        super().__init__(f"{code}: {message}")
        self.code, self.message = str(code), message


def _digits(step: str) -> int:
    """'0.001' → 3, '1' → 0."""
    return max(0, -Decimal(step).normalize().as_tuple().exponent)


class BinanceFutures:
    def __init__(self, key: str, secret: str, session=None):
        self.key, self.secret = key, secret.encode()
        self.s = session or requests.Session()
        self.offset = 0                 # 서버 시각 - 로컬 시각 (ms)
        self.filters = {}               # symbol -> {"qty_d", "price_d", "min_qty", "min_notional"}

    def _req(self, method: str, path: str, signed: bool = True, **params):
        if signed:
            params["timestamp"] = int(time.time() * 1000) + self.offset
            params["recvWindow"] = 5000
            query = urllib.parse.urlencode(params)
            params["signature"] = hmac.new(self.secret, query.encode(), hashlib.sha256).hexdigest()
        r = self.s.request(method, BINANCE_FAPI + path, params=params, headers={"X-MBX-APIKEY": self.key}, timeout=10)
        try:
            j = r.json()
        except ValueError:
            j = {"code": r.status_code, "msg": r.text[:200]}
        if r.status_code != 200 or (isinstance(j, dict) and int(j.get("code", 0)) < 0):
            code, msg = (j.get("code", r.status_code), j.get("msg", "")) if isinstance(j, dict) else (r.status_code, "")
            raise BrokerError(code, msg)
        return j

    # -- 준비 -------------------------------------------------------------------
    def sync_time(self):
        self.offset = int(self._req("GET", "/fapi/v1/time", signed=False)["serverTime"]) - int(time.time() * 1000)

    def load_filters(self, symbols: list):
        info = self._req("GET", "/fapi/v1/exchangeInfo", signed=False)
        for s in info["symbols"]:
            if s["symbol"] in symbols:
                f = {x["filterType"]: x for x in s["filters"]}
                self.filters[s["symbol"]] = {"qty_d": _digits(f["LOT_SIZE"]["stepSize"]), "price_d": _digits(f["PRICE_FILTER"]["tickSize"]),
                                             "min_qty": float(f["LOT_SIZE"]["minQty"]), "min_notional": float(f["MIN_NOTIONAL"]["notional"])}
        missing = [s for s in symbols if s not in self.filters]
        if missing:
            raise BrokerError("unknown-symbol", ", ".join(missing))

    def setup(self, symbols: list, leverage: int):
        """헤지 모드 · 심볼별 격리 마진 · 배율. 포지션이 열려 있으면 모드 변경이 거부된다 — 그건 그대로 오류로 올린다."""
        for call in ([("POST", "/fapi/v1/positionSide/dual", {"dualSidePosition": "true"})]
                     + [("POST", "/fapi/v1/marginType", {"symbol": s, "marginType": "ISOLATED"}) for s in symbols]):
            try:
                self._req(call[0], call[1], **call[2])
            except BrokerError as e:
                if e.code not in IGNORABLE:
                    raise
        for s in symbols:
            got = int(self._req("POST", "/fapi/v1/leverage", symbol=s, leverage=leverage)["leverage"])
            if got != leverage:
                raise BrokerError("leverage", f"{s} 배율이 {got} 로 설정됐다 (원한 값 {leverage})")

    def balance(self) -> float:
        for row in self._req("GET", "/fapi/v2/balance"):
            if row["asset"] == "USDT":
                return float(row["availableBalance"])
        return 0.0

    # -- 수량·가격 --------------------------------------------------------------
    def round_qty(self, symbol: str, qty: float) -> float:
        d = self.filters[symbol]["qty_d"]
        return math.floor(qty * 10 ** d + 1e-9) / 10 ** d

    def round_price(self, symbol: str, price: float) -> float:
        return round(price, self.filters[symbol]["price_d"])

    def min_notional(self, symbol: str) -> float:
        return self.filters[symbol]["min_notional"]

    def _fmt(self, symbol: str, value: float, key: str) -> str:
        return f"{value:.{self.filters[symbol][key]}f}"

    # -- 주문 -------------------------------------------------------------------
    def _market(self, symbol: str, order_side: str, pos_side: str, qty: float) -> tuple:
        r = self._req("POST", "/fapi/v1/order", symbol=symbol, side=order_side, positionSide=pos_side, type="MARKET",
                      quantity=self._fmt(symbol, qty, "qty_d"), newOrderRespType="RESULT")
        filled = float(r.get("executedQty") or 0)
        if r.get("status") != "FILLED" or filled <= 0:
            raise BrokerError("not-filled", f"시장가 주문 {r.get('orderId')} 상태 {r.get('status')}")
        return str(r["orderId"]), float(r["avgPrice"]), filled

    def market_open(self, symbol: str, side: str, qty: float) -> tuple:
        """(주문번호, 평균 체결가, 체결 수량)."""
        return self._market(symbol, OPEN_SIDE[side], POS_SIDE[side], qty)

    def market_close(self, symbol: str, side: str, qty: float) -> tuple:
        return self._market(symbol, CLOSE_SIDE[side], POS_SIDE[side], qty)

    def place_stop(self, symbol: str, side: str, stop_price: float) -> str:
        """포지션 전체를 닫는 STOP_MARKET 알고 주문(마크 가격 트리거). 알고 주문 번호를 돌려준다."""
        r = self._req("POST", "/fapi/v1/algoOrder", algoType="CONDITIONAL", symbol=symbol, side=CLOSE_SIDE[side],
                      positionSide=POS_SIDE[side], type="STOP_MARKET", triggerPrice=self._fmt(symbol, stop_price, "price_d"),
                      closePosition="true", workingType="MARK_PRICE")
        return str(r["algoId"])

    def stop_status(self, algo_id: str) -> dict:
        """{"triggered", "price", "active"} — 트리거되면 actualOrderId 가 채워지고 actualPrice 가 평균 체결가다."""
        r = self._req("GET", "/fapi/v1/algoOrder", algoId=algo_id)
        return {"triggered": bool(r.get("actualOrderId")), "price": float(r.get("actualPrice") or 0),
                "active": r.get("algoStatus") == "NEW"}

    def cancel_stop(self, algo_id: str):
        self._req("DELETE", "/fapi/v1/algoOrder", algoId=algo_id)

    def position_qty(self, symbol: str, side: str) -> float:
        for row in self._req("GET", "/fapi/v2/positionRisk", symbol=symbol):
            if row.get("positionSide") == POS_SIDE[side]:
                return abs(float(row["positionAmt"]))
        return 0.0
