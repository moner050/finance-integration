"""시장 전체 일봉 스캔 — 투자대회 우승자 규칙을 항목별로 그대로 구현해 전 종목에서 검증한다.

데이터: data/backtest/daily/{심볼}.json (scan_fetch.py), 유니버스 data/backtest/universe/universe.csv, 지수 data/backtest/{069500,SPY,QQQ}_1d.json
평가 기간: TEST_FROM ~ 마지막 세션. 표본 내 < SPLIT ≤ 표본 외. 비용 왕복 COST% (소형주 슬리피지 포함해 0.5%).

우승자 규칙 → 구현 (원문 값 그대로, 근사한 곳은 주석)
  공통 유니버스 필터  가격 ≥ KR 1,000원 / US $3, 20일 평균 거래대금 ≥ KR 10억 / US $10M (Kullamägi "$1~3M+ 달러 거래량", Minervini 유동성)
  상대강도(RS)        Minervini/IBD: 가중 12개월 수익률(2×3M + 6M + 9M + 12M) 시장별 백분위. Kullamägi: 1·3·6개월 상승률 상위 1~2%
  국면               Kell: QQQ(KODEX200) > 10·20일 EMA. 폭: 유니버스 중 20일선 위 비율. 결과는 국면별로 나눠 본다

  KQ_FLAG   Kullamägi 돌파: 1/3/6개월 상승률 시장 상위 2% 또는 1~3개월 +30%+, ADR20 ≥ 3.5%, 5~40세션 수렴(최근 10세션 범위 ≤ 2.5×ADR, 저점 상승),
            10·20일선 위·정배열. 트리거: 고가 > 피벗(직전 10세션 고점)·종가 > 피벗·종가 봉 상단 절반·거래량 ≥ 1×ADV20. 진입 = max(시가, 피벗×1.002)(매수 스탑),
            손절 = 당일 저가. 청산: 3세션 뒤 종가에 1/3 익절 → 본전 손절, 나머지 10일선 종가 이탈 → 다음 시가 (변형: 20일선)
  KQ_EP     Kullamägi EP: 갭 ≥ 10%, 거래량 ≥ 2×ADV20, 6개월 상승률 ≤ 30%(쉬었던 종목), 종가 > 시가. 진입 = 시가×1.01(ORH 근사, 고가가 닿아야),
            손절 = 당일 저가. 청산: 20일선 추적, 최대 60세션 (부분 익절 없음 — 수 주 보유)
  KQ_PARA_S Kullamägi 파라볼릭 숏(참고, 엔진은 롱 전용): 5세션 ≤ 안 +50%(대형 +30%)·3일 연속 상승 뒤 첫 음봉 다음 시가 숏, 손절 = 급등 고점, 커버 = 종가 ≤ 10일선 또는 10세션
  KELL_WEDGE Kell Wedge Pop: 10~30세션 고점 하락(회귀 기울기 < 0)·거래량 감소(vol10 < vol30)·10/20 EMA 아래에서, 10·20 EMA 위로 종가 회복 + 직전 5세션 고점 돌파 + 거래량 ≥ 1.2×ADV.
            진입 종가, 손절 당일 저가, 청산 10 EMA 추적, 10 EMA 대비 +8% 이상 확장 시 절반 익절
  KELL_CROSS Kell EMA Crossback: 10 EMA > 20 EMA > 50일선, 15세션 안 20일 신고가, 최근 3세션 저가가 20 EMA(+1%) 터치, 오늘 종가 > 10 EMA·전일 고점.
            진입 종가, 손절 눌림 저점, 청산 10 EMA 추적 + 확장 절반 익절
  KELL_BASE  Kell Base n Break: 15세션 이상 범위 ≤ 15% 베이스, 50일선 위, 베이스 고점 돌파 + 거래량 ≥ 1.5×ADV. 진입 매수 스탑, 손절 당일 저가, 청산 20 EMA 추적
  MV_VCP    Minervini 추세 템플릿 8항목(종가 > 150·200일선, 150 > 200, 200일선 1개월 상승, 50 > 150 > 200, 종가 > 50, 52주 저점 +30%↑, 52주 고점 −25% 안, RS ≥ 70)
            + VCP(60세션 안 2개 이상 수축, 각 수축이 앞 것보다 얕음, 마지막 수축 ≤ 10%, 거래량 고갈 vol5 ≤ 0.7×vol50) + 피벗 돌파 거래량 ≥ 1.5×ADV50.
            진입 매수 스탑(피벗×1.002), 손절 max(진입 −7%, 마지막 수축 저점). 청산: +20% 또는 10 EMA 대비 +10% 확장 3일 연속이면 절반, 2R 뒤 본전, 21 EMA 종가 이탈 → 다음 시가, 최대 60세션
  KR_CLOSE  국내 종가베팅: 당일 +5%↑(상한가 아님), 종가 ≥ 고가×0.98, 거래량 ≥ 3×ADV20, 당일 거래대금 시장 상위 30. 진입 종가 → 다음날 시가 (변형: 다음날 종가)
  KR_LIMIT  국내 상한가 따라잡기: 종가 ≥ 전일 +29% (KR 만). 진입 종가 → 다음날 시가
  KR_PULL   국내 급등 후 첫 눌림목: 10세션 안 +10%↑·거래량 5×ADV 급등일 뒤, 저가가 5일선(+1%) 터치·거래량 급등일의 절반 미만·20일선 위 유지, 오늘 양봉·종가 > 5일선.
            진입 종가, 손절 눌림 저점, 청산 +10% 또는 5일선 종가 이탈, 최대 10세션
사용: python tools/backtest/scan.py [KR|US|ALL]  → data/backtest/results/scan_report_{시장}.md, scan_trades_{시장}.json
"""
import csv
import json
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from alertbot.timeutil import parse_ts                        # noqa: E402
from tools.backtest.universe import DATA_DIR, daily_path      # noqa: E402

