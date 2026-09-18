"""전략 랩 — 투자대회 우승자 셋업·필터·청산 규칙을 같은 1분봉·일봉 데이터로 조합 테스트한다.

구성
  data     : universe.DATA_DIR 의 {심볼}.json(1분봉) · {심볼}_1d.json(일봉) → Sym (정규장 봉 연속 배열 + 세션 구간)
  context  : 일봉 국면(MA·EMA·ADR·수익률·전일), 상대강도 백분위, 지수 국면, 세션 지표(VWAP·RVOL·시가범위)
  setups   : RVOL_BREAKOUT(현재 엔진 진입 근사) · ORB30 · ORB60 · PULLBACK(첫 눌림목) · CLOSE_BET(종가베팅) · GAP_EP(갭 EP)
  exits    : simulate(entry, cfg) — 손절(당일 저가·신호봉 저점·-1%·ATR)·목표(R 배수·%)·보유(당일·N일·EMA10 추적)·부분 익절·넘김 조건
  grid     : 진입×청산 손익을 한 번 계산해 두고 필터 집합은 부분집합 합으로 평가. 표본 내(< SPLIT)로 고르고 표본 외로 판정.
사용: python tools/backtest/lab.py [KR|US|ALL] → DATA_DIR/results/lab_report.md, lab_trades.json
"""
import json
import random
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime
from itertools import product
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from alertbot.timeutil import parse_ts                                          # noqa: E402
from tools.backtest.universe import DATA_DIR, MARKET, NAME, TRADABLE, WATCH     # noqa: E402

TZ = {"US": ZoneInfo("America/New_York"), "KR": ZoneInfo("Asia/Seoul")}
REG = {"KR": (9 * 60, 15 * 60 + 30), "US": (9 * 60 + 30, 16 * 60)}
IDX_MAIN = {"KR": "069500", "US": "SPY"}
COST, BUFFER = 0.35, 0.3          # 왕복 비용 % (수수료·슬리피지) / 매수 지정가 버퍼 %
SPLIT = "2026-07-01"              # 표본 내 < SPLIT <= 표본 외
MIN_IS, MIN_OOS = 40, 30
BAND_MULT = {"KR": 1.0, "US": 0.5}
GAP_EP_MIN = {"KR": 3.0, "US": 4.0}


