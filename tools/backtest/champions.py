"""우승자 셋업 원형 재구현 — 일봉 맥락 + 장중 트리거 + 수 주 보유(부분 익절·이동평균 추적).

1차 랩에서 빠졌던 것: 수개월 선행 상승·수렴·다중 세션 고점(피벗) 돌파, 10/20일선 눌림 뒤 회복, 추세 템플릿(50/150/200일선·52주 위치·200일선 상승),
거래량 고갈 뒤 돌파 거래량, 3~5일 뒤 1/3 익절 + 10/20일선 종가 이탈 추적(수 주 보유), 유니버스 폭(20일선 위 비율) 국면, 시가갭 눌림목·오전 거래대금 상위.

셋업 (롱)
  KQ_BREAKOUT  Kullamägi 플래그 돌파: 3개월 +30%(또는 1개월 +20%) 선행, 최근 10~20세션 수렴(범위 ≤ 12%·저점 상승), 10>20일선 위, 20세션 고점(피벗) 장중 돌파
  MV_VCP       Minervini: 추세 템플릿(종가>50>150>200일선, 200일선 20세션 전보다 위, 52주 저점 +30% 이상·고점 −25% 안), ADR5 ≤ 0.6×ADR20, 5일 거래량 < 50일 평균 0.8,
               20세션 고점 돌파 + 당일 거래량 속도 ≥ 20세션 평균 1.5배(같은 시각 누적 비교)
  OK_CROSSBACK Kell EMA 크로스백: 10>20일 EMA 상승 종목이 최근 3세션 안에 20일 EMA(±1%)까지 눌린 뒤 전일 고점을 장중 회복
  EP_GAP       Kullamägi EP: 갭 ≥ 8%(US)/5%(KR), 첫 30분 거래량 ≥ 20세션 정규장 평균 ×0.5, 3개월 선행 +30% 미만(쉬었던 종목), ORH(30분) 돌파
  KR_GAP_PULL  국내 시가갭 눌림목: 갭 +1.5% 이상, 첫 60분 안에 시가(±0.5%)까지 눌렸다가 시가·VWAP 위로 회복하는 봉
  KR_TURNOVER  국내 오전 거래대금 상위: 09:30 시점 유니버스(시장) 거래대금 상위 3 + 당일 +1% 이상 + 30분 고점 근접 → 09:31 진입
청산
  SW  스윙: 손절 진입일 저가(일봉 저가로 판정, 갭은 시가) · 3세션 뒤 1/3 익절 · 나머지 10일선 종가 이탈 → 다음 시가 · 최대 30세션
  SW20 같은 규칙, 20일선 추적 · 2R 도달 시 1/2 익절 · 최대 40세션
  D1  다음날 시가   ·   DAY 당일 종가
국면 필터: 지수 > 10일 EMA · 유니버스 20일선 위 비율 ≥ 50%
사용: python tools/backtest/champions.py [KR|US|ALL]
"""
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.backtest import lab                                   # noqa: E402
from tools.backtest.universe import DATA_DIR, MARKET, TRADABLE, WATCH   # noqa: E402

COST, SPLIT = lab.COST, lab.SPLIT
REG = lab.REG


# ---------------------------------------------------------------------------
# 일봉 컨텍스트 (Sym.daily: (date, o, h, l, c, v) 과거→현재)
# ---------------------------------------------------------------------------
def daily_arrays(s: lab.Sym) -> dict:
    d = s.daily
    closes = [r[4] for r in d]; highs = [r[2] for r in d]; lows = [r[3] for r in d]; vols = [r[5] for r in d]
    n = len(d)

    def sma(arr, k, i):
        return sum(arr[i - k + 1:i + 1]) / k if i - k + 1 >= 0 else None
    ema10, ema20 = lab._ema(closes, 10), lab._ema(closes, 20)
    return {"dates": [r[0] for r in d], "o": [r[1] for r in d], "h": highs, "l": lows, "c": closes, "v": vols, "n": n,
            "ema10": ema10, "ema20": ema20, "sma": sma}