DAILY = DATA_DIR / "daily"
UNI = DATA_DIR / "universe" / "universe.csv"
TEST_FROM, SPLIT = "2026-01-02", "2026-07-01"
COST = 0.5
IDX = {"KR": "069500", "US": "QQQ"}
LIQ = {"KR": (1_000, 1_000_000_000), "US": (3.0, 10_000_000)}
LIMIT_UP_KR = 29.0


# ---------------------------------------------------------------------------
# 데이터
# ---------------------------------------------------------------------------
def load_symbol(sym: str, market: str, path: Path = None):
    p = path or daily_path(sym)
    if not p.exists():
        return None
    raw = json.loads(p.read_text(encoding="utf-8"))
    if len(raw) < 260:
        return None
    rows = sorted(((parse_ts(b["timestamp"], market).strftime("%Y-%m-%d"), float(b["openPrice"]), float(b["highPrice"]),
                    float(b["lowPrice"]), float(b["closePrice"]), float(b["volume"])) for b in raw), key=lambda r: r[0])
    # 같은 날짜 중복 제거(마지막 값)
    dd = {}
    for r in rows:
        dd[r[0]] = r
    rows = [dd[k] for k in sorted(dd)]
    d = {"sym": sym, "market": market, "dates": [r[0] for r in rows]}
    for j, k in enumerate(("o", "h", "l", "c", "v"), start=1):
        d[k] = np.array([r[j] for r in rows], dtype=float)
    return d


def _sma(x, n):
    out = np.full_like(x, np.nan)
    if len(x) >= n:
        c = np.cumsum(np.insert(x, 0, 0.0))
        out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def _ema(x, n):
    out = np.empty_like(x); k = 2 / (n + 1); e = x[0]
    for i, v in enumerate(x):
        e = v if i == 0 else v * k + e * (1 - k)
        out[i] = e
    return out


def _shift(x, n):
    out = np.full_like(x, np.nan); out[n:] = x[:-n]; return out


def _roll_max(x, n):
    out = np.full_like(x, np.nan)
    for i in range(n - 1, len(x)):
        out[i] = x[i - n + 1:i + 1].max()
    return out


def _roll_min(x, n):
    out = np.full_like(x, np.nan)
    for i in range(n - 1, len(x)):
        out[i] = x[i - n + 1:i + 1].min()
    return out


def indicators(d: dict):
    o, h, l, c, v = d["o"], d["h"], d["l"], d["c"], d["v"]
    d["ma5"], d["ma10"], d["ma20"], d["ma50"], d["ma150"], d["ma200"] = (_sma(c, n) for n in (5, 10, 20, 50, 150, 200))
    d["ema10"], d["ema20"], d["ema21"] = _ema(c, 10), _ema(c, 20), _ema(c, 21)
    d["adr20"] = _sma((h / l - 1) * 100, 20)
    d["adv20"], d["adv50"] = _sma(c * v, 20), _sma(c * v, 50)
    d["vol20"], d["vol50"], d["vol5"], d["vol10"], d["vol30"] = _sma(v, 20), _sma(v, 50), _sma(v, 5), _sma(v, 10), _sma(v, 30)
    for n, name in ((21, "r1m"), (63, "r3m"), (126, "r6m"), (189, "r9m"), (252, "r12m")):
        d[name] = (c / _shift(c, n) - 1) * 100
    d["rs_raw"] = 2 * d["r3m"] + d["r6m"] + d["r9m"] + d["r12m"]
    d["hi52"], d["lo52"] = _roll_max(h, 250), _roll_min(l, 250)
    d["hi10p"], d["lo10p"] = _shift(_roll_max(h, 10), 1), _shift(_roll_min(l, 10), 1)       # 직전 10세션(오늘 제외)
    d["hi20p"], d["hi5p"] = _shift(_roll_max(h, 20), 1), _shift(_roll_max(h, 5), 1)
    d["ma200_prev20"] = _shift(d["ma200"], 20)
    d["prev_c"] = _shift(c, 1)
    d["ret"] = (c / d["prev_c"] - 1) * 100
    d["gap"] = (o / d["prev_c"] - 1) * 100
    d["turnover"] = c * v
    return d