# ---------------------------------------------------------------------------
# 데이터
# ---------------------------------------------------------------------------
class Sym:
    """정규장 1분봉을 날짜 순 연속 배열로. days[date] = (start, end) 인덱스 구간. daily = [(date, o, h, l, c, v)] 과거→현재."""

    def __init__(self, sym: str, raw: list = None, daily: list = None, market: str = None):
        """raw/daily 를 주면 파일 대신 그것을 쓴다 (테스트)."""
        self.sym, self.market = sym, market or MARKET[sym]
        o, c = REG[self.market]
        if raw is None:
            raw = json.loads((DATA_DIR / f"{sym}.json").read_text(encoding="utf-8"))
        raw = sorted(raw, key=lambda b: b["timestamp"])
        self.dt, self.o, self.h, self.l, self.c, self.v, self.mod = [], [], [], [], [], [], []
        self.days = {}
        for b in raw:
            dt = parse_ts(b["timestamp"], self.market)
            hm = dt.hour * 60 + dt.minute
            if not (o <= hm < c):
                continue
            d = dt.strftime("%Y-%m-%d")
            if d not in self.days:
                self.days[d] = [len(self.dt), len(self.dt)]
            self.days[d][1] = len(self.dt) + 1
            self.dt.append(dt); self.mod.append(hm)
            self.o.append(float(b["openPrice"])); self.h.append(float(b["highPrice"]))
            self.l.append(float(b["lowPrice"])); self.c.append(float(b["closePrice"])); self.v.append(float(b["volume"]))
        self.dates = sorted(self.days)
        self.daily = []
        p1d = DATA_DIR / f"{sym}_1d.json"
        if daily is None and p1d.exists():
            daily = json.loads(p1d.read_text(encoding="utf-8"))
        if daily:
            for b in daily:
                dt = parse_ts(b["timestamp"], self.market)
                self.daily.append((dt.strftime("%Y-%m-%d"), float(b["openPrice"]), float(b["highPrice"]),
                                   float(b["lowPrice"]), float(b["closePrice"]), float(b["volume"])))
            self.daily.sort()
        self.daily_before = {}                  # date -> 그 날짜보다 앞선 일봉 개수
        j = 0
        for d in self.dates:
            while j < len(self.daily) and self.daily[j][0] < d:
                j += 1
            self.daily_before[d] = j
        # 직전 20세션 정규장 거래량 평균 (일봉 거래량은 시간외가 섞여 정규장 누적과 스케일이 다르다)
        self.reg_vol20, hist = {}, []
        for d in self.dates:
            a, b = self.days[d]
            self.reg_vol20[d] = sum(hist[-20:]) / len(hist[-20:]) if hist else None
            hist.append(sum(self.v[a:b]))
        self._series()

    def _series(self):
        """연속 배열 위의 EMA9/20/50, ATR20%(직전 20봉), RVOL(같은 시각 직전 10세션 중앙값, 부족하면 당일 직전 20봉 평균), 세션 VWAP."""
        n = len(self.c)
        self.ema9, self.ema20, self.ema50 = _ema(self.c, 9), _ema(self.c, 20), _ema(self.c, 50)
        self.atr = [0.0] * n                    # 직전 20봉 TR% 평균 (누적합)
        trs = [0.0] * n
        for i in range(1, n):
            trs[i] = max(self.h[i] - self.l[i], abs(self.h[i] - self.c[i - 1]), abs(self.l[i] - self.c[i - 1])) / self.c[i] * 100
        cum = 0.0
        for i in range(n):
            cum += trs[i]
            if i >= 21:
                cum -= trs[i - 20]
                self.atr[i] = cum / 20
        self.rvol, self.vwap, self.cumvol = [None] * n, [0.0] * n, [0.0] * n
        profile = defaultdict(list)
        for d in self.dates:
            s, e = self.days[d]
            pv = vol = 0.0
            for i in range(s, e):
                hist = profile.get(self.mod[i], [])
                if len(hist) >= 3:
                    med = st.median(hist)
                    self.rvol[i] = round(self.v[i] / med, 2) if med > 0 else None
                elif i - s >= 20:
                    avg = sum(self.v[i - 20:i]) / 20
                    self.rvol[i] = round(self.v[i] / avg, 2) if avg > 0 else None
                pv += (self.h[i] + self.l[i] + self.c[i]) / 3 * self.v[i]; vol += self.v[i]
                self.vwap[i] = pv / vol if vol else self.c[i]
                self.cumvol[i] = vol
            for i in range(s, e):
                hist = profile[self.mod[i]]
                hist.append(self.v[i])
                if len(hist) > 10:
                    del hist[0]

    def ctx(self, d: str) -> dict:
        """d 세션의 일봉 국면 — d 보다 앞선 일봉만 쓴다. 자료가 모자란 항목은 None."""
        k = self.daily_before.get(d, 0)
        rows = self.daily[:k]
        closes = [r[4] for r in rows]
        out = {"prev_close": None, "prev_open": None, "prev_ret": None}
        if not rows:
            return out
        out["prev_close"], out["prev_open"] = rows[-1][4], rows[-1][1]
        out["prev_ret"] = (rows[-1][4] - rows[-1][1]) / rows[-1][1] * 100 if rows[-1][1] else None
        for n in (10, 20, 50, 100):
            out[f"ma{n}"] = sum(closes[-n:]) / n if len(closes) >= n else None
        out["ema10"] = _ema(closes, 10)[-1] if len(closes) >= 10 else None
        out["ema20"] = _ema(closes, 20)[-1] if len(closes) >= 20 else None
        adr = [(r[2] - r[3]) / r[4] * 100 for r in rows if r[4]]
        out["adr5"] = sum(adr[-5:]) / 5 if len(adr) >= 5 else None
        out["adr20"] = sum(adr[-20:]) / 20 if len(adr) >= 20 else None
        out["ret5"] = (closes[-1] - closes[-6]) / closes[-6] * 100 if len(closes) >= 6 else None
        out["ret1m"] = (closes[-1] - closes[-22]) / closes[-22] * 100 if len(closes) >= 22 else None
        out["ret3m"] = (closes[-1] - closes[-64]) / closes[-64] * 100 if len(closes) >= 64 else None
        vols = [r[5] for r in rows]
        out["avg_vol20"] = sum(vols[-20:]) / 20 if len(vols) >= 20 else None
        return out


def _ema(values, n):
    out, k = [], 2 / (n + 1)
    e = None
    for v in values:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


# ---------------------------------------------------------------------------
# 셋업 — Entry: sym, date, i(진입 봉 인덱스; 그 봉 시가에 산다), entry, sig_low, day_low, rvol, setup, close_entry
# ---------------------------------------------------------------------------
def _strong(s, i):
    rng = s.h[i] - s.l[i]
    return True if rng <= 0 else (s.c[i] - s.l[i]) / rng >= 0.5