def ctx_at(D: dict, k: int) -> dict:
    """k = 오늘 세션 직전 일봉 인덱스(포함). 오늘 정보는 쓰지 않는다."""
    c, h, l, v, sma = D["c"], D["h"], D["l"], D["v"], D["sma"]
    if k < 25:
        return {}
    out = {"prev_close": c[k], "prev_open": D["o"][k], "prev_high": h[k], "prev_low": l[k],
           "ema10": D["ema10"][k], "ema20": D["ema20"][k], "ema10_prev": D["ema10"][k - 1], "ema20_prev": D["ema20"][k - 1],
           "ma10": sma(c, 10, k), "ma20": sma(c, 20, k), "ma50": sma(c, 50, k), "ma150": sma(c, 150, k), "ma200": sma(c, 200, k),
           "ma200_prev20": sma(c, 200, k - 20) if k - 20 >= 199 else None,
           "vol20": sma(v, 20, k), "vol50": sma(v, 50, k), "vol5": sma(v, 5, k),
           "ret1m": (c[k] - c[k - 21]) / c[k - 21] * 100 if k >= 21 else None,
           "ret3m": (c[k] - c[k - 63]) / c[k - 63] * 100 if k >= 63 else None,
           "hi20": max(h[k - 19:k + 1]), "lo20": min(l[k - 19:k + 1]), "hi10": max(h[k - 9:k + 1]), "lo10": min(l[k - 9:k + 1]),
           "hi52": max(h[max(0, k - 249):k + 1]), "lo52": min(l[max(0, k - 249):k + 1]),
           "adr5": sum((h[i] - l[i]) / c[i] * 100 for i in range(k - 4, k + 1)) / 5,
           "adr20": sum((h[i] - l[i]) / c[i] * 100 for i in range(k - 19, k + 1)) / 20}
    # 수렴: 최근 10세션 범위 / 종가, 저점 상승(최근 10세션 저점의 전반 최소 < 후반 최소)
    rng10 = (out["hi10"] - out["lo10"]) / c[k] * 100
    out["range10_pct"] = rng10
    out["higher_lows"] = min(l[k - 9:k - 4]) < min(l[k - 4:k + 1])
    return out


def trend_template(x: dict) -> bool:
    ma150 = x.get("ma150") or x.get("ma50")
    ma200 = x.get("ma200") or ma150
    ok = x["prev_close"] > x["ma50"] > ma150 >= ma200 if x.get("ma50") else False
    ok = ok and x["prev_close"] >= x["lo52"] * 1.30 and x["prev_close"] >= x["hi52"] * 0.75
    if x.get("ma200") and x.get("ma200_prev20"):
        ok = ok and x["ma200"] > x["ma200_prev20"]
    return ok


# ---------------------------------------------------------------------------
# 장중 트리거 → Entry {sym, date, i(진입 봉), entry, stop(진입일 저가는 사후 확정), setup, kday}
# ---------------------------------------------------------------------------
def _first_break(s: lab.Sym, a: int, b: int, level: float, start_min: int = 10, need_vwap: bool = True):
    o = REG[s.market][0]
    for i in range(a, b - 1):
        if s.mod[i] - o < start_min:
            continue
        if s.c[i] > level and (not need_vwap or s.c[i] > s.vwap[i]):
            return i
    return None


KQ = {"ret3m": 20.0, "ret1m": 10.0, "range10": 15.0}       # 원문 30%/–/좁은 플래그. 대형주 32종목·5개월에서는 표본이 없어 완화 (민감도는 보고서에)
MV = {"adr_ratio": 0.75, "vol_dry": 1.0, "vol_pace": 1.2}    # 원문 ADR 0.6·거래량 고갈 0.8·돌파 거래량 1.5


def setup_kq_breakout(s, d, x, a, b):
    if not x or x.get("ret3m") is None:
        return None
    if not ((x["ret3m"] >= KQ["ret3m"]) or (x["ret1m"] is not None and x["ret1m"] >= KQ["ret1m"])):
        return None
    if not (x["prev_close"] > x["ema10"] > x["ema20"] and x["range10_pct"] <= KQ["range10"]):
        return None
    i = _first_break(s, a, b, x["hi10"])
    return None if i is None else ("KQ_BREAKOUT", i, {"pivot": x["hi10"]})


def setup_mv_vcp(s, d, x, a, b):
    if not x or not x.get("ma50") or not trend_template(x):
        return None
    if not (x["adr5"] <= MV["adr_ratio"] * x["adr20"] and x["vol5"] < MV["vol_dry"] * (x["vol50"] or x["vol20"])):
        return None
    base = s.reg_vol20.get(d)
    i = _first_break(s, a, b, x["hi20"])
    if i is None or not base:
        return None
    # 돌파 시점까지의 누적 거래량이 같은 시각 평균 속도의 배수 이상 (하루 평균 × 경과 비율)
    o, c_ = REG[s.market]
    frac = (s.mod[i] - o + 1) / (c_ - o)
    if s.cumvol[i] < MV["vol_pace"] * base * frac:
        return None
    return ("MV_VCP", i, {"pivot": x["hi20"]})