# ---------------------------------------------------------------------------
# 시장 단위 (날짜별 순위·국면)
# ---------------------------------------------------------------------------
class Market:
    def __init__(self, market: str, syms: dict, idx: dict):
        self.market, self.syms, self.idx = market, syms, idx
        self.pct = {}       # (date) -> {sym: {rs, m1, m3, m6, turnover}} 백분위
        self.breadth = {}   # date -> % above ma20
        dates = sorted({d for s in syms.values() for d in s["dates"] if d >= "2025-06-01"})
        pos = {t: {d: i for i, d in enumerate(s["dates"])} for t, s in syms.items()}
        self.pos = pos
        for d in dates:
            vals = {"rs": {}, "m1": {}, "m3": {}, "m6": {}, "tv": {}}
            above = tot = 0
            for t, s in syms.items():
                i0 = pos[t].get(d)
                if i0 is None or i0 < 253:
                    continue
                i = i0 - 1                      # 어제 종가 기준 (장 시작 전에 아는 값)
                if not self.liquid(s, i):
                    continue
                for key, arr in (("rs", "rs_raw"), ("m1", "r1m"), ("m3", "r3m"), ("m6", "r6m"), ("tv", "turnover")):
                    x = s[arr][i]
                    if not np.isnan(x):
                        vals[key][t] = x
                if not np.isnan(s["ma20"][i]):
                    tot += 1; above += s["c"][i] > s["ma20"][i]
            pct = defaultdict(dict)
            for key, m in vals.items():
                if not m:
                    continue
                order = sorted(m, key=lambda z: m[z])
                n = len(order)
                for r, t in enumerate(order):
                    pct[t][key] = 100 * r / max(n - 1, 1)
            self.pct[d] = pct
            self.breadth[d] = 100 * above / tot if tot else None

    def liquid(self, s, i):
        lo_px, lo_tv = LIQ[self.market]
        return s["c"][i] >= lo_px and not np.isnan(s["adv20"][i]) and s["adv20"][i] >= lo_tv

    def regime(self, d):
        """Kell: 지수 > 10·20 EMA. 어제 종가 기준."""
        i = self.idx_pos.get(d)
        if i is None or i < 1:
            return None
        x, j = self.idx, i - 1
        return bool(x["c"][j] > x["ema10"][j] and x["c"][j] > x["ema20"][j])

    def index_ret(self, d_from, n_days):
        i = self.idx_pos.get(d_from)
        if i is None:
            return 0.0
        j = min(i + n_days, len(self.idx["c"]) - 1)
        return (self.idx["c"][j] - self.idx["c"][i]) / self.idx["c"][i] * 100


# ---------------------------------------------------------------------------
# 셋업 판정 (i = 오늘 인덱스). 반환: dict(entry, stop, kind) 또는 None
# ---------------------------------------------------------------------------
def _pos(s, i):
    rng = s["h"][i] - s["l"][i]
    return 1.0 if rng <= 0 else (s["c"][i] - s["l"][i]) / rng


def _buy_stop(s, i, pivot):
    """매수 스탑 체결가. 오늘 고가가 피벗을 넘으면 체결되고, 갭으로 위에서 열리면 시가에 체결된다.
    오늘의 종가·거래량·종가 위치는 조건에 쓰지 않는다 — 주문을 넣는 시점(장 시작 전)에는 모르는 값이라
    쓰면 미래를 보고 산 셈이 된다 (look-ahead)."""
    trigger = pivot * 1.002
    if s["h"][i] < trigger:          # 주문가에 닿지 않으면 체결이 없다
        return None
    return max(s["o"][i], trigger)


_WX = np.arange(20.0)
_WXC = _WX - _WX.mean()
_WXD = float((_WXC ** 2).sum())


