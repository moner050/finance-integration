"""테스트 공용 픽스처 — 합성 캔들.

값은 토스 API 처럼 숫자 문자열로 넣는다. 지표 함수가 float() 변환을 하므로
문자열을 그대로 쓰는 편이 실제 응답과 같은 경로를 탄다.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = {"US": ZoneInfo("America/New_York"), "KR": ZoneInfo("Asia/Seoul")}


def bar(ts: datetime, close: float, volume: float, high: float = None,
        low: float = None, open_: float = None, market: str = "KR") -> dict:
    """1분봉 하나. 고가/저가를 안 주면 종가 ±0.5 로 잡는다 (종가가 봉 중앙 → 강봉 경계값)."""
    high = close + 0.5 if high is None else high
    low = close - 0.5 if low is None else low
    open_ = close if open_ is None else open_
    return {
        "timestamp": ts.isoformat(),
        "openPrice": f"{open_:.4f}", "highPrice": f"{high:.4f}",
        "lowPrice": f"{low:.4f}", "closePrice": f"{close:.4f}",
        "volume": f"{volume:g}",
        "currency": "KRW" if market == "KR" else "USD",
    }


def make_candles(market: str, date: str, start: str, n: int, *,
                 volume: float = 1000, close: float = 100.0, step: float = 0.0,
                 volumes: list = None, closes: list = None) -> list:
    """`start`(HH:MM, 현지시각)부터 1분 간격으로 n개. volumes/closes 로 봉별 값을 지정한다."""
    y, m, d = map(int, date.split("-"))
    h, mi = map(int, start.split(":"))
    t0 = datetime(y, m, d, h, mi, tzinfo=TZ[market])
    out = []
    for i in range(n):
        c = closes[i] if closes is not None else close + step * i
        v = volumes[i] if volumes is not None else volume
        out.append(bar(t0 + timedelta(minutes=i), c, v, market=market))
    return out


def make_sessions(market: str, dates: list, start: str, n: int, volume: float = 1000,
                  close: float = 100.0) -> list:
    """여러 세션의 봉을 시간순으로 이어 붙인다 (프로파일 구축용)."""
    out = []
    for d in dates:
        out.extend(make_candles(market, d, start, n, volume=volume, close=close))
    return out


def kr_fixture():
    """골든 테스트용 고정 세트: 오늘(2026-03-25) 70봉 + 직전 4세션.

    거래량은 결정적 수식으로 만들고 40·41번째 봉에 급증을 넣는다.
    """
    n = 70
    closes = [round(100 + ((i * 7) % 11) * 0.3 - (i % 5) * 0.2, 4) for i in range(n)]
    volumes = [800 + (i * 53) % 900 for i in range(n)]
    volumes[40] = 6000
    volumes[41] = 3500
    cur = make_candles("KR", "2026-03-25", "09:00", n, closes=closes, volumes=volumes)
    hist = make_sessions("KR", ["2026-03-19", "2026-03-20", "2026-03-23", "2026-03-24"], "09:00", n)
    return cur, hist