def _entry(s, d, i, setup, sig, extra=None):
    """신호봉 sig 다음 봉 i 의 시가에 진입."""
    st_, _ = s.days[d]
    e = {"sym": s.sym, "market": s.market, "date": d, "i": i, "entry": s.o[i], "sig": sig, "setup": setup,
         "sig_low": s.l[sig], "day_low": min(s.l[st_:i]), "rvol": s.rvol[sig] or 0.0, "mfo": s.mod[i] - REG[s.market][0],
         "close_entry": False, "watch": s.sym in WATCH}
    if extra:
        e.update(extra)
    return e


def setup_rvol_breakout(s: Sym, d: str) -> list:
    """현재 엔진 진입 근사: 직전봉 RVOL < 2 ≤ 신호봉, 강봉, 종가 > VWAP×(1+밴드), EMA 역배열 아님, 개장 10분 뒤. 진입 뒤 60봉 재진입 금지."""
    st_, e_ = s.days[d]
    out, block = [], -1
    for i in range(st_ + 1, e_ - 1):
        if s.mod[i] - REG[s.market][0] < 10 or i < block:
            continue
        pr, cr = s.rvol[i - 1], s.rvol[i]
        if pr is None or cr is None or not (pr < 2.0 <= cr) or not _strong(s, i):
            continue
        band = max(0.15, s.atr[i] * BAND_MULT[s.market])
        if s.c[i] <= s.vwap[i] * (1 + band / 100):
            continue
        if s.ema9[i] < s.ema20[i] < s.ema50[i]:
            continue
        out.append(_entry(s, d, i + 1, "RVOL_BREAKOUT", i, {"ema_ok": s.ema9[i] > s.ema20[i] > s.ema50[i]}))
        block = i + 60
    return out


def setup_orb(s: Sym, d: str, window: int) -> list:
    """시가범위(첫 window 분) 고점을 종가로 처음 넘는 봉(RVOL ≥ 1.5, 종가 > VWAP) 다음 봉 시가 진입. 하루 1건."""
    st_, e_ = s.days[d]
    o = REG[s.market][0]
    rng = [i for i in range(st_, e_) if s.mod[i] - o < window]
    if len(rng) < window // 2:
        return []
    orh = max(s.h[i] for i in rng)
    for i in range(rng[-1] + 1, e_ - 1):
        if s.c[i] > orh and (s.rvol[i] or 0) >= 1.5 and s.c[i] > s.vwap[i]:
            return [_entry(s, d, i + 1, f"ORB{window}", i, {"orh": orh})]
    return []


def setup_pullback(s: Sym, d: str) -> list:
    """RVOL ≥ 3 양봉 급등 뒤 20봉 안에 VWAP(+0.3%) 까지 눌린 다음, 직전 봉 고점을 넘는 양봉(종가 > VWAP)에서 진입. 급등봉당 1건, 하루 2건."""
    st_, e_ = s.days[d]
    out, i = [], st_ + 1
    while i < e_ - 1 and len(out) < 2:
        if s.mod[i] - REG[s.market][0] >= 10 and (s.rvol[i] or 0) >= 3 and s.c[i] > s.o[i] and s.c[i] > s.vwap[i]:
            touched = False
            for j in range(i + 1, min(i + 21, e_ - 1)):
                if not touched and s.l[j] <= s.vwap[j] * 1.003:
                    touched = True
                    continue
                if touched and s.c[j] > s.o[j] and s.c[j] > s.h[j - 1] and s.c[j] > s.vwap[j]:
                    out.append(_entry(s, d, j + 1, "PULLBACK", j, {"surge": i}))
                    i = j + 1
                    break
            else:
                i += 1
                continue
            continue
        i += 1
    return out


def setup_close_bet(s: Sym, d: str, ctx: dict) -> list:
    """마감 10분 전 판정: 당일 등락 ≥ +1.5%, 종가가 당일 고점 −0.5% 안, 정규장 누적 거래량 ≥ 직전 20세션 평균 1.3배 → 종가 매수(진입가 = 마지막 봉 종가)."""
    st_, e_ = s.days[d]
    base = s.reg_vol20.get(d)
    if e_ - st_ < 30 or not ctx.get("prev_close") or not base:
        return []
    k = e_ - 11
    if s.mod[e_ - 1] < REG[s.market][1] - 5:          # 세션이 일찍 끝난 날(조기폐장·결손)은 뺀다
        return []
    close = s.c[k]
    day_ret = (close - ctx["prev_close"]) / ctx["prev_close"] * 100
    hi = max(s.h[st_:k + 1])
    if day_ret >= 1.5 and close >= hi * 0.995 and s.cumvol[k] >= 1.3 * base:
        e = _entry(s, d, e_ - 1, "CLOSE_BET", k)
        e["entry"], e["close_entry"] = s.c[e_ - 1], True
        return [e]
    return []