# 판정 시점 규칙 — 이것을 어기면 미래를 보고 사는 셈이 된다 (look-ahead).
#   장 시작 전 판정 + 매수 스탑 진입(_buy_stop): 어제 종가까지의 값(i-1)만 조건에 쓴다. 오늘 값은 고가가 피벗을 넘었는지만.
#   종가 판정 + 종가 진입: 오늘 값(i)을 다 써도 된다 — 종가에 보고 종가에 사기 때문이다. 이름 끝에 _C 를 붙인다.
def kq_flag(s, i, pct):
    """Kullamägi 플래그 돌파 (장전 판정·매수 스탑). 선행 상승 + 변동성 + 수렴 + 이동평균 정배열은 전부 어제까지의 값."""
    if np.isnan(s["r3m"][i - 1]) or np.isnan(s["adr20"][i - 1]) or s["adr20"][i - 1] < 3.5:
        return None
    leader = (pct.get("m1", 0) >= 98 or pct.get("m3", 0) >= 98 or pct.get("m6", 0) >= 98
              or s["r1m"][i - 1] >= 30 or s["r3m"][i - 1] >= 30)
    if not leader:
        return None
    if not (s["c"][i - 1] > s["ma10"][i - 1] > s["ma20"][i - 1] and s["ma20"][i - 1] > s["ma20"][i - 6]):
        return None
    rng10 = (s["hi10p"][i] - s["lo10p"][i]) / s["c"][i - 1] * 100
    if np.isnan(rng10) or rng10 > 2.5 * s["adr20"][i - 1]:
        return None
    if not (s["l"][i - 5:i].min() > s["l"][i - 10:i - 5].min()):        # 저점 상승
        return None
    ent = _buy_stop(s, i, s["hi10p"][i])
    return None if ent is None else {"entry": ent, "stop": s["l"][i], "setup": "KQ_FLAG"}


def kq_flag_c(s, i, pct):
    """같은 셋업을 종가에 확인하고 종가에 사는 판본 — 돌파 거래량·강봉까지 보고 산다."""
    e = kq_flag(s, i, pct)
    if e is None:
        return None
    if s["c"][i] <= s["hi10p"][i] or s["v"][i] < s["vol20"][i] or _pos(s, i) < 0.5:
        return None
    return {"entry": s["c"][i], "stop": s["l"][i], "setup": "KQ_FLAG_C"}


def kq_ep(s, i, pct):
    """Kullamägi EP (장전 판정). 갭은 시가에 알 수 있다. 거래량·종가 방향은 조건에 넣지 않는다.
    시가 +1% 를 매수 스탑으로 잡아 시가범위 돌파를 근사한다 — 고가가 닿아야 체결."""
    if np.isnan(s["gap"][i]) or s["gap"][i] < 10:
        return None
    if not np.isnan(s["r6m"][i - 1]) and s["r6m"][i - 1] > 30:          # 쉬었던 종목이어야 EP 다
        return None
    trigger = s["o"][i] * 1.01
    if s["h"][i] < trigger:
        return None
    return {"entry": trigger, "stop": s["l"][i], "setup": "KQ_EP"}


def kq_ep_c(s, i, pct):
    """EP 를 종가에 확인하고 종가 진입 — 갭 + 거래량 2배 + 양봉."""
    if np.isnan(s["gap"][i]) or s["gap"][i] < 10 or s["v"][i] < 2 * s["vol20"][i - 1]:
        return None
    if not np.isnan(s["r6m"][i - 1]) and s["r6m"][i - 1] > 30:
        return None
    if s["c"][i] <= s["o"][i]:
        return None
    return {"entry": s["c"][i], "stop": s["l"][i], "setup": "KQ_EP_C"}


def kq_para_short(s, i, pct):
    """Kullamägi 파라볼릭 숏 — 급등 3연상 뒤 첫 음봉 종가에 숏 (종가 판정·종가 진입).
    한국은 개인 공매도가 사실상 막혀 있어 참고용이다."""
    if i < 6 or np.isnan(s["adv20"][i - 1]):
        return None
    run = (s["c"][i - 1] / s["c"][i - 6] - 1) * 100
    big = s["adv20"][i - 1] > (5e10 if s["market"] == "KR" else 5e8)
    if run < (30 if big else 50):
        return None
    ups = all(s["c"][i - k] > s["c"][i - k - 1] for k in range(1, 4))
    if not ups or not (s["c"][i] < s["o"][i] and s["c"][i] < s["c"][i - 1]):
        return None
    return {"entry": s["c"][i], "stop": s["h"][i - 5:i + 1].max(), "setup": "KQ_PARA_S", "short": True}


def kell_wedge(s, i, pct):
    """Kell 웨지 팝 — 종가 판정·종가 진입이라 오늘 값을 써도 된다."""
    if i < 40 or np.isnan(s["vol30"][i]):
        return None
    if not (s["c"][i] > s["ema10"][i] and s["c"][i] > s["ema20"][i] and s["c"][i] > s["hi5p"][i] and s["v"][i] >= 1.2 * s["vol20"][i]):
        return None
    if not (s["c"][i - 1] < s["ema10"][i - 1] and s["c"][i - 1] < s["ema20"][i - 1]):
        return None
    if s["vol10"][i - 1] >= s["vol30"][i - 1]:
        return None
    hs = s["h"][i - 20:i]
    slope = float((_WXC * (hs - hs.mean())).sum()) / _WXD / hs.mean() * 100      # 최소제곱 기울기 (polyfit 과 같다)
    if slope >= -0.05:
        return None
    return {"entry": s["c"][i], "stop": s["l"][i], "setup": "KELL_WEDGE"}


