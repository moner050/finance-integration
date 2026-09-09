"""브로커 — 토스 주문 API 클라이언트와 dry-run 가짜 브로커.

읽기 전용 클라이언트(toss_client.py)와 일부러 분리했다. 주문을 낼 수 있는 코드는 이 파일에만 있고,
AUTOTRADE_MODE=live 일 때만 만들어진다.

토스 주문 스펙 (openapi.json 1.2.15 에서 확정, 2026-09-09):
  POST /api/v1/orders                 body {clientOrderId?, symbol, side BUY|SELL, orderType LIMIT|MARKET,
                                            timeInForce DAY, quantity, price(LIMIT), confirmHighValueOrder}
                                      → result {orderId, clientOrderId}. market 필드 없음(심볼로 판단).
  GET  /api/v1/orders/{orderId}       → result {status, execution{filledQuantity, averageFilledPrice, ...}}
  POST /api/v1/orders/{orderId}/cancel
  GET  /api/v1/buying-power?currency=KRW|USD  → result {cashBuyingPower}
  GET  /api/v1/sellable-quantity?symbol=      → result {sellableQuantity}
  헤더 X-Tossinvest-Account 필수. clientOrderId 는 멱등키(36자, 10분 유효).
  KR 지정가는 호가 단위를 맞춰야 한다. 틀리면 400 invalid-request + data.tickSize / nearestPrices.
"""

import logging
import time
from dataclasses import dataclass

import requests

from ..config import API_BASE
from ..toss_client import TossReadOnlyClient
from .models import TOSS_STATUS_MAP

log = logging.getLogger("scalper")

PATHS = {
    "create": "/api/v1/orders",
    "get": "/api/v1/orders/{order_id}",
    "cancel": "/api/v1/orders/{order_id}/cancel",
    "buying_power": "/api/v1/buying-power",
    "sellable": "/api/v1/sellable-quantity",
}


class BrokerError(Exception):
    """주문 API 오류. code 는 토스 에러 코드('insufficient-buying-power' 등), data 는 부가 정보."""

    def __init__(self, code: str, message: str = "", data: dict = None, status: int = None):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.data, self.status = code, message, data or {}, status


@dataclass
class OrderState:
    order_id: str
    status: str             # intent 상태 (open|filled|canceled|failed)
    filled_qty: float = 0.0
    avg_price: float = None
    raw_status: str = None  # 토스 status 원문


def kr_tick(price: float) -> int:
    """KRX 호가 단위 (주식 기준). ETF 는 5원이라 틀릴 수 있다 — 그 경우 서버가 알려주는 tickSize 로 재시도한다."""
    for limit, tick in ((2_000, 1), (5_000, 5), (20_000, 10), (50_000, 50), (200_000, 100), (500_000, 500)):
        if price < limit:
            return tick
    return 1_000