def setup_gap_ep(s: Sym, d: str, ctx: dict) -> list:
    """갭 ≥ 기준(KR 3%·US 4% — 대형주 유니버스라 Kullamägi 의 10% 는 표본이 없다), 첫 30분 거래량 ≥ 직전 20세션 정규장 평균 × 0.3,
    이후 시가범위(30분) 고점을 종가로 넘는 봉 다음 진입."""
    st_, e_ = s.days[d]
    base = s.reg_vol20.get(d)
    if not ctx.get("prev_close") or not base:
        return []
    gap = (s.o[st_] - ctx["prev_close"]) / ctx["prev_close"] * 100
    if gap < GAP_EP_MIN[s.market]:
        return []
    o = REG[s.market][0]
    rng = [i for i in range(st_, e_) if s.mod[i] - o < 30]
    if len(rng) < 15 or sum(s.v[i] for i in rng) < 0.3 * base:
        return []
    orh = max(s.h[i] for i in rng)
    for i in range(rng[-1] + 1, e_ - 1):
        if s.c[i] > orh and s.c[i] > s.vwap[i]:
            return [_entry(s, d, i + 1, "GAP_EP", i, {"gap": gap, "orh": orh})]
    return []


# ---------------------------------------------------------------------------
# 필터
# ---------------------------------------------------------------------------
def filter_flags(e: dict, ctx: dict, idx_ctx: dict, rs_pct: float) -> dict:
    p, ent = ctx.get("prev_close"), e["entry"]
    ma20, ma50, ma100 = ctx.get("ma20"), ctx.get("ma50"), ctx.get("ma100")
    f = {
        "trend2": bool(p and ma20 and ma50 and p > ma20 > ma50),
        "trend3": bool(p and ma20 and ma50 and ma100 and p > ma20 > ma50 > ma100),
        "ext10_3": bool(ctx.get("ema10")) and ent <= ctx["ema10"] * 1.03,
        "ext10_5": bool(ctx.get("ema10")) and ent <= ctx["ema10"] * 1.05,
        "ext20_5": bool(ctx.get("ema20")) and ent <= ctx["ema20"] * 1.05,
        "contract": bool(ctx.get("adr5") and ctx.get("adr20")) and ctx["adr5"] <= 0.8 * ctx["adr20"],
        "rs_top30": rs_pct is not None and rs_pct >= 70,
        "rs_pos": ctx.get("ret1m") is not None and idx_ctx.get("ret1m") is not None and ctx["ret1m"] > idx_ctx["ret1m"],
        "regime": bool(idx_ctx.get("prev_close") and idx_ctx.get("ema20")) and idx_ctx["prev_close"] > idx_ctx["ema20"],
        "tw60": 10 <= e["mfo"] < 60,
        "tw240": 10 <= e["mfo"] < 240,
        "rv_lo": 2 <= e["rvol"] < 4, "rv_mid": 4 <= e["rvol"] < 8, "rv_hi": e["rvol"] >= 8,
    }
    chase = False
    if p:
        gap = (e["session_open"] - p) / p * 100 if e.get("session_open") else 0
        chase = gap >= 1 or (ent - p) / p * 100 >= 3 or (ctx.get("prev_ret") or 0) >= 2 or (ctx.get("ret5") or 0) >= 8 \
            or (ma20 is not None and (ent - ma20) / ma20 * 100 >= 10)
    f["chase_ok"] = not chase
    return f


# ---------------------------------------------------------------------------
# 청산
# ---------------------------------------------------------------------------
EXITS = {
    # 이름: stop(day_low|sig_low|pct1|atr1|pct5|none), target(none|r1|r2|pct1|pct2), hold(day|d1|d3|d5|ema10|next_open), partial, carry
    "E1 당일저가손절·종가":            dict(stop="day_low", target="none", hold="day"),
    "E2 당일저가·목표2R·종가":         dict(stop="day_low", target="r2", hold="day"),
    "E3 신호봉저점(종가)·+1%":        dict(stop="sig_low", target="pct1", hold="day", close_stop=True),
    "E4 -1%·+1%":                  dict(stop="pct1", target="pct1", hold="day"),
    "E5 당일저가·+2%":               dict(stop="day_low", target="pct2", hold="day"),
    "E6 ATR·목표2R":               dict(stop="atr1", target="r2", hold="day"),
    "E7 당일저가·1일 넘김":            dict(stop="day_low", target="none", hold="d1", carry="always"),
    "E8 당일저가·수익중만 1일":          dict(stop="day_low", target="none", hold="d1", carry="profit"),
    "E9 당일저가·3일·2R절반+본전":       dict(stop="day_low", target="none", hold="d3", partial=True, carry="always"),
    "E10 당일저가·5일·EMA10추적·절반":   dict(stop="day_low", target="none", hold="ema10", days=5, partial=True, carry="always"),
    "E11 당일저가·3일·고점근접만":        dict(stop="day_low", target="none", hold="d3", carry="near_high"),
    "E12 -5%·다음날 시가":            dict(stop="pct5", target="none", hold="next_open"),
    "E13 -5%·다음날 종가":            dict(stop="pct5", target="none", hold="d1", carry="always"),
    "E14 당일저가·다음날 시가":          dict(stop="day_low", target="none", hold="next_open"),
    "E15 -2%·다음날 시가":            dict(stop="pct2", target="none", hold="next_open"),
}