def kell_cross(s, i, pct):
    """Kell EMA 크로스백 — 종가 판정·종가 진입."""
    if np.isnan(s["ma50"][i]) or not (s["ema10"][i] > s["ema20"][i] > s["ma50"][i]):
        return None
    if s["h"][i - 15:i].max() < s["hi20p"][i]:                          # 15세션 안에 20일 신고가가 있어야 '추세 중 눌림' 이다
        return None
    touched = any(s["l"][j] <= s["ema20"][j] * 1.01 for j in range(i - 3, i))
    if not touched or not (s["c"][i] > s["ema10"][i] and s["c"][i] > s["h"][i - 1]):
        return None
    return {"entry": s["c"][i], "stop": s["l"][i - 3:i + 1].min(), "setup": "KELL_CROSS"}


def kell_base(s, i, pct):
    """Kell 베이스 돌파 (장전 판정·매수 스탑). 베이스와 50일선은 어제까지의 값."""
    if i < 40 or np.isnan(s["ma50"][i - 1]):
        return None
    hi, lo = s["h"][i - 15:i].max(), s["l"][i - 15:i].min()
    if (hi - lo) / lo * 100 > 15 or s["c"][i - 1] < s["ma50"][i - 1]:
        return None
    ent = _buy_stop(s, i, hi)
    return None if ent is None else {"entry": ent, "stop": s["l"][i], "setup": "KELL_BASE"}


def kell_base_c(s, i, pct):
    """베이스 돌파를 종가에 확인하고 종가 진입 — 거래량 1.5배까지 보고 산다."""
    e = kell_base(s, i, pct)
    if e is None:
        return None
    hi = s["h"][i - 15:i].max()
    if s["c"][i] <= hi or s["v"][i] < 1.5 * s["vol20"][i - 1]:
        return None
    return {"entry": s["c"][i], "stop": s["l"][i], "setup": "KELL_BASE_C"}


def mv_trend_template(s, i, pct):
    """Minervini 추세 템플릿 8항목 — 전부 어제 종가까지의 값."""
    c = s["c"][i - 1]
    if np.isnan(s["ma200"][i - 1]) or np.isnan(s["ma200_prev20"][i - 1]) or np.isnan(s["hi52"][i - 1]):
        return False
    return (c > s["ma150"][i - 1] and c > s["ma200"][i - 1] and s["ma150"][i - 1] > s["ma200"][i - 1]
            and s["ma200"][i - 1] > s["ma200_prev20"][i - 1] and s["ma50"][i - 1] > s["ma150"][i - 1]
            and c > s["ma50"][i - 1] and c >= 1.3 * s["lo52"][i - 1] and c >= 0.75 * s["hi52"][i - 1] and pct.get("rs", 0) >= 70)


def _contractions(s, i, look=60):
    """직전 look 세션의 스윙 고점→저점 수축 폭(%) 목록 (시간순). 단순 지그재그: 5세션 국소 고점/저점."""
    h, l = s["h"][i - look:i], s["l"][i - look:i]
    pts = []
    for k in range(2, len(h) - 2):
        if h[k] == h[k - 2:k + 3].max():
            pts.append(("H", k, h[k]))
        elif l[k] == l[k - 2:k + 3].min():
            pts.append(("L", k, l[k]))
    out, last_h = [], None
    for typ, k, val in pts:
        if typ == "H":
            last_h = val
        elif last_h:
            out.append((last_h - val) / last_h * 100); last_h = None
    return out


def _vcp_base(s, i, pct):
    """추세 템플릿 + VCP 수축 + 거래량 고갈 — 어제까지의 값만."""
    if not mv_trend_template(s, i, pct):
        return False
    cons = _contractions(s, i)
    if len(cons) < 2 or not all(cons[k] < cons[k - 1] for k in range(1, len(cons))) or cons[-1] > 10:
        return False
    return s["vol5"][i - 1] <= 0.7 * s["vol50"][i - 1]


def mv_vcp(s, i, pct):
    """Minervini VCP 피벗 돌파 (장전 판정·매수 스탑). 손절은 진입 −7% 와 마지막 수축 저점 중 높은 쪽."""
    if not _vcp_base(s, i, pct):
        return None
    ent = _buy_stop(s, i, s["hi10p"][i])
    if ent is None:
        return None
    return {"entry": ent, "stop": max(ent * 0.93, s["lo10p"][i]), "setup": "MV_VCP"}


