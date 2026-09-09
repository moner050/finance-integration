"""엔진 시나리오 — 원본과 분리본이 같은 알림을 내는지 비교하는 데 쓴다.

가짜 클라이언트는 고정 캔들·현재가·보유를 돌려주고, 가짜 알림기는 쿨다운 없이
모든 send 호출을 기록한다. 시각은 2026-03-25 10:10 KST 로 고정한다.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from tests.conftest import TZ, bar

FIXED_NOW_UTC = datetime(2026, 3, 25, 1, 10, tzinfo=ZoneInfo("UTC"))   # 10:10 KST


def fixed_now_local(market: str) -> datetime:
    return FIXED_NOW_UTC.astimezone(TZ[market])


def scenario_candles() -> list:
    """09:00~10:01 KR 62봉. 60번째까지 조용하고, 60(거래량 1.5배)→61(3배, 강봉, 기준선 위) 돌파."""
    t0 = datetime(2026, 3, 25, 9, 0, tzinfo=TZ["KR"])
    out = []
    for i in range(60):
        close = 100.0 + (i % 3) * 0.05
        out.append(bar(t0 + timedelta(minutes=i), close, 1000, high=close + 0.1, low=close - 0.1))
    out.append(bar(t0 + timedelta(minutes=60), 100.3, 1500, high=100.4, low=100.1))
    out.append(bar(t0 + timedelta(minutes=61), 100.8, 3000, high=100.85, low=100.3))
    return out


WATCHLIST = {"AAA": {"market": "KR", "leaders": None, "inverse": False, "pair": None, "name": "테스트"}}


class FakeClient:
    def __init__(self, candles, daily=None, history=None):
        self.candles = candles
        self.daily = daily or []          # 일봉 (전일 종가용)
        self.history = history or []      # 프로파일용 긴 이력
        self.account_seq = "1"

    def get_candles(self, symbol, interval="1m", count=120):
        if interval == "1d":
            return self.daily
        return self.candles[-count:]

    def get_candles_paged(self, symbol, pages):
        return self.history

    def get_market_calendar(self, market):
        return None

    def get_prices(self, symbols):
        return {}

    def get_holdings(self):
        return {}

    def us_regular_close(self):
        return None

    def load_account(self):
        return True


class CaptureNotifier:
    def __init__(self):
        self.sent = []

    def send(self, signal, force=False):
        self.sent.append([signal.title, signal.label, signal.body])
        return {"capture": "ok"}


STEPS = [
    # (현재가, 보유)  → evaluate 한 번씩
    (100.8, {}),                                   # 돌파 → 🔵 매수하세요
    (100.85, {}),                                  # 아직 미진입 → 🔵 매수하세요 (조건 유지)
    (100.9, {"AAA": {"qty": 10.0, "avg": 100.8}}), # 보유 전환. 유예 중이라 알림 없음
    (100.2, {"AAA": {"qty": 10.0, "avg": 100.8}}), # 신호봉 저점 100.3 이탈 → 🔴 매도하세요
    (100.2, {}),                                   # 청산됨 → ✅ 손절 완료
]


def drive(engine, notifier) -> list:
    for price, holdings in STEPS:
        engine.evaluate("AAA", {"AAA": price}, holdings)
    return notifier.sent