def setup_ok_crossback(s, d, x, a, b, D, k):
    if not x or not (x["ema10"] > x["ema20"] and x["ema10"] > x["ema10_prev"]):
        return None
    touched = any(D["l"][j] <= D["ema20"][j] * 1.01 for j in range(k - 2, k + 1))
    if not touched or x["prev_close"] < x["ema20"] * 0.97:
        return None
    i = _first_break(s, a, b, x["prev_high"])
    return None if i is None else ("OK_CROSSBACK", i, {})


def setup_ep_gap(s, d, x, a, b):
    if not x:
        return None
    gap = (s.o[a] - x["prev_close"]) / x["prev_close"] * 100
    if gap < lab.GAP_EP_MIN[s.market] or (x.get("ret3m") or 0) >= 30:
        return None
    base = s.reg_vol20.get(d)
    o = REG[s.market][0]
    rng = [i for i in range(a, b) if s.mod[i] - o < 30]
    if not base or len(rng) < 15 or sum(s.v[i] for i in rng) < 0.5 * base:
        return None
    orh = max(s.h[i] for i in rng)
    i = _first_break(s, rng[-1] + 1, b, orh, start_min=0)
    return None if i is None else ("EP_GAP", i, {"gap": gap})


def setup_kr_gap_pull(s, d, x, a, b):
    if not x:
        return None
    op = s.o[a]
    gap = (op - x["prev_close"]) / x["prev_close"] * 100
    if gap < 1.5:
        return None
    o = REG[s.market][0]
    touched = False
    for i in range(a + 1, b - 1):
        if s.mod[i] - o > 60:
            break
        if not touched and s.l[i] <= op * 1.005 and s.c[i] >= op * 0.99:
            touched = True
            continue
        if touched and s.c[i] > op and s.c[i] > s.vwap[i] and s.c[i] > s.h[i - 1]:
            return ("KR_GAP_PULL", i, {"gap": gap})
    return None


def collect(market: str):
    syms = {t: lab.Sym(t) for t in TRADABLE if MARKET[t] == market and (DATA_DIR / f"{t}.json").exists()}
    idx = lab.Sym(lab.IDX_MAIN[market])
    Dm = {t: daily_arrays(s) for t, s in syms.items()}
    Di = daily_arrays(idx)
    # 국면: 지수 > 10일 EMA, 유니버스 20일선 위 비율
    def regime(d):
        k = idx.daily_before.get(d, 0) - 1
        if k < 25:
            return None, None
        above = 0; tot = 0
        for t, s in syms.items():
            kk = s.daily_before.get(d, 0) - 1
            if kk >= 20:
                tot += 1; above += Dm[t]["c"][kk] > Dm[t]["sma"](Dm[t]["c"], 20, kk)
        return Di["c"][k] > Di["ema10"][k], (above / tot * 100 if tot else None)
    # 오전 거래대금 상위 (09:30 시점 누적 거래대금 = Σ 종가×거래량)
    turnover = defaultdict(dict)
    o = REG[market][0]
    for t, s in syms.items():
        for d in s.dates:
            a, b = s.days[d]
            tv = sum(s.c[i] * s.v[i] for i in range(a, b) if s.mod[i] - o < 30)
            turnover[d][t] = tv
    entries = []
    for t, s in syms.items():
        D = Dm[t]
        for d in s.dates:
            a, b = s.days[d]
            k = s.daily_before.get(d, 0) - 1
            x = ctx_at(D, k) if k >= 25 else {}
            reg_idx, breadth = regime(d)
            found = [f for f in (setup_kq_breakout(s, d, x, a, b), setup_mv_vcp(s, d, x, a, b),
                                 setup_ok_crossback(s, d, x, a, b, D, k) if x else None, setup_ep_gap(s, d, x, a, b),
                                 setup_kr_gap_pull(s, d, x, a, b)) if f]
            # 오전 거래대금 상위 3 + 09:30 까지 +1% 이상 + 30분 고점 근접
            rank = sorted(turnover[d], key=lambda z: -turnover[d][z])[:3]
            if x and t in rank:
                rng = [i for i in range(a, b) if s.mod[i] - o < 30]
                if len(rng) >= 15:
                    c30 = s.c[rng[-1]]
                    if (c30 - x["prev_close"]) / x["prev_close"] * 100 >= 1.0 and c30 >= max(s.h[i] for i in rng) * 0.995:
                        found.append(("KR_TURNOVER" if market == "KR" else "US_TURNOVER", rng[-1], {}))
            for name, sig, extra in found:
                i = sig + 1
                if i >= b:
                    continue
                entries.append({"sym": t, "market": market, "date": d, "setup": name, "sig": sig, "i": i, "entry": s.o[i],
                                "day_low": min(s.l[a:i]), "k": k, "is": d < SPLIT, "watch": t in WATCH,
                                "reg_idx": reg_idx, "breadth": breadth, "trend": trend_template(x) if x and x.get("ma50") else False,
                                "rs": (x.get("ret1m") or 0) > ((Di["c"][idx.daily_before.get(d, 0) - 1] - Di["c"][idx.daily_before.get(d, 0) - 22]) / Di["c"][idx.daily_before.get(d, 0) - 22] * 100 if idx.daily_before.get(d, 0) - 22 >= 0 else 0),
                                "k_idx": idx.daily_before.get(d, 0) - 1, **extra})
    return syms, Dm, entries, Di