def mv_vcp_c(s, i, pct):
    """VCP 돌파를 종가에 확인하고 종가 진입 — 돌파 거래량 1.5배(50일 평균)까지 보고 산다."""
    if not _vcp_base(s, i, pct):
        return None
    if s["c"][i] <= s["hi10p"][i] or s["v"][i] < 1.5 * s["vol50"][i - 1]:
        return None
    ent = s["c"][i]
    return {"entry": ent, "stop": max(ent * 0.93, s["lo10p"][i]), "setup": "MV_VCP_C"}


def kr_close_bet(s, i, pct):
    """국내 종가베팅 — 종가 단일가에 산다. 상한가는 매수가 불가능하므로 제외한다."""
    if s["market"] != "KR" or np.isnan(s["ret"][i]) or not (5 <= s["ret"][i] < LIMIT_UP_KR):
        return None
    if s["c"][i] < s["h"][i] * 0.98 or s["v"][i] < 3 * s["vol20"][i - 1]:      # 거래대금 순위는 거래량 3배 조건과 겹쳐 뺀다
        return None
    return {"entry": s["c"][i], "stop": None, "setup": "KR_CLOSE"}


def kr_limit_up(s, i, pct):
    """국내 상한가 따라잡기 — 상한가에는 종가로 살 수 없으므로 '다음날 시가' 에 산다 (실제로 체결 가능한 유일한 값)."""
    if s["market"] != "KR" or np.isnan(s["ret"][i]) or s["ret"][i] < LIMIT_UP_KR:
        return None
    if i + 1 >= len(s["c"]):
        return None
    return {"entry": s["o"][i + 1], "stop": None, "setup": "KR_LIMIT", "offset": 1}


def kr_pull(s, i, pct):
    """국내 급등 후 첫 눌림목 — 종가 판정·종가 진입."""
    if s["market"] != "KR" or i < 15 or np.isnan(s["ma20"][i]):
        return None
    surge = [j for j in range(i - 10, i) if s["ret"][j] >= 10 and s["v"][j] >= 5 * s["vol20"][j - 1]]
    if not surge:
        return None
    j = surge[-1]
    if not (s["l"][i] <= s["ma5"][i] * 1.01 and s["v"][i] < 0.5 * s["v"][j] and s["c"][i] > s["ma20"][i]
            and s["c"][i] > s["o"][i] and s["c"][i] > s["ma5"][i]):
        return None
    return {"entry": s["c"][i], "stop": s["l"][i - 3:i + 1].min(), "setup": "KR_PULL"}


SETUPS = [kq_flag, kq_flag_c, kq_ep, kq_ep_c, kq_para_short, kell_wedge, kell_cross, kell_base, kell_base_c,
          mv_vcp, mv_vcp_c, kr_close_bet, kr_limit_up, kr_pull]


# ---------------------------------------------------------------------------
# 청산 (일봉). 진입일 i, 다음 세션부터 판정. 반환 (손익 %, 사유, 보유 세션)
# ---------------------------------------------------------------------------
def exit_sim(s, i, e, rule):
    ent, stop, short = e["entry"], e["stop"], e.get("short", False)
    n = len(s["c"])
    sign = -1 if short else 1
    pnl = lambda px: sign * (px - ent) / ent * 100
    if rule == "next_open":
        return (pnl(s["o"][i + 1]), "next_open", 1) if i + 1 < n else None
    if rule == "next_close":
        return (pnl(s["c"][i + 1]), "next_close", 1) if i + 1 < n else None
    R = abs(ent - stop) if stop else ent * 0.05
    realized, frac = 0.0, 1.0
    ma_key = {"kq10": "ma10", "kq20": "ma20", "kell10": "ema10", "kell20": "ema20", "mv21": "ema21", "pull5": "ma5", "para10": "ma10", "hold5": None}[rule]
    max_days = {"kq10": 40, "kq20": 60, "kell10": 40, "kell20": 60, "mv21": 60, "pull5": 10, "para10": 10, "hold5": 5}[rule]
    for k in range(1, max_days + 1):
        j = i + k
        if j >= n:
            return realized + frac * pnl(s["c"][n - 1]), "data_end", k - 1
        o_, h_, l_, c_ = s["o"][j], s["h"][j], s["l"][j], s["c"][j]
        if stop is not None:
            hit = (o_ >= stop or h_ >= stop) if short else (o_ <= stop or l_ <= stop)
            if hit:
                px = (max(o_, stop) if short else min(o_, stop))
                return realized + frac * pnl(px), "stop", k
        # 부분 익절·본전
        if rule in ("kq10", "kq20") and k == 3 and frac == 1.0:
            realized, frac = (1 / 3) * pnl(c_), 2 / 3
            if pnl(c_) > 0:
                stop = ent
        if rule in ("kell10", "kell20") and frac == 1.0 and c_ >= s["ema10"][j] * 1.08:
            realized, frac, stop = 0.5 * pnl(c_), 0.5, max(stop or 0, ent)
        if rule == "mv21":
            if frac == 1.0 and (pnl(c_) >= 20 or (c_ >= s["ema10"][j] * 1.10 and all(s["c"][j - m] > s["c"][j - m - 1] for m in range(3)))):
                realized, frac, stop = 0.5 * pnl(c_), 0.5, max(stop or 0, ent)
            if stop is not None and stop < ent and pnl(h_) >= 2 * R / ent * 100:
                stop = ent
        if rule == "pull5" and pnl(h_) >= 10:
            return realized + frac * 10.0, "target", k
        if ma_key and k >= 2:
            ma_v = s[ma_key][j]
            crossed = (c_ > ma_v) if short else (c_ < ma_v)
            if crossed and not np.isnan(ma_v):
                px = s["o"][j + 1] if j + 1 < n else c_
                return realized + frac * pnl(px), ma_key, k
    j = min(i + max_days, n - 1)
    return realized + frac * pnl(s["c"][j]), "max", max_days


