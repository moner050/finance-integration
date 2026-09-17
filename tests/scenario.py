"""엔진 시나리오 — 원본과 분리본이 같은 알림을 내는지 비교하는 데 쓴다.

가짜 클라이언트는 고정 캔들·현재가를 돌려주고(엔진은 계좌를 읽지 않는다 — 보유는 evaluate 에 넘기는 가상 장부), 가짜 알림기는 쿨다운 없이
모든 send 호출을 기록한다. 시각은 2026-03-25 10:10 KST 로 고정한다.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from tests.conftest import TZ, bar

FIXED_NOW_UTC = datetime(2026, 3, 25, 1, 10, tzinfo=ZoneInfo("UTC"))   # 10:10 KST


def fixed_now_local(market: str) -> datetime:
    return FIXED_NOW_UTC.astimezone(TZ[market])


def scenario_candles(start_hour: int = 9, start_minute: int = 0) -> list:
    """시작 시각부터 KR 62봉. 60번째까지 조용하고, 60(거래량 1.5배)→61(3.4배, 강봉, 기준선 위) 돌파.
    기본 09:00~10:01. 돌파봉 거래량은 확신도 기준(ENTRY_STRONG_RVOL 3배)을 넘겨 '매수하세요' 가 되게 한다."""
    t0 = datetime(2026, 3, 25, start_hour, start_minute, tzinfo=TZ["KR"])
    out = []
    for i in range(60):
        close = 100.0 + (i % 3) * 0.05
        out.append(bar(t0 + timedelta(minutes=i), close, 1000, high=close + 0.1, low=close - 0.1))
    out.append(bar(t0 + timedelta(minutes=60), 100.3, 1500, high=100.4, low=100.1))
    out.append(bar(t0 + timedelta(minutes=61), 100.8, 3500, high=100.85, low=100.3))
    return out


WATCHLIST = {"AAA": {"market": "KR", "leaders": None, "inverse": False, "pair": None, "name": "테스트"}}


class FakeClient:
    def __init__(self, candles, daily=None, history=None):
        self.candles = candles
        self.daily = daily or []          # 일봉 (전일 종가용)
        self.history = history or []      # 프로파일용 긴 이력

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

    def us_regular_close(self):
        return None


class CaptureNotifier:
    def __init__(self):
        self.sent = []
        self.signals = []           # Signal 원본 (account 줄 확인용)

    def send(self, signal, force=False):
        self.sent.append([signal.title, signal.label, signal.body])
        self.signals.append(signal)
        return {"capture": "ok"}


STEPS = [
    # (현재가, 보유)  → evaluate 한 번씩
    (100.8, {}),                                   # 돌파 → 🔵 매수하세요 (확인 3/3) → 곧바로 신호 포지션(보유)
    (100.85, {}),                                  # 내 계좌 미보유지만 대기·만료 알림 없음. 익절 유예 중이라 침묵
    (100.9, {"AAA": {"qty": 10.0, "avg": 100.8}}), # 내 매수 확인. 유예 중이라 알림 없음
    (100.2, {"AAA": {"qty": 10.0, "avg": 100.8}}), # 10:02 봉 종가 100.2 가 신호봉 저점 100.3 이탈, 매도 거래량 → 🔴 매도하세요
    (100.2, {}),                                   # 청산됨 → ✅ 손절 완료
]
# 매도·취소 판정은 완성봉 종가라 4단계 전에 하락 봉이 하나 필요하다. run_steps 가 해당 단계 직전에 붙인다.
# 거래량 3000(≈2.7배)은 이탈이 얕아도(밴드 안) '매도하세요' 가 되게 하는 매도 물량이다.
STEP_BARS = {3: bar(datetime(2026, 3, 25, 10, 2, tzinfo=TZ["KR"]), 100.2, 3000, high=100.75, low=100.1)}


def run_steps(engine, indexes=None, symbol="AAA"):
    """STEPS 를 순서대로 evaluate 한다. indexes 를 주면 그 단계만."""
    for i in (range(len(STEPS)) if indexes is None else indexes):
        if i in STEP_BARS:
            engine.client.candles = engine.client.candles + [STEP_BARS[i]]
        price, holdings = STEPS[i]
        engine.evaluate(symbol, {symbol: price}, holdings)


def drive(engine, notifier) -> list:
    run_steps(engine)
    return notifier.sent