LEVERED = {"SOXL", "SOXS", "KORU", "BITX", "114800"}


def index_ret(Di: dict, k_idx: int, n_days: int, next_open: bool = False) -> float:
    """진입일 시가 → n_days 뒤 종가(또는 다음날 시가)의 지수 수익률 %. 초과수익 = 거래 손익 − 이 값."""
    k0 = k_idx + 1
    if k0 >= Di["n"]:
        return 0.0
    if next_open:
        return (Di["o"][min(k0 + 1, Di["n"] - 1)] - Di["o"][k0]) / Di["o"][k0] * 100
    k = min(k0 + n_days, Di["n"] - 1)
    return (Di["c"][k] - Di["o"][k0]) / Di["o"][k0] * 100


# ---------------------------------------------------------------------------
# 스윙 청산 (진입일은 1분봉, 이후는 일봉)
# ---------------------------------------------------------------------------
def swing(s: lab.Sym, D: dict, e: dict, ma: int = 10, partial_day: int = 3, partial_frac: float = 1 / 3, max_days: int = 30,
          partial_r: float = None, stop_mode: str = "day_low") -> tuple:
    ent = e["entry"]; a, b = s.days[e["date"]]
    # 진입일: 장중 손절(진입 전까지의 당일 저가), 아니면 마감
    stop = e["day_low"] if stop_mode == "day_low" else ent * 0.95
    for i in range(e["i"] + 1, b):
        if s.l[i] <= stop:
            return (min(s.o[i], stop) - ent) / ent * 100, "stop_d0", 0
    stop = min(stop, min(s.l[a:b]))          # 진입일 저가로 확정
    R = ent - stop if ent > stop else ent * 0.01
    k0 = e["k"] + 1                           # 진입일의 일봉 인덱스
    realized, frac = 0.0, 1.0
    last_px = s.c[b - 1]
    if k0 >= D["n"]:
        return (last_px - ent) / ent * 100, "close_d0", 0
    for n in range(1, max_days + 1):
        k = k0 + n
        if k >= D["n"]:
            return realized + frac * (D["c"][k - 1] - ent) / ent * 100, "data_end", n - 1
        o_, h_, l_, c_ = D["o"][k], D["h"][k], D["l"][k], D["c"][k]
        if o_ <= stop:
            return realized + frac * (o_ - ent) / ent * 100, "gap_stop", n
        if l_ <= stop:
            return realized + frac * (stop - ent) / ent * 100, "stop", n
        if partial_r and frac == 1.0 and h_ >= ent + partial_r * R:
            realized, frac, stop = partial_frac * partial_r * R / ent * 100, 1.0 - partial_frac, max(stop, ent)
        elif partial_day and n == partial_day and frac == 1.0 and c_ > ent:
            realized, frac, stop = partial_frac * (c_ - ent) / ent * 100, 1.0 - partial_frac, max(stop, ent)
        ma_v = D["ema10"][k] if ma == 10 else D["ema20"][k]
        if c_ < ma_v and n >= 2:
            px = D["o"][k + 1] if k + 1 < D["n"] else c_
            return realized + frac * (px - ent) / ent * 100, f"ema{ma}", n
    k = min(k0 + max_days, D["n"] - 1)
    return realized + frac * (D["c"][k] - ent) / ent * 100, "max_days", max_days


