"""지표 — 순수 함수. 캔들 리스트(시간순, 완성봉)를 받아 값을 돌려준다.

지표는 '완성된 봉'으로만 계산한다. 마지막 봉은 진행 중이라 거래량이 부분값이다.
"""

from .config import (ATR_BAND_MULT, MIN_PROFILE_SESSIONS, OPEN_EXCLUDE_MIN,
                     RVOL_WINDOW, STRONG_BAR_MIN, VWAP_BAND_PCT)
from .timeutil import parse_ts


def _bucket(c: dict, market: str):
    """캔들 -> (세션 날짜 'YYYY-MM-DD', 시각 'HH:MM') 현지 기준. 실패 시 None."""
    dt = parse_ts(c.get("timestamp"), market)
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")


def _regular_minutes(market: str) -> tuple:
    """정규장 시작/종료를 '자정 이후 분'으로. (US 09:30~16:00, KR 09:00~15:30)"""
    return (9 * 60 + 30, 16 * 60) if market == "US" else (9 * 60, 15 * 60 + 30)


def _is_regular(hhmm: str, market: str) -> bool:
    """HH:MM 이 정규장 안인지."""
    try:
        h, m = hhmm.split(":")
        t = int(h) * 60 + int(m)
    except ValueError:
        return False
    s, e = _regular_minutes(market)
    return s <= t < e


def _minutes_from_open(hhmm: str, market: str) -> int:
    """정규장 개장 후 몇 분째인지. 개장 전이면 음수."""
    try:
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m) - _regular_minutes(market)[0]
    except ValueError:
        return -1


def build_volume_profile(candles: list, market: str, exclude_session: str = None) -> dict:
    """현지 시각(HH:MM) -> 과거 그 시각의 거래량 목록.

    장중 거래량은 U자 형태라 시간대별로 비교해야 '평소 대비'가 성립한다.
    오늘 세션(exclude_session)은 뺀다. 안 빼면 자기 자신과 비교하는 꼴이 되어
    급등한 날일수록 기준선이 함께 올라가 RVOL 이 과소평가된다.
    """
    profile = {}
    for c in candles:
        b = _bucket(c, market)
        if b is None or (exclude_session and b[0] == exclude_session):
            continue
        # 정규장 봉만 넣는다. 프리마켓·애프터 봉이 섞이면 버킷이 하루 960개로
        # 흩어져 버킷당 표본이 2개 남짓이 되고, 대부분 이동평균으로 떨어진다.
        # 정규장만이면 390개 버킷에 표본이 집중된다.
        if not _is_regular(b[1], market):
            continue
        try:
            profile.setdefault(b[1], []).append(float(c["volume"]))
        except (KeyError, TypeError, ValueError):
            continue
    return profile


def _median(nums: list) -> float:
    if not nums:
        return 0.0
    s = sorted(nums)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def compute_rvol(candles: list, market: str, profile: dict = None) -> tuple:
    """(직전봉 RVOL, 현재봉 RVOL, 계산방식).

    프로파일이 있으면 같은 현지 시각의 과거 중앙값과 비교, 없으면 직전 20봉 평균.
    두 기준선은 스케일이 달라 방식을 함께 돌려준다. 직전봉과 현재봉의 방식이
    다르면 '혼합'으로 표시하고, 호출부는 그 경우 돌파 판정을 보류한다.
    """
    if len(candles) < RVOL_WINDOW + 2:
        return 0.0, 0.0, "부족"
    vols = [float(c["volume"]) for c in candles]
    methods = []

    def at(i: int) -> float:
        if profile:
            b = _bucket(candles[i], market)
            hist = profile.get(b[1], []) if b else []
            if len(hist) >= MIN_PROFILE_SESSIONS:
                base = _median(hist)
                if base > 0:
                    methods.append("프로파일")
                    return round(vols[i] / base, 2)
        methods.append("이동평균")
        past = vols[i - RVOL_WINDOW:i]
        avg = sum(past) / len(past) if past else 0
        return round(vols[i] / avg, 2) if avg > 0 else 0.0

    prev, cur = at(len(vols) - 2), at(len(vols) - 1)
    method = methods[-1] if len(set(methods)) == 1 else "혼합"
    return prev, cur, method


def rvol_at(candles: list, i: int, market: str, profile: dict = None) -> float:
    """i번째 봉의 RVOL. 프로파일이 있으면 같은 시각 과거 중앙값, 없으면 직전 20봉 평균."""
    if i < RVOL_WINDOW:
        return 0.0
    try:
        cur = float(candles[i]["volume"])
    except (KeyError, TypeError, ValueError):
        return 0.0
    if profile:
        b = _bucket(candles[i], market)
        hist = profile.get(b[1], []) if b else []
        if len(hist) >= MIN_PROFILE_SESSIONS:
            base = _median(hist)
            if base > 0:
                return round(cur / base, 2)
    past = [float(c["volume"]) for c in candles[i - RVOL_WINDOW:i]]
    avg = sum(past) / len(past) if past else 0
    return round(cur / avg, 2) if avg > 0 else 0.0