RULES = {"KQ_FLAG": ["kq10", "kq20", "next_open"], "KQ_FLAG_C": ["kq10", "kq20", "next_open"],
         "KQ_EP": ["kq20", "kq10", "next_open"], "KQ_EP_C": ["kq20", "kq10", "next_open"],
         "KQ_PARA_S": ["para10", "next_open"],
         "KELL_WEDGE": ["kell10", "kell20", "next_open"], "KELL_CROSS": ["kell10", "kell20", "next_open"],
         "KELL_BASE": ["kell20", "kell10", "next_open"], "KELL_BASE_C": ["kell20", "kell10", "next_open"],
         "MV_VCP": ["mv21", "kq20", "next_open"], "MV_VCP_C": ["mv21", "kq20", "next_open"],
         "KR_CLOSE": ["next_open", "next_close"], "KR_LIMIT": ["next_open", "next_close"],
         "KR_PULL": ["pull5", "next_open"]}


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------
def agg(v, cost=COST):
    if not v:
        return None
    net = [x - cost for x in v]
    w = [x for x in net if x > 0]; l_ = [x for x in net if x <= 0]
    pf = sum(w) / -sum(l_) if l_ and sum(l_) < 0 else float("inf")
    return {"n": len(net), "wr": round(100 * len(w) / len(net), 1), "avg": round(sum(net) / len(net), 2), "med": round(st.median(net), 2),
            "sum": round(sum(net), 1), "pf": round(pf, 2)}


def fmt(a):
    return "—" if not a else f"{a['avg']:+.2f} (중앙 {a['med']:+.2f}, n={a['n']}, 승률 {a['wr']}%, 손익비 {a['pf']})"


def run(market: str):
    with UNI.open(encoding="utf-8-sig") as f:
        uni = [r for r in csv.DictReader(f) if r["market"] == market]
    syms = {}
    for r in uni:
        d = load_symbol(r["sym"], market)
        if d is not None:
            syms[r["sym"]] = indicators(d)
    names = {r["sym"]: r["name"] for r in uni}
    idx = indicators(load_symbol(IDX[market], market, DATA_DIR / f"{IDX[market]}_1d.json"))
    M = Market(market, syms, idx)
    M.idx_pos = {d: i for i, d in enumerate(idx["dates"])}
    trades = []
    for t, s in syms.items():
        pos = M.pos[t]
        for i, d in enumerate(s["dates"]):
            if d < TEST_FROM or i < 260 or i >= len(s["c"]) - 1:
                continue
            if not M.liquid(s, i - 1):
                continue
            pct = M.pct.get(d, {}).get(t, {})
            for fn in SETUPS:
                e = fn(s, i, pct)
                if not e:
                    continue
                rec = {"sym": t, "name": names.get(t, t), "market": market, "date": d, "setup": e["setup"], "entry": round(e["entry"], 4),
                       "short": e.get("short", False), "is": d < SPLIT, "regime": M.regime(d), "breadth": M.breadth.get(d),
                       "rs": pct.get("rs"), "m1": pct.get("m1"), "pnl": {}, "xs": {}, "hold": {}}
                ei = i + e.get("offset", 0)
                for rule in RULES[e["setup"]]:
                    r = exit_sim(s, ei, e, rule)
                    if r is None:
                        continue
                    p, why, k = r
                    rec["pnl"][rule] = round(p, 3); rec["hold"][rule] = k
                    ir = M.index_ret(d, k)
                    rec["xs"][rule] = round(p - (-ir if rec["short"] else ir), 3)
                trades.append(rec)
    report(market, syms, trades, M)