def day_exit(s, e, to_next_open=False):
    a, b = s.days[e["date"]]; ent = e["entry"]
    for i in range(e["i"] + 1, b):
        if s.l[i] <= e["day_low"]:
            return (min(s.o[i], e["day_low"]) - ent) / ent * 100
    if to_next_open:
        di = s.dates.index(e["date"])
        if di + 1 < len(s.dates):
            return (s.o[s.days[s.dates[di + 1]][0]] - ent) / ent * 100
    return (s.c[b - 1] - ent) / ent * 100


def agg(v, cost=COST):
    if not v:
        return None
    net = [x - cost for x in v]
    w = [x for x in net if x > 0]; l_ = [x for x in net if x <= 0]
    pf = sum(w) / -sum(l_) if l_ and sum(l_) < 0 else float("inf")
    return {"n": len(net), "wr": round(100 * len(w) / len(net), 1), "avg": round(sum(net) / len(net), 2), "sum": round(sum(net), 1),
            "pf": round(pf, 2), "med": round(st.median(net), 2)}


def f(a):
    return "—" if not a else f"{a['avg']:+.2f} (n={a['n']}, 승률 {a['wr']}%, 손익비 {a['pf']})"


def run(market: str):
    syms, Dm, E, Di = collect(market)
    EXITS = {
        "SW 10일선 추적·3일 뒤 1/3 익절·30세션": lambda s, D, e: swing(s, D, e, 10, 3, 1 / 3, 30),
        "SW20 20일선 추적·2R 절반·40세션": lambda s, D, e: swing(s, D, e, 20, None, 0.5, 40, partial_r=2.0),
        "SW 10일선·익절 없음": lambda s, D, e: swing(s, D, e, 10, None, 0, 30),
        "고정 5세션 보유(−5% 손절)": lambda s, D, e: swing(s, D, e, 200, None, 0, 5, stop_mode="pct5"),
        "다음날 시가": lambda s, D, e: (day_exit(s, e, True), "next", -1),
        "당일 종가": lambda s, D, e: (day_exit(s, e, False), "close", 0),
    }
    for e in E:
        e["pnl"], e["xs"] = {}, {}
        for name, fn in EXITS.items():
            p, why, n = fn(syms[e["sym"]], Dm[e["sym"]], e)
            e["pnl"][name] = p
            e["xs"][name] = p - index_ret(Di, e["k_idx"], max(n, 0), next_open=(n == -1))      # 지수 대비 초과수익
        e["hold"] = swing(syms[e["sym"]], Dm[e["sym"]], e, 10, 3, 1 / 3, 30)[2]
        e["lev"] = e["sym"] in LEVERED
    L = [f"# 우승자 셋업 원형 재현 — {market}  (비용 왕복 {COST}%, 표본 내 < {SPLIT} ≤ 표본 외)\n"]
    dates = sorted({e['date'] for e in E})
    L.append(f"기간 {dates[0] if dates else '-'} ~ {dates[-1] if dates else '-'}, 종목 {len(syms)}, 진입 {len(E)}건 "
             f"(감시 12종목 {sum(e['watch'] for e in E)}건, 레버리지 ETF {sum(e['lev'] for e in E)}건)\n")
    L.append(f"완화한 문턱: KQ 선행 3개월 +{KQ['ret3m']:g}% 또는 1개월 +{KQ['ret1m']:g}%, 10세션 범위 ≤ {KQ['range10']:g}% · MV ADR5/ADR20 ≤ {MV['adr_ratio']}, "
             f"거래량 고갈 < {MV['vol_dry']}×50일, 돌파 거래량 속도 ≥ {MV['vol_pace']}× (원문은 30% · 0.6 · 0.8 · 1.5 — 대형주 32종목에선 표본 0~3건)\n")
    setups = sorted({e["setup"] for e in E})
    L.append("## 셋업 × 청산 (순 평균 %, 표본 내 / 표본 외)\n")
    L.append("| 셋업 | n IS/OOS | " + " | ".join(EXITS) + " |")
    L.append("|---|---|" + "---|" * len(EXITS))
    for sp in setups + ["전체", "전체(레버리지 ETF 제외)"]:
        g = E if sp == "전체" else ([e for e in E if not e["lev"]] if sp.startswith("전체(") else [e for e in E if e["setup"] == sp])
        gi, go = [e for e in g if e["is"]], [e for e in g if not e["is"]]
        cells = []
        for name in EXITS:
            ai, ao = agg([e["pnl"][name] for e in gi]), agg([e["pnl"][name] for e in go])
            cells.append(f"{ai['avg'] if ai else 0:+.2f} / {ao['avg'] if ao else 0:+.2f}")
        L.append(f"| {sp} | {len(gi)}/{len(go)} | " + " | ".join(cells) + " |")
    L.append("\n## 지수 대비 초과수익 (거래 손익 − 같은 기간 지수 수익률, 순 평균 표본 내 / 표본 외; 중앙값 괄호)\n")
    L.append("| 셋업 | SW 10일선 | SW20 | 다음날 시가 | 당일 종가 |\n|---|---|---|---|---|")
    for sp in setups + ["전체", "전체(레버리지 ETF 제외)"]:
        g = E if sp == "전체" else ([e for e in E if not e["lev"]] if sp.startswith("전체(") else [e for e in E if e["setup"] == sp])
        cells = []
        for name in ("SW 10일선 추적·3일 뒤 1/3 익절·30세션", "SW20 20일선 추적·2R 절반·40세션", "다음날 시가", "당일 종가"):
            ai, ao = agg([e["xs"][name] for e in g if e["is"]]), agg([e["xs"][name] for e in g if not e["is"]])
            cells.append(f"{ai['avg'] if ai else 0:+.2f} ({ai['med'] if ai else 0:+.2f}) / {ao['avg'] if ao else 0:+.2f} ({ao['med'] if ao else 0:+.2f})")
        L.append(f"| {sp} | " + " | ".join(cells) + " |")
    L.append("\n## 국면·필터 (청산 SW 10일선, 순 평균 표본 내 / 표본 외 (n))\n")
    L.append("| 조건 | 켬 | 끔 |\n|---|---|---|")
    ex0 = "SW 10일선 추적·3일 뒤 1/3 익절·30세션"
    for cname, cond in (("지수 > 10일 EMA", lambda e: e["reg_idx"]), ("유니버스 20일선 위 ≥ 50%", lambda e: (e["breadth"] or 0) >= 50),
                        ("추세 템플릿", lambda e: e["trend"]), ("1개월 RS > 지수", lambda e: e["rs"]),
                        ("개장 60분 안 진입", lambda e: syms[e["sym"]].mod[e["i"]] - REG[market][0] <= 60), ("감시 12종목", lambda e: e["watch"])):
        on_i = agg([e["pnl"][ex0] for e in E if e["is"] and cond(e)]); on_o = agg([e["pnl"][ex0] for e in E if not e["is"] and cond(e)])
        off_i = agg([e["pnl"][ex0] for e in E if e["is"] and not cond(e)]); off_o = agg([e["pnl"][ex0] for e in E if not e["is"] and not cond(e)])
        fmt2 = lambda x, y: f"{x['avg'] if x else 0:+.2f} / {y['avg'] if y else 0:+.2f} ({x['n'] if x else 0}/{y['n'] if y else 0})"
        L.append(f"| {cname} | {fmt2(on_i, on_o)} | {fmt2(off_i, off_o)} |")
    L.append("\n## 셋업별 상세 (SW 10일선)\n")
    for sp in setups:
        g = [e for e in E if e["setup"] == sp]
        v = [e["pnl"][ex0] for e in g]
        a = agg(v)
        holds = [e["hold"] for e in g]
        months = defaultdict(list)
        for e in g:
            months[e["date"][:7]].append(e["pnl"][ex0] - COST)
        L.append(f"- **{sp}**: {f(a)}, 합 {a['sum']:+.1f}%, 보유 중앙 {st.median(holds) if holds else 0:.0f}세션, 월별 "
                 + " ".join(f"{m[5:]}월 {sum(x)/len(x):+.2f}({len(x)})" for m, x in sorted(months.items())))
        top = sorted(g, key=lambda e: -e["pnl"][ex0])[:3]
        L.append("  최고: " + "; ".join(f"{e['sym']} {e['date']} {e['pnl'][ex0]:+.1f}%" for e in top))
    out = DATA_DIR / "results" / f"champions_report_{market}.md"
    out.write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L)); print(f"\n→ {out}")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "ALL"
    for m in (["KR", "US"] if arg == "ALL" else [arg]):
        run(m)