def simulate(s: Sym, e: dict, cfg: dict) -> tuple:
    """진입 뒤 봉을 순회해 (손익 %, 사유). 손절은 봉 저가(close_stop 이면 종가 이탈 → 다음 봉 시가), 목표는 봉 고가, 갭은 시가."""
    ent = e["entry"]
    stop_kind, target_kind, hold = cfg["stop"], cfg["target"], cfg["hold"]
    stop = {"day_low": e["day_low"], "sig_low": e["sig_low"], "pct1": ent * 0.99, "pct2": ent * 0.98, "pct5": ent * 0.95,
            "atr1": ent * (1 - (s.atr[e["i"]] or 0.3) / 100), "none": None}[stop_kind]
    if stop is not None and stop >= ent:
        stop = ent * 0.995
    R = ent - stop if stop else ent * 0.01
    target = {"none": None, "r1": ent + R, "r2": ent + 2 * R, "pct1": ent * 1.01, "pct2": ent * 1.02}[target_kind]
    di = s.dates.index(e["date"])
    max_days = {"day": 0, "d1": 1, "d3": 3, "d5": 5, "next_open": 1, "ema10": cfg.get("days", 5)}[hold]
    seq = []
    if not e["close_entry"]:
        seq.append((e["date"], e["i"], s.days[e["date"]][1]))
    for k in range(1, max_days + 1):
        if di + k < len(s.dates):
            dd = s.dates[di + k]
            seq.append((dd, s.days[dd][0], s.days[dd][1]))
    if not seq:
        return None
    realized, frac, hi_run, pending = 0.0, 1.0, ent, False
    for n, (d, a, b) in enumerate(seq):
        if hold == "next_open" and n == len(seq) - 1 and (n > 0 or e["close_entry"]):
            return realized + frac * (s.o[a] - ent) / ent * 100, "next_open"      # 진입일 손절을 넘겼으면 다음날 시가
        if n > 0 and stop is not None and s.o[a] <= stop:          # 갭 손절
            return realized + frac * (s.o[a] - ent) / ent * 100, "gap_stop"
        for i in range(a, b):
            o_, h_, l_, c_ = s.o[i], s.h[i], s.l[i], s.c[i]
            if pending:
                return realized + frac * (o_ - ent) / ent * 100, "close_stop"
            if stop is not None:
                if cfg.get("close_stop"):
                    if c_ < stop:
                        pending = True
                elif l_ <= stop:
                    px = min(o_, stop)
                    return realized + frac * (px - ent) / ent * 100, "stop"
            if target is not None and h_ >= target:
                return realized + frac * (target - ent) / ent * 100, "target"
            if cfg.get("partial") and frac == 1.0 and h_ >= ent + 2 * R:
                realized, frac, stop = 0.5 * 2 * R / ent * 100, 0.5, ent        # 절반 익절, 나머지 본전
            hi_run = max(hi_run, h_)
        last = s.c[b - 1]
        if pending:
            return realized + frac * (last - ent) / ent * 100, "close_stop"
        if n == len(seq) - 1:
            return realized + frac * (last - ent) / ent * 100, "close"
        if hold == "ema10":
            k = s.daily_before.get(d, 0)
            closes = [r[4] for r in s.daily[:k]] + [last]
            if len(closes) >= 10 and last < _ema(closes, 10)[-1]:
                return realized + frac * (last - ent) / ent * 100, "ema10"
        carry = cfg.get("carry", "always")
        ok = carry == "always" or (carry == "profit" and last > ent) or (carry == "near_high" and last >= max(s.h[a:b]) * 0.995)
        if not ok:
            return realized + frac * (last - ent) / ent * 100, "close"
    return realized + frac * (last - ent) / ent * 100, "close"