def report(market, syms, trades, M):
    out = DATA_DIR / "results"; out.mkdir(parents=True, exist_ok=True)
    L = [f"# 시장 전체 일봉 스캔 — {market}  (유니버스 {len(syms)}종목 중 유동성 통과분, {TEST_FROM}~, 표본 내 < {SPLIT} ≤ 표본 외, 비용 왕복 {COST}%)\n"]
    setups = [n for n in RULES if any(t["setup"] == n for t in trades)]
    L.append(f"진입 {len(trades)}건, 셋업별: " + ", ".join(f"{n} {sum(t['setup'] == n for t in trades)}" for n in setups) + "\n")
    L.append("## 셋업 × 청산 — 순 평균 (중앙, n, 승률, 손익비)  표본 내 / 표본 외\n")
    for n in setups:
        L.append(f"\n### {n}")
        g = [t for t in trades if t["setup"] == n]
        for rule in RULES[n]:
            gi = [t["pnl"][rule] for t in g if t["is"] and rule in t["pnl"]]; go = [t["pnl"][rule] for t in g if not t["is"] and rule in t["pnl"]]
            xi = [t["xs"][rule] for t in g if t["is"] and rule in t["xs"]]; xo = [t["xs"][rule] for t in g if not t["is"] and rule in t["xs"]]
            L.append(f"- {rule}: IS {fmt(agg(gi))} / OOS {fmt(agg(go))}")
            ai, ao = agg(xi), agg(xo)
            L.append(f"  · 지수 대비 초과: IS {ai['avg'] if ai else 0:+.2f} (중앙 {ai['med'] if ai else 0:+.2f}) / OOS {ao['avg'] if ao else 0:+.2f} (중앙 {ao['med'] if ao else 0:+.2f})"
                     f" · 보유 중앙 {st.median([t['hold'][rule] for t in g if rule in t['hold']]) if g else 0:.0f}세션")
        r0 = RULES[n][0]
        months = defaultdict(list)
        for t in g:
            if r0 in t["pnl"]:
                months[t["date"][:7]].append(t["pnl"][r0] - COST)
        L.append("  월별(" + r0 + "): " + " ".join(f"{m[5:]}월 {sum(v)/len(v):+.2f}({len(v)})" for m, v in sorted(months.items())))
        for cname, cond in (("국면 강세(지수>10·20EMA)", lambda t: t["regime"]), ("폭 ≥ 50%", lambda t: (t["breadth"] or 0) >= 50),
                            ("RS ≥ 90", lambda t: (t["rs"] or 0) >= 90), ("1개월 상위 2%", lambda t: (t["m1"] or 0) >= 98)):
            on = agg([t["pnl"][r0] for t in g if cond(t) and r0 in t["pnl"]]); off = agg([t["pnl"][r0] for t in g if not cond(t) and r0 in t["pnl"]])
            L.append(f"  {cname}: 켬 {on['avg'] if on else 0:+.2f} (n={on['n'] if on else 0}) / 끔 {off['avg'] if off else 0:+.2f} (n={off['n'] if off else 0})")
        top = sorted([t for t in g if r0 in t["pnl"]], key=lambda t: -t["pnl"][r0])[:3]
        L.append("  최고: " + "; ".join(f"{t['name']}({t['sym']}) {t['date']} {t['pnl'][r0]:+.1f}%" for t in top))
    # 종합
    L.append("\n## 종합 — 롱 셋업 전체 (각 셋업의 대표 청산, 표본 내 / 표본 외)\n")
    allp_i = [t["pnl"][RULES[t["setup"]][0]] for t in trades if not t["short"] and t["is"] and RULES[t["setup"]][0] in t["pnl"]]
    allp_o = [t["pnl"][RULES[t["setup"]][0]] for t in trades if not t["short"] and not t["is"] and RULES[t["setup"]][0] in t["pnl"]]
    allx_i = [t["xs"][RULES[t["setup"]][0]] for t in trades if not t["short"] and t["is"] and RULES[t["setup"]][0] in t["xs"]]
    allx_o = [t["xs"][RULES[t["setup"]][0]] for t in trades if not t["short"] and not t["is"] and RULES[t["setup"]][0] in t["xs"]]
    L.append(f"- 순 손익: IS {fmt(agg(allp_i))} / OOS {fmt(agg(allp_o))}")
    L.append(f"- 지수 대비 초과: IS {fmt(agg(allx_i))} / OOS {fmt(agg(allx_o))}")
    (out / f"scan_report_{market}.md").write_text("\n".join(L), encoding="utf-8")
    (out / f"scan_trades_{market}.json").write_text(json.dumps(trades, ensure_ascii=False), encoding="utf-8")
    print("\n".join(L)); print(f"\n→ {out / f'scan_report_{market}.md'}")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "ALL"
    for m in (["KR", "US"] if arg == "ALL" else [arg]):
        run(m)