def session_peak_rvol(candles: list, market: str, profile: dict = None) -> float:
    """오늘 세션 중 최고 RVOL.

    실행 중 관측한 값만 쌓으면 장중에 스크립트를 켰을 때 이전 정점을 모른다.
    캔들 이력에서 직접 계산하면 언제 켜도 그날의 정점이 잡힌다.
    """
    if len(candles) < RVOL_WINDOW + 1:
        return 0.0
    last = _bucket(candles[-1], market)
    if last is None:
        return 0.0
    session = last[0]
    peak = 0.0
    for i in range(RVOL_WINDOW, len(candles)):
        b = _bucket(candles[i], market)
        if b is None or b[0] != session:
            continue
        # 개장 직후 봉은 정점에서 뺀다. 개장봉은 구조적으로 거래량이 몰려
        # 70배 같은 값이 나오는데, 그걸 정점으로 잡으면 3분 뒤 정상화를
        # '연료 소진'으로 오독해 팔라고 하게 된다.
        if 0 <= _minutes_from_open(b[1], market) < OPEN_EXCLUDE_MIN:
            continue
        peak = max(peak, rvol_at(candles, i, market, profile))
    return round(peak, 2)


def compute_vwap(candles: list, market: str) -> float:
    """당일(현지 세션 기준) VWAP. typical price = (고+저+종)/3."""
    if not candles:
        return 0.0
    last = _bucket(candles[-1], market)
    if last is None:
        return 0.0
    session = last[0]
    pv = vol = 0.0
    for c in candles:
        b = _bucket(c, market)
        if b is None or b[0] != session:
            continue
        try:
            tp = (float(c["highPrice"]) + float(c["lowPrice"]) + float(c["closePrice"])) / 3
            v = float(c["volume"])
        except (KeyError, TypeError, ValueError):
            continue
        pv += tp * v
        vol += v
    return round(pv / vol, 4) if vol > 0 else 0.0


def compute_ema(values: list, period: int) -> float:
    if len(values) < period:
        return 0.0
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return round(ema, 4)


def ema_alignment(candles: list) -> str:
    closes = [float(c["closePrice"]) for c in candles]
    if len(closes) < 50:
        return "unknown"
    e9, e20, e50 = compute_ema(closes, 9), compute_ema(closes, 20), compute_ema(closes, 50)
    if e9 > e20 > e50:
        return "정배열"
    if e9 < e20 < e50:
        return "역배열"
    return "혼조"


def compute_rsi(candles: list, period: int = 14) -> tuple:
    """(직전 RSI, 현재 RSI). 기울기를 보기 위해 둘 다."""
    closes = [float(c["closePrice"]) for c in candles]
    if len(closes) < period + 2:
        return 0.0, 0.0

    def at(end: int) -> float:
        g = l = 0.0
        for i in range(end - period + 1, end + 1):
            d = closes[i] - closes[i - 1]
            g += max(d, 0)
            l += max(-d, 0)
        if l == 0:
            return 100.0
        return round(100 - 100 / (1 + (g / period) / (l / period)), 1)

    return at(len(closes) - 2), at(len(closes) - 1)


def compute_atr_pct(candles: list, n: int = 20) -> float:
    """최근 n봉의 평균 진폭(고가-저가)을 가격 대비 %로.

    고정 밴드(0.15%)는 종목마다 의미가 다르다. SOXL 은 1분에 0.5% 가 보통이라
    0.15% 는 노이즈 안이고, 조용한 종목은 0.15% 도 큰 움직임이다.
    변동성에 맞춰 밴드를 늘리면 '사자마자 팔라'는 진동이 줄어든다.
    """
    if len(candles) < n:
        return 0.0
    ranges = []
    for c in candles[-n:]:
        try:
            hi, lo, cl = float(c["highPrice"]), float(c["lowPrice"]), float(c["closePrice"])
            if cl > 0:
                ranges.append((hi - lo) / cl * 100)
        except (KeyError, TypeError, ValueError):
            continue
    return round(sum(ranges) / len(ranges), 3) if ranges else 0.0


def effective_band(candles: list) -> float:
    """실제 적용할 밴드 %. 고정값과 변동성 기반값 중 큰 쪽."""
    atr = compute_atr_pct(candles)
    return max(VWAP_BAND_PCT, round(atr * ATR_BAND_MULT, 3))


def strong_bar(candle: dict) -> bool:
    """종가가 봉 범위의 상단 절반에 있는가.

    거래량이 터졌어도 긴 윗꼬리에 종가가 아래라면 매수세가 밀린 봉이다.
    그런 봉에서 진입하면 바로 눌린다.
    """
    try:
        hi, lo, cl = float(candle["highPrice"]), float(candle["lowPrice"]), float(candle["closePrice"])
    except (KeyError, TypeError, ValueError):
        return False
    rng = hi - lo
    if rng <= 0:
        return True
    return (cl - lo) / rng >= STRONG_BAR_MIN


def vwap_position(price: float, vwap: float, band: float = None) -> str:
    """밴드는 호출부가 종목 변동성에 맞춰 넘긴다. 없으면 고정값."""
    if vwap <= 0 or price <= 0:
        return "neutral"
    b = band if band is not None else VWAP_BAND_PCT
    diff = (price - vwap) / vwap * 100
    if diff > b:
        return "above"
    if diff < -b:
        return "below"
    return "neutral"
