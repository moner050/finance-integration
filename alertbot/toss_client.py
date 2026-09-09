"""토스증권 Open API 클라이언트 (읽기 전용).

이 모듈은 절대 주문을 내지 않는다. 주문 API(POST /api/v1/orders)는 코드에 포함돼 있지 않다.
호출하는 API 는 전부 읽기 전용이다: 토큰, 캔들, 현재가, 계좌 목록, 보유 주식, 장 캘린더.
"""

import logging
import time
from datetime import datetime, timedelta, timezone

import requests

from .config import API_BASE, CANDLE_COUNT, MIN_CALL_GAP_SEC

log = logging.getLogger("scalper")


class TossReadOnlyClient:
    """읽기 전용. 주문 관련 메서드는 의도적으로 없다."""

    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self._token = None
        self._expires_at = datetime.min.replace(tzinfo=timezone.utc)
        self.account_seq = None
        self._last_call = 0.0
        self.session = requests.Session()

    def _ensure_token(self):
        now = datetime.now(timezone.utc)
        if self._token and now < self._expires_at - timedelta(minutes=5):
            return
        resp = self.session.post(
            f"{API_BASE}/oauth2/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials",
                  "client_id": self.client_id, "client_secret": self.client_secret},
            timeout=10)
        if resp.status_code == 403:
            raise SystemExit("403 — 허용 IP 미등록. WTS > 설정 > Open API > 허용 IP 관리.")
        resp.raise_for_status()
        data = resp.json()
        if "access_token" not in data:
            raise SystemExit(f"토큰 발급 실패: {data}\n.env 의 client_id/secret 을 확인할 것.")
        self._token = data["access_token"]
        self._expires_at = now + timedelta(seconds=int(data.get("expires_in", 3600)))
        log.info("토큰 발급 완료")

    def _get(self, path: str, params: dict = None, with_account: bool = False):
        """GET 전용. 실패 시 None (빈 결과 {} 와 구분해야 보유 조회 실패를 감지할 수 있다)."""
        gap = time.monotonic() - self._last_call
        if gap < MIN_CALL_GAP_SEC:
            time.sleep(MIN_CALL_GAP_SEC - gap)
        self._last_call = time.monotonic()

        for attempt in range(3):
            try:
                self._ensure_token()
                headers = {"Authorization": f"Bearer {self._token}"}
                if with_account:
                    headers["X-Tossinvest-Account"] = str(self.account_seq)
                resp = self.session.get(f"{API_BASE}{path}", headers=headers,
                                        params=params, timeout=10)
                if resp.status_code == 429 or (resp.status_code >= 400 and "rate-limit" in resp.text):
                    wait = float(resp.headers.get("Retry-After", 2 ** attempt))
                    log.warning("레이트리밋 — %.1f초 대기", wait)
                    time.sleep(wait)
                    continue
                if resp.status_code == 401:
                    self._token = None
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.HTTPError as e:
                body = e.response.text[:200] if e.response is not None else ""
                # 어느 종목에서 실패했는지 남긴다. 심볼 없이는 원인 추적이 불가능하다.
                log.error("GET %s %s 실패: %s", path, params or "", body)
                return None
            except requests.RequestException as e:
                log.error("GET %s 통신 오류: %s", path, e)
                time.sleep(2 ** attempt)
        return None

    @staticmethod
    def _unwrap(data):
        """토스 응답은 `result` 로 감싼다. 벗겨서 돌려준다."""
        if not isinstance(data, dict):
            return data
        for w in ("result", "data"):
            if data.get(w) is not None:
                return data[w]
        return data

    @classmethod
    def _items(cls, data, *keys) -> list:
        payload = cls._unwrap(data)
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for k in keys:
                if isinstance(payload.get(k), list):
                    return payload[k]
            lists = [v for v in payload.values() if isinstance(v, list)]
            if len(lists) == 1:
                return lists[0]
        return []

    # -- 계좌 (읽기 전용) -----------------------------------------------------
    def load_account(self) -> bool:
        data = self._get("/api/v1/accounts")
        for acc in self._items(data, "accounts"):
            if acc.get("accountType") == "BROKERAGE":
                self.account_seq = str(acc["accountSeq"])
                log.info("계좌 연결(조회 전용) 완료")
                return True
        log.warning("BROKERAGE 계좌를 찾지 못했다 — 손절 알림 비활성")
        return False

    def get_holdings(self):
        """symbol -> {qty, avg}. 조회 실패 시 None (빈 보유 {} 와 구분)."""
        data = self._get("/api/v1/holdings", with_account=True)
        if data is None:
            return None
        out = {}
        for it in self._items(data, "holdings", "items"):
            try:
                out[it["symbol"]] = {"qty": float(it["quantity"]),
                                     "avg": float(it["averagePurchasePrice"])}
            except (KeyError, TypeError, ValueError):
                continue
        return out

    # -- 시세 ---------------------------------------------------------------
    def get_candles(self, symbol: str, interval: str = "1m", count: int = CANDLE_COUNT) -> list:
        data = self._get("/api/v1/candles",
                         {"symbol": symbol, "interval": interval, "count": count})
        return self._sorted(self._items(data, "candles"))

    def get_candles_paged(self, symbol: str, pages: int) -> list:
        """nextBefore 로 과거를 거슬러 여러 페이지를 받는다 (프로파일 구축용)."""
        out, before = [], None
        for _ in range(pages):
            params = {"symbol": symbol, "interval": "1m", "count": 200}
            if before:
                params["before"] = before
            data = self._get("/api/v1/candles", params)
            batch = self._items(data, "candles")
            if not batch:
                break
            out.extend(batch)
            inner = self._unwrap(data)
            before = inner.get("nextBefore") if isinstance(inner, dict) else None
            if not before:
                break
        return self._sorted(out)

    @staticmethod
    def _sorted(candles: list) -> list:
        try:
            return sorted(candles, key=lambda c: c["timestamp"])
        except (KeyError, TypeError):
            return candles

    def get_prices(self, symbols: list):
        """symbol -> lastPrice. 조회 실패 시 None (빈 결과 {} 와 구분)."""
        data = self._get("/api/v1/prices", {"symbols": ",".join(symbols)})
        if data is None:
            return None
        out = {}
        for it in self._items(data, "prices"):
            try:
                out[it["symbol"]] = float(it["lastPrice"])
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def get_market_calendar(self, market: str):
        """장 운영 캘린더 원본(result 벗긴 것). 해석은 market_hours.parse_calendar 가 한다. 실패 시 None."""
        data = self._get(f"/api/v1/market-calendar/{market}")
        return self._unwrap(data) if data is not None else None