def round_price(price: float, market: str, side: str, tick: float = None) -> float:
    """호가 단위로 맞춘다. 매수는 내림(더 싸게), 매도는 올림 — 지정가가 불리해지지 않는 방향."""
    if market == "KR":
        tick = tick or kr_tick(price)
        units = price / tick
        rounded = (int(units) if side == "BUY" else -int(-units // 1)) * tick
        return float(int(rounded))
    tick = tick or (0.01 if price >= 1 else 0.0001)
    digits = 2 if tick >= 0.01 else 4
    units = price / tick
    rounded = (int(units) if side == "BUY" else -int(-units // 1)) * tick
    return round(rounded, digits)


def _fmt(value: float, market: str) -> str:
    if market == "KR":
        return str(int(round(value)))
    return f"{value:.4f}".rstrip("0").rstrip(".") if value < 1 else f"{value:.2f}"


class TossOrderClient:
    """실제 주문. 토큰·세션·계좌는 읽기 전용 클라이언트 것을 같이 쓴다."""

    name = "toss"

    def __init__(self, reader: TossReadOnlyClient, timeout: int = 10):
        self.reader = reader
        self.timeout = timeout

    def _headers(self) -> dict:
        self.reader._ensure_token()
        if not self.reader.account_seq:
            raise BrokerError("no-account", "계좌가 연결되지 않았다 (load_account 실패)")
        return {"Authorization": f"Bearer {self.reader._token}",
                "X-Tossinvest-Account": str(self.reader.account_seq)}

    def _call(self, method: str, path: str, params: dict = None, body: dict = None) -> dict:
        """한 번 호출. 429 는 Retry-After 만큼 기다려 한 번 더. 그 외 오류는 BrokerError 로 올린다.

        주문 생성은 멱등키가 있어 재시도가 안전하지만, 통신 오류 뒤 '나갔는지 모르는' 주문은
        여기서 다시 보내지 않는다 — reconcile 이 주문 조회로 확인한다.
        """
        for attempt in range(2):
            try:
                resp = self.reader.session.request(method, f"{API_BASE}{path}", headers=self._headers(),
                                                   params=params, json=body, timeout=self.timeout)
            except requests.RequestException as e:
                raise BrokerError("network", str(e))
            if resp.status_code == 429 and attempt == 0:
                wait = float(resp.headers.get("Retry-After", 1))
                log.warning("주문 API 레이트리밋 — %.1f초 대기", wait)
                time.sleep(wait)
                continue
            try:
                data = resp.json()
            except ValueError:
                data = {}
            if resp.status_code >= 400 or data.get("error"):
                err = data.get("error") or {}
                raise BrokerError(err.get("code") or f"http-{resp.status_code}", err.get("message") or resp.text[:200],
                                  err.get("data"), resp.status_code)
            return data.get("result", data)
        raise BrokerError("rate-limit", "레이트리밋 재시도 실패")

    # -- 사전 정보 --------------------------------------------------------------
    def buying_power(self, currency: str) -> float:
        r = self._call("GET", PATHS["buying_power"], params={"currency": currency})
        return float(r.get("cashBuyingPower") or 0)

    def sellable_quantity(self, symbol: str) -> float:
        r = self._call("GET", PATHS["sellable"], params={"symbol": symbol})
        return float(r.get("sellableQuantity") or 0)

    # -- 주문 -----------------------------------------------------------------
    def place(self, intent) -> str:
        """주문 생성. orderId 를 돌려준다. 호가 단위 오류면 서버가 준 가격으로 한 번 더 시도한다."""
        body = {"clientOrderId": intent.intent_id, "symbol": intent.symbol, "side": intent.side,
                "orderType": intent.order_type, "timeInForce": "DAY", "quantity": _fmt_qty(intent.quantity, intent.market)}
        if intent.order_type == "LIMIT":
            body["price"] = _fmt(intent.price, intent.market)
        try:
            r = self._call("POST", PATHS["create"], body=body)
        except BrokerError as e:
            near = (e.data or {}).get("nearestPrices") if e.code == "invalid-request" else None
            if not near or intent.order_type != "LIMIT":
                raise
            # 매수는 위쪽(체결 가능성), 매도 지정가는 아래쪽 근접가
            fixed = float(near[-1] if intent.side == "BUY" else near[0])
            log.warning("%s 호가 단위 보정 %s → %s (tick %s)", intent.symbol, body["price"], fixed, e.data.get("tickSize"))
            intent.price = fixed
            body["price"] = _fmt(fixed, intent.market)
            r = self._call("POST", PATHS["create"], body=body)
        return str(r["orderId"])

    def get(self, order_id: str) -> OrderState:
        r = self._call("GET", PATHS["get"].format(order_id=order_id))
        ex = r.get("execution") or {}
        raw = r.get("status")
        return OrderState(order_id, TOSS_STATUS_MAP.get(raw, "open"), float(ex.get("filledQuantity") or 0),
                          float(ex["averageFilledPrice"]) if ex.get("averageFilledPrice") else None, raw)

    def cancel(self, order_id: str) -> None:
        try:
            self._call("POST", PATHS["cancel"].format(order_id=order_id), body={})
        except BrokerError as e:
            if e.code not in ("already-filled", "already-canceled", "already-processing"):
                raise


def _fmt_qty(qty: float, market: str) -> str:
    return str(int(qty)) if market == "KR" or float(qty).is_integer() else f"{qty:.6f}".rstrip("0").rstrip(".")


class DryRunBroker:
    """가짜 브로커. API 를 부르지 않고 즉시 체결된 것으로 친다.

    지정가 매수는 지정가에, 시장가 매도는 참조가(신호 시점 현재가)에 체결. 매수가능금액은 무한대로 본다 —
    dry 의 목적은 '무엇을 얼마나 주문했을지' 를 보는 것이지 잔고 시뮬레이션이 아니다.
    """

    name = "dry"

    def __init__(self):
        self.orders = {}
        self._seq = 0

    def buying_power(self, currency: str) -> float:
        return float("inf")

    def sellable_quantity(self, symbol: str) -> float:
        return float("inf")

    def place(self, intent) -> str:
        self._seq += 1
        order_id = f"dry-{self._seq}"
        self.orders[order_id] = OrderState(order_id, "filled", intent.quantity, intent.price, "FILLED")
        return order_id

    def get(self, order_id: str) -> OrderState:
        return self.orders[order_id]

    def cancel(self, order_id: str) -> None:
        st = self.orders.get(order_id)
        if st and st.status == "open":
            st.status, st.raw_status = "canceled", "CANCELED"