# ---------------------------------------------------------------------------
# 격자 · 평가
# ---------------------------------------------------------------------------
FILTER_SETS = [
    (), ("trend2",), ("trend3",), ("regime",), ("trend2", "regime"), ("ext10_5",), ("ext20_5",), ("trend2", "ext10_5"),
    ("trend2", "regime", "ext10_5"), ("contract",), ("trend2", "contract"), ("rs_top30",), ("trend2", "rs_top30"),
    ("rs_pos",), ("chase_ok",), ("trend2", "chase_ok"), ("regime", "chase_ok"), ("tw60",), ("tw240",), ("trend2", "tw240"),
    ("rv_lo",), ("rv_mid",), ("rv_hi",), ("trend2", "regime", "tw240"), ("trend2", "regime", "chase_ok", "tw240"),
    ("regime", "rs_top30", "ext10_5"), ("trend2", "regime", "rs_top30"),
]


def metrics(pnls: list, cost: float = COST) -> dict:
    if not pnls:
        return {"n": 0}
    net = [p - cost for p in pnls]
    wins = [x for x in net if x > 0]; losses = [x for x in net if x <= 0]
    pf = (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else float("inf")
    return {"n": len(net), "wr": round(100 * len(wins) / len(net), 1), "avg": round(sum(net) / len(net), 3),
            "sum": round(sum(net), 1), "pf": round(pf, 2), "med": round(st.median(net), 2)}


def collect(markets: list, with_pnl: bool = True):
    """유니버스를 읽고 셋업 진입 후보를 모은다 → (syms, entries). with_pnl 이면 EXITS 별 손익까지 붙인다."""
    syms = {t: Sym(t) for t in TRADABLE + list({IDX_MAIN[m] for m in markets}) if (DATA_DIR / f"{t}.json").exists()}
    tradable = [t for t in TRADABLE if t in syms and MARKET[t] in markets]
    # 상대강도 백분위: 시장·날짜별 ret1m 순위
    ret1m = defaultdict(dict)
    for t in tradable:
        for d in syms[t].dates:
            r = syms[t].ctx(d).get("ret1m")
            if r is not None:
                ret1m[(MARKET[t], d)][t] = r
    entries = []
    for t in tradable:
        s = syms[t]
        idx = syms.get(IDX_MAIN[s.market])
        for d in s.dates:
            ctx = s.ctx(d)
            ictx = idx.ctx(d) if idx else {}
            peers = ret1m.get((s.market, d), {})
            rs = None
            if t in peers and len(peers) >= 5:
                rs = 100 * sum(1 for v in peers.values() if v < peers[t]) / (len(peers) - 1)
            found = (setup_rvol_breakout(s, d) + setup_orb(s, d, 30) + setup_orb(s, d, 60) + setup_pullback(s, d)
                     + setup_close_bet(s, d, ctx) + setup_gap_ep(s, d, ctx))
            for e in found:
                e["session_open"] = s.o[s.days[d][0]]
                e["flags"] = filter_flags(e, ctx, ictx, rs)
                e["is"] = d < SPLIT
                e["pnl"] = {}
                if with_pnl:
                    for name, cfg in EXITS.items():
                        if e["setup"] == "CLOSE_BET" and cfg["hold"] == "day":
                            continue
                        r = simulate(s, e, cfg)
                        if r is not None:
                            e["pnl"][name] = round(r[0], 3)
                entries.append(e)
    return syms, entries


def run(markets: list):
    random.seed(7)
    syms, entries = collect(markets)
    # 무작위 기준선: 셋업별로 같은 세션·같은 개수의 무작위 봉 진입 (개장 10분 뒤 ~ 마감 30분 전)
    rand = []
    for e in entries:
        if e["close_entry"]:
            continue
        s = syms[e["sym"]]; a, b = s.days[e["date"]]
        lo = next((i for i in range(a, b) if s.mod[i] - REG[s.market][0] >= 10), a + 10)
        if b - 31 <= lo + 1:
            continue
        i = random.randint(lo + 1, b - 31)
        re = dict(e, i=i, entry=s.o[i], sig=i - 1, sig_low=s.l[i - 1], day_low=min(s.l[a:i]), pnl={})
        for name, cfg in EXITS.items():
            if cfg["hold"] == "day" or name.startswith("E7") or name.startswith("E13"):
                r = simulate(s, re, cfg)
                if r is not None:
                    re["pnl"][name] = round(r[0], 3)
        rand.append(re)
    report(entries, rand, markets)


def _sel(entries, setup, market, flags, exit_name, is_=None, watch=None):
    out = []
    for e in entries:
        if e["setup"] != setup or e["market"] != market or exit_name not in e["pnl"]:
            continue
        if is_ is not None and e["is"] != is_:
            continue
        if watch is not None and e["watch"] != watch:
            continue
        if all(e["flags"].get(f) for f in flags):
            out.append(e["pnl"][exit_name])
    return out


def report(entries, rand, markets):
    out_dir = DATA_DIR / "results"; out_dir.mkdir(parents=True, exist_ok=True)
    setups = ["RVOL_BREAKOUT", "ORB30", "ORB60", "PULLBACK", "CLOSE_BET", "GAP_EP"]
    L = [f"# 전략 랩 보고서 — {', '.join(markets)}  (표본 내 < {SPLIT} ≤ 표본 외, 비용 왕복 {COST}%, 순 손익 기준)\n"]
    dates = sorted({e["date"] for e in entries})
    L.append(f"기간 {dates[0]} ~ {dates[-1]}, 종목 {len({e['sym'] for e in entries})}, 진입 후보 {len(entries)}건\n")
    passing = []
    for m in markets:
        L.append(f"\n## {m}\n")
        ref = out_dir / f"result_{m}.json"                       # replay.py 로 재생한 실제 엔진(감시 12종목) — 기준선
        if ref.exists():
            tr = [t for t in json.loads(ref.read_text(encoding="utf-8"))["trades"] if t["opened_at"][:10] >= dates[0]]
            a, b = [t["pnl"] for t in tr if t["opened_at"][:10] < SPLIT], [t["pnl"] for t in tr if t["opened_at"][:10] >= SPLIT]
            L.append(f"실제 엔진 재생(감시 12종목, 현재 코드): IS {metrics(a).get('avg', 0):+.2f} (n={len(a)}) / OOS {metrics(b).get('avg', 0):+.2f} "
                     f"(n={len(b)}, 승률 {metrics(b).get('wr', 0)}%) — 순 손익\n")
        L.append("### 셋업별 진입 수 (표본 내 / 표본 외, 감시 12종목)\n")
        for sp in setups:
            a = [e for e in entries if e["market"] == m and e["setup"] == sp]
            L.append(f"- {sp}: {sum(e['is'] for e in a)} / {sum(not e['is'] for e in a)} (감시 {sum(e['watch'] for e in a)})")
        for sp in setups:
            rows = []
            for fl, ex in product(FILTER_SETS, EXITS):
                is_v, oos_v = _sel(entries, sp, m, fl, ex, True), _sel(entries, sp, m, fl, ex, False)
                if len(is_v) < MIN_IS or len(oos_v) < MIN_OOS:
                    continue
                mi, mo = metrics(is_v), metrics(oos_v)
                months = defaultdict(list)
                for e in entries:
                    if e["setup"] == sp and e["market"] == m and ex in e["pnl"] and all(e["flags"].get(f) for f in fl):
                        months[e["date"][:7]].append(e["pnl"][ex] - COST)
                pos_months = sum(1 for v in months.values() if sum(v) > 0)
                rows.append((mi["avg"], fl, ex, mi, mo, pos_months, len(months)))
            rows.sort(key=lambda r: -r[0])
            L.append(f"\n### {m} · {sp} — 표본 내 순 기대값 상위 12 (표본 외로 판정)\n")
            L.append("| 필터 | 청산 | IS n | IS 승률 | IS 평균 | OOS n | OOS 승률 | OOS 평균 | OOS 손익비 | 양수 월 |")
            L.append("|---|---|---|---|---|---|---|---|---|---|")
            for avg, fl, ex, mi, mo, pm, nm in rows[:12]:
                L.append(f"| {'+'.join(fl) or '없음'} | {ex} | {mi['n']} | {mi['wr']}% | {mi['avg']:+.2f} | {mo['n']} | {mo['wr']}% | "
                         f"{mo['avg']:+.2f} | {mo['pf']} | {pm}/{nm} |")
            for avg, fl, ex, mi, mo, pm, nm in rows:                 # 통과 판정은 표본 내 순위와 무관하게 전 조합에
                if mi["avg"] > 0 and mo["avg"] > 0 and mo["pf"] > 1.2 and mo["n"] >= MIN_OOS:
                    passing.append((m, sp, fl, ex, mi, mo, pm, nm))
            # 청산 규칙별 요약 (필터 없음): 어느 청산이 셋업 자체의 성격에 맞는지
            L.append("\n청산 규칙별 (필터 없음) IS / OOS:")
            for ex in EXITS:
                a, b = _sel(entries, sp, m, (), ex, True), _sel(entries, sp, m, (), ex, False)
                if len(a) >= 10 and len(b) >= 10:
                    L.append(f"- {ex}: IS {metrics(a)['avg']:+.2f} (n={len(a)}) / OOS {metrics(b)['avg']:+.2f} (n={len(b)}, 승률 {metrics(b)['wr']}%)")
            # 표본 외 기준 상위도 참고로 (사후 선택이라 과대평가)
            rows_oos = sorted(rows, key=lambda r: -r[4]["avg"])[:5]
            L.append("\n표본 외 상위 5 (참고, 사후 선택):")
            for avg, fl, ex, mi, mo, pm, nm in rows_oos:
                L.append(f"- {'+'.join(fl) or '없음'} · {ex}: IS {mi['avg']:+.2f} (n={mi['n']}) → OOS {mo['avg']:+.2f} (n={mo['n']}, 승률 {mo['wr']}%, 손익비 {mo['pf']})")
            # 필터 한계 효과 (E1 기준): 필터 하나씩 켰을 때 전체 대비 표본 외 변화
            base_is, base_oos = _sel(entries, sp, m, (), "E1 당일저가손절·종가", True), _sel(entries, sp, m, (), "E1 당일저가손절·종가", False)
            if sp == "CLOSE_BET":
                base_is, base_oos = _sel(entries, sp, m, (), "E13 -5%·다음날 종가", True), _sel(entries, sp, m, (), "E13 -5%·다음날 종가", False)
            ex0 = "E13 -5%·다음날 종가" if sp == "CLOSE_BET" else "E1 당일저가손절·종가"
            if base_is and base_oos:
                L.append(f"\n필터 한계 효과 ({ex0}, 전체 IS {metrics(base_is)['avg']:+.2f} / OOS {metrics(base_oos)['avg']:+.2f}):")
                for f in ("trend2", "trend3", "regime", "ext10_3", "ext10_5", "ext20_5", "contract", "rs_top30", "rs_pos", "chase_ok", "tw60", "tw240", "rv_lo", "rv_mid", "rv_hi"):
                    a, b = _sel(entries, sp, m, (f,), ex0, True), _sel(entries, sp, m, (f,), ex0, False)
                    if len(a) >= 10 and len(b) >= 10:
                        L.append(f"- {f:<9} IS {metrics(a)['avg']:+.2f} (n={len(a)}) / OOS {metrics(b)['avg']:+.2f} (n={len(b)})")
            # 무작위 기준선
            rv = [r["pnl"].get(ex0) for r in rand if r["setup"] == sp and r["market"] == m and not r["is"] and r["pnl"].get(ex0) is not None]
            if rv:
                L.append(f"\n무작위 진입 기준선(같은 세션·개수, {ex0}, 표본 외): {metrics(rv)['avg']:+.2f}% (n={len(rv)}, 승률 {metrics(rv)['wr']}%)")
    L.append("\n\n## 판정 — 통과 조합 (표본 내 평균 > 0, 표본 외 평균 > 0 · 손익비 > 1.2 · n ≥ 30)\n")
    if not passing:
        L.append("없음")
    for m, sp, fl, ex, mi, mo, pm, nm in sorted(passing, key=lambda r: -r[5]["avg"]):
        w = _sel(entries, sp, m, fl, ex, None, True)
        wm = metrics(w) if w else {"n": 0}
        L.append(f"- {m} {sp} [{'+'.join(fl) or '없음'}] {ex}: IS {mi['avg']:+.2f} (n={mi['n']}) / OOS {mo['avg']:+.2f} (n={mo['n']}, 승률 {mo['wr']}%, 손익비 {mo['pf']}), "
                 f"양수 월 {pm}/{nm}, 감시 12종목만 {wm.get('avg', 0):+.2f} (n={wm['n']})")
    L.append(f"\n※ 매수 버퍼 {BUFFER}% 까지 빼면 위 평균에서 {BUFFER}% 를 더 뺀 값이다. 조합 수 {len(FILTER_SETS) * len(EXITS)}/셋업 — 표본 내 상위 선택은 사후 선택 편향이 있으므로 표본 외 열만 믿는다.")
    tag = "_".join(markets)
    (out_dir / f"lab_report_{tag}.md").write_text("\n".join(L), encoding="utf-8")
    slim = [{k: v for k, v in e.items() if k != "flags"} | {"flags": [k for k, v in e["flags"].items() if v]} for e in entries]
    (out_dir / f"lab_trades_{tag}.json").write_text(json.dumps(slim, ensure_ascii=False), encoding="utf-8")
    print("\n".join(L[:12]))
    print(f"... 보고서 {out_dir / f'lab_report_{tag}.md'}, 통과 {len(passing)}건")
    return passing


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "ALL"
    run(["KR", "US"] if arg == "ALL" else [arg])
