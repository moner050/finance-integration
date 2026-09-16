"""지표 — 순수 함수. 캔들 리스트(시간순, 완성봉)를 받아 값을 돌려준다.

지표는 '완성된 봉'으로만 계산한다. 마지막 봉은 진행 중이라 거래량이 부분값이다.
세션 누적값(VWAP, 정점 RVOL)은 SessionState 가 봉을 하나씩 더해 유지한다.
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


def bar_minutes_from_open(candle: dict, market: str) -> int:
    """이 봉이 정규장 개장 후 몇 분째인지. 개장 전이면 음수, 시각을 못 읽으면 -1."""
    b = _bucket(candle, market)
    return _minutes_from_open(b[1], market) if b else -1


def is_regular_bar(candle: dict, market: str) -> bool:
    """이 봉이 정규장 봉인지. 프리마켓·시간외(NXT 포함) 봉이면 False."""
    b = _bucket(candle, market)
    return bool(b) and _is_regular(b[1], market)


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


def rvol_at(candles: list, i: int, market: str, profile: dict = None) -> tuple:
    """i번째 봉의 (RVOL, 계산방식). 방식은 '프로파일' | '이동평균' | '부족'.

    프로파일이 있으면 같은 현지 시각의 과거 중앙값과 비교한다. 없으면 직전 정규장
    봉 RVOL_WINDOW 개의 평균이다. 프리마켓·시간외 봉은 기준선에서 뺀다 — 얇은
    거래량이 평균을 끌어내려 개장 직후 RVOL 이 과대평가되고 가짜 돌파가 나온다.
    정규장 봉이 RVOL_WINDOW 개에 못 미치면 '부족'으로 돌려 판단을 보류하게 한다.
    """
    try:
        cur = float(candles[i]["volume"])
    except (KeyError, TypeError, ValueError, IndexError):
        return 0.0, "부족"
    b = _bucket(candles[i], market)
    if profile and b:
        hist = profile.get(b[1], [])
        if len(hist) >= MIN_PROFILE_SESSIONS:
            base = _median(hist)
            if base > 0:
                return round(cur / base, 2), "프로파일"
    past = []
    for j in range(i - 1, -1, -1):
        bj = _bucket(candles[j], market)
        if bj is None or not _is_regular(bj[1], market):
            continue
        try:
            past.append(float(candles[j]["volume"]))
        except (KeyError, TypeError, ValueError):
            continue
        if len(past) == RVOL_WINDOW:
            break
    if len(past) < RVOL_WINDOW:
        return 0.0, "부족"
    avg = sum(past) / RVOL_WINDOW
    if avg <= 0:
        return 0.0, "부족"
    return round(cur / avg, 2), "이동평균"


def compute_rvol(candles: list, market: str, profile: dict = None) -> tuple:
    """(직전봉 RVOL, 현재봉 RVOL, 계산방식).

    프로파일이 있으면 같은 현지 시각의 과거 중앙값과 비교, 없으면 직전 정규장 20봉 평균.
    두 기준선은 스케일이 달라 방식을 함께 돌려준다. 직전봉과 현재봉의 방식이
    다르면 '혼합'으로 표시하고, 호출부는 그 경우 돌파 판정을 보류한다.
    """
    n = len(candles)
    if n < 2:
        return 0.0, 0.0, "부족"
    prev, prev_method = rvol_at(candles, n - 2, market, profile)
    cur, cur_method = rvol_at(candles, n - 1, market, profile)
    if "부족" in (prev_method, cur_method):
        method = "부족"
    else:
        method = prev_method if prev_method == cur_method else "혼합"
    return prev, cur, method


class SessionState:
    """종목 하나의 당일 정규장 누적값 — VWAP 과 세션 정점 RVOL.

    120봉 창으로 계산하면 개장 2시간 뒤부터 앞부분이 잘려 VWAP 이 '세션 VWAP' 이 아니라
    '최근 2시간 VWAP' 으로 표류하고, 오전 정점이 창 밖으로 나가 익절 판단이 틀어진다.
    그래서 정규장 완성봉을 하나씩 누적한다. 기동 시엔 프로파일용으로 받은 긴 이력으로
    오늘 세션을 백필하고, 이후엔 사이클마다 새 봉만 더한다. 같은 봉은 두 번 넣지 않는다.

    프리마켓·시간외 봉은 넣지 않는다. 프로파일과 같은 기준이어야 정점 RVOL 과 VWAP 이
    같은 세션을 말한다. 개장 후 OPEN_EXCLUDE_MIN 분은 정점 계산에서 뺀다 — 개장봉은
    구조적으로 거래량이 몰려 70배 같은 값이 나오는데, 그걸 정점으로 잡으면 3분 뒤
    정상화를 '연료 소진'으로 오독해 팔라고 하게 된다.
    """

    def __init__(self, market: str):
        self.market = market
        self.session = None      # 'YYYY-MM-DD' (현지)
        self.pv = 0.0            # Σ typical price × volume
        self.vol = 0.0
        self.peak = 0.0          # 세션 정점 RVOL
        self.last_dt = None      # 마지막으로 반영한 봉의 현지시각

    def _reset(self, session: str):
        self.session, self.pv, self.vol, self.peak, self.last_dt = session, 0.0, 0.0, 0.0, None

    def update(self, candles: list, profile: dict = None) -> int:
        """시간순 완성봉을 넣는다. 마지막(최신) 세션에 새로 반영한 봉 수를 돌려준다."""
        added = 0
        for i, c in enumerate(candles):
            dt = parse_ts(c.get("timestamp"), self.market)
            if dt is None:
                continue
            session, hhmm = dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
            if self.session is None or session > self.session:
                self._reset(session)
                added = 0                     # 백필 이력의 이전 세션은 세지 않는다
            elif session < self.session:
                continue                      # 창에 남아 있는 전 세션 봉
            if self.last_dt is not None and dt <= self.last_dt:
                continue                      # 이미 반영한 봉
            if not _is_regular(hhmm, self.market):
                continue
            try:
                tp = (float(c["highPrice"]) + float(c["lowPrice"]) + float(c["closePrice"])) / 3
                v = float(c["volume"])
            except (KeyError, TypeError, ValueError):
                continue
            self.pv += tp * v
            self.vol += v
            if _minutes_from_open(hhmm, self.market) >= OPEN_EXCLUDE_MIN:
                self.peak = max(self.peak, rvol_at(candles, i, self.market, profile)[0])
            self.last_dt = dt
            added += 1
        return added

    @property
    def vwap(self) -> float:
        """당일 정규장 VWAP. typical price = (고+저+종)/3."""
        return round(self.pv / self.vol, 4) if self.vol > 0 else 0.0

    def to_dict(self) -> dict:
        return {"session": self.session, "pv": self.pv, "vol": self.vol, "peak": self.peak,
                "last_dt": self.last_dt.isoformat() if self.last_dt else None}

    @classmethod
    def from_dict(cls, market: str, d: dict) -> "SessionState":
        s = cls(market)
        s.session = d.get("session")
        s.pv, s.vol, s.peak = float(d.get("pv", 0)), float(d.get("vol", 0)), float(d.get("peak", 0))
        s.last_dt = parse_ts(d["last_dt"], market) if d.get("last_dt") else None
        return s


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
    """(직전 RSI, 현재 RSI). 기울기를 보기 위해 둘 다.

    Wilder 평활을 쓴다 — 토스 앱을 비롯한 차트 프로그램과 같은 방식이라 값을 대조할 수 있다.
    단순평균(Cutler) 방식은 창에서 큰 변화가 빠져나가는 순간 값이 툭 튀어 기울기가 흔들린다.
    """
    closes = [float(c["closePrice"]) for c in candles]
    if len(closes) < period + 2:
        return 0.0, 0.0
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]

    def rsi(g: float, l: float) -> float:
        if l == 0:
            return 100.0
        return round(100 - 100 / (1 + g / l), 1)

    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    series = [rsi(avg_g, avg_l)]
    for g, l in zip(gains[period:], losses[period:]):
        avg_g = (avg_g * (period - 1) + g) / period
        avg_l = (avg_l * (period - 1) + l) / period
        series.append(rsi(avg_g, avg_l))
    return series[-2], series[-1]


def compute_atr_pct(candles: list, n: int = 20) -> float:
    """최근 n봉의 평균 True Range 를 가격 대비 %로.

    TR = max(고-저, |고-전봉 종가|, |저-전봉 종가|). 단순 고저폭은 봉 사이 갭을 놓친다.
    고정 밴드(0.15%)는 종목마다 의미가 다르다. SOXL 은 1분에 0.5% 가 보통이라
    0.15% 는 노이즈 안이고, 조용한 종목은 0.15% 도 큰 움직임이다.
    변동성에 맞춰 밴드를 늘리면 '사자마자 팔라'는 진동이 줄어든다.
    """
    if len(candles) < n + 1:
        return 0.0
    ranges = []
    for prev, c in zip(candles[-n - 1:-1], candles[-n:]):
        try:
            hi, lo, cl = float(c["highPrice"]), float(c["lowPrice"]), float(c["closePrice"])
            pc = float(prev["closePrice"])
        except (KeyError, TypeError, ValueError):
            continue
        if cl > 0:
            ranges.append(max(hi - lo, abs(hi - pc), abs(lo - pc)) / cl * 100)
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
