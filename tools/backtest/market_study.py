"""시장별 전략 연구 — 한국·미국을 따로 놓고 (1) 시장에 맞는 청산 아래 필터의 한계 효과·조합, (2) 보유 중 '익절 전 신호' 의 예측력을 본다.

(2) 익절 전 신호: 진입 뒤 처음 신호가 켜진 시점부터 당일 종가(·다음날 시가)까지의 수익률이 음수면 그 신호에 파는 게 맞고,
    0 이거나 양수면 그 신호는 익절 근거가 아니다. 신호가 안 켜진 거래의 잔여 수익률과 비교한다.
사용: python tools/backtest/market_study.py [KR|US|ALL] → DATA_DIR/results/market_report_{시장}.md
"""
import statistics as st
import sys
from collections import defaultdict
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.backtest import lab                                   # noqa: E402
from tools.backtest.exit_study import SETUPS, path, sim          # noqa: E402
from tools.backtest.universe import DATA_DIR                     # noqa: E402

COST = lab.COST
FILTERS = ["trend2", "trend3", "regime", "ext10_3", "ext10_5", "ext20_5", "contract", "rs_top30", "rs_pos", "chase_ok",
           "tw60", "tw240", "rv_lo", "rv_mid", "rv_hi"]
# 시장별 기준 청산: KR 은 다음날 시가(밤사이 갭이 유일한 양수 구간), US 는 당일 종가와 60분 제한
EXIT_SETS = {
    "KR": [("다음날 시가(당일저가 손절)", dict(to_next_open=True)), ("당일 종가(당일저가 손절)", {}), ("목표 +1%", dict(target=1.0))],
    "US": [("당일 종가(당일저가 손절)", {}), ("60분 제한(당일저가 손절)", dict(tmax=60)), ("목표 +1%", dict(target=1.0)),
           ("다음날 시가(당일저가 손절)", dict(to_next_open=True))],
}


def agg(v, cost=COST):
    if not v:
        return None
    net = [x - cost for x in v]
    return {"n": len(net), "wr": round(100 * sum(1 for x in net if x > 0) / len(net), 1), "avg": round(sum(net) / len(net), 3)}


def f2(a):
    return "—" if not a else f"{a['avg']:+.2f} (n={a['n']})"


def rsi_series(closes, period=14):
    out = [None] * len(closes)
    if len(closes) < period + 1:
        return out
    g = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    l_ = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    ag, al = sum(g[:period]) / period, sum(l_[:period]) / period
    out[period] = 100 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(period, len(g)):
        ag, al = (ag * (period - 1) + g[i]) / period, (al * (period - 1) + l_[i]) / period
        out[i + 1] = 100 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def run(market: str):
    syms, entries = lab.collect([market], with_pnl=False)
    E = [e for e in entries if e["setup"] in SETUPS]
    for e in E:
        e["p"] = path(syms[e["sym"]], e)
    L = [f"# 시장별 전략 연구 — {market}  (진입 {len(E)}건, 표본 내 < {lab.SPLIT} ≤ 표본 외, 비용 왕복 {COST}%)\n"]

    # ---------- (1) 필터 ----------
    for ex_name, ex_kw in EXIT_SETS[market]:
        def pnl(e):
            return sim(e["p"], e["entry"], day_low=e["day_low"], **ex_kw)
        for e in E:
            e["_x"] = pnl(e)
        base_is = agg([e["_x"] for e in E if e["is"]]); base_oos = agg([e["_x"] for e in E if not e["is"]])
        L.append(f"\n## 1. 필터 한계 효과 — 청산 {ex_name}  (전체 IS {base_is['avg']:+.2f} / OOS {base_oos['avg']:+.2f})\n")
        L.append("| 필터 | 켬 IS | 켬 OOS | 끔 IS | 끔 OOS | 판정 |\n|---|---|---|---|---|---|")
        good = []
        for f in FILTERS:
            on_is = agg([e["_x"] for e in E if e["is"] and e["flags"].get(f)]); on_oos = agg([e["_x"] for e in E if not e["is"] and e["flags"].get(f)])
            off_is = agg([e["_x"] for e in E if e["is"] and not e["flags"].get(f)]); off_oos = agg([e["_x"] for e in E if not e["is"] and not e["flags"].get(f)])
            if not (on_is and on_oos and off_is and off_oos) or on_is["n"] < 50 or on_oos["n"] < 50:
                continue
            d_is, d_oos = on_is["avg"] - off_is["avg"], on_oos["avg"] - off_oos["avg"]
            verdict = "일관 개선" if d_is > 0.05 and d_oos > 0.05 else ("일관 악화" if d_is < -0.05 and d_oos < -0.05 else "불일치")
            if verdict == "일관 개선":
                good.append(f)
            L.append(f"| {f} | {f2(on_is)} | {f2(on_oos)} | {f2(off_is)} | {f2(off_oos)} | {verdict} |")
        # 두 필터 조합 (양쪽 표본 n ≥ 80)
        rows = []
        for a, b in combinations(FILTERS, 2):
            vi = [e["_x"] for e in E if e["is"] and e["flags"].get(a) and e["flags"].get(b)]
            vo = [e["_x"] for e in E if not e["is"] and e["flags"].get(a) and e["flags"].get(b)]
            if len(vi) >= 80 and len(vo) >= 80:
                ai, ao = agg(vi), agg(vo)
                rows.append((min(ai["avg"], ao["avg"]), a, b, ai, ao))
        rows.sort(key=lambda r: -r[0])
        L.append(f"\n두 필터 조합 — 표본 내·외 중 낮은 쪽이 높은 순 (n ≥ 80):\n")
        L.append("| 조합 | IS | OOS |\n|---|---|---|")
        for _, a, b, ai, ao in rows[:8]:
            L.append(f"| {a}+{b} | {f2(ai)} (승률 {ai['wr']}%) | {f2(ao)} (승률 {ao['wr']}%) |")
        L.append(f"\n일관 개선 필터: {', '.join(good) or '없음'}")

    # ---------- (2) 익절 전 신호 ----------
    L.append("\n## 2. 익절 전 신호 — 보유 중 신호가 처음 켜진 뒤 남은 수익률 (비용 전, %)\n")
    L.append("신호가 켜진 시점 종가 → 당일 종가(· KR 은 다음날 시가) 잔여 수익률. 음수면 '그때 팔아야' 하는 신호, 0 근처면 익절 근거가 아니다.\n")
    sig_names = ["VWAP 이탈(종가<VWAP)", "VWAP 밴드 이탈", "신호봉 저점 이탈(종가)", "거래량 소진(3봉≤정점 40%)", "EMA9<EMA20 데드크로스",
                 "RSI>75 과열", "RSI 70위→하락 전환", "15봉 신고가 없음", "고점 대비 −0.5% 되돌림", "고점 대비 −1% 되돌림",
                 "수익 +1% 도달", "수익 +2% 도달", "진입 60분 경과", "진입 120분 경과", "지수 ETF VWAP 아래"]
    idx = syms.get(lab.IDX_MAIN[market])
    rsi_cache = {}
    rec = {n: {"IS": [], "OOS": [], "IS_no": [], "OOS_no": [], "fire": 0, "IS_ovn": [], "OOS_ovn": []} for n in sig_names}
    rec_at = {n: {"IS": [], "OOS": []} for n in sig_names}        # 신호 시점의 진입 대비 손익 (어느 자리에서 켜지나)
    for e in E:
        s = syms[e["sym"]]
        if e["sym"] not in rsi_cache:
            rsi_cache[e["sym"]] = rsi_series(s.c)
        rsi = rsi_cache[e["sym"]]
        a, b = s.days[e["date"]]
        i0 = e["i"]; ent = e["entry"]
        last = s.c[b - 1]
        nxt_open = e["p"]["next_open"]
        peak = 0.0
        for k in range(a, i0):
            if s.mod[k] - lab.REG[market][0] >= 10 and s.rvol[k]:
                peak = max(peak, s.rvol[k])
        hi = ent; fired = set(); no_high = 0
        idx_a = idx.days.get(e["date"]) if idx else None
        per = "IS" if e["is"] else "OOS"
        for k in range(i0, b):
            c = s.c[k]
            band = max(0.15, s.atr[k] * lab.BAND_MULT[market])
            if s.rvol[k] and s.mod[k] - lab.REG[market][0] >= 10:
                peak = max(peak, s.rvol[k])
            if s.h[k] > hi:
                hi, no_high = s.h[k], 0
            else:
                no_high += 1
            recent = [r for r in s.rvol[max(a, k - 2):k + 1] if r is not None]
            fade = bool(recent) and peak >= 2.5 and sum(recent) / len(recent) <= 0.4 * peak
            r_now, r_prev = rsi[k], rsi[k - 1]
            idx_below = False
            if idx_a:
                ia, ib = idx_a
                j = ia + (k - a)
                if ia <= j < ib and idx.dt[j].strftime("%H:%M") == s.dt[k].strftime("%H:%M"):
                    idx_below = idx.c[j] < idx.vwap[j]
            cond = {
                "VWAP 이탈(종가<VWAP)": c < s.vwap[k], "VWAP 밴드 이탈": c < s.vwap[k] * (1 - band / 100),
                "신호봉 저점 이탈(종가)": c < e["sig_low"], "거래량 소진(3봉≤정점 40%)": fade,
                "EMA9<EMA20 데드크로스": s.ema9[k] < s.ema20[k], "RSI>75 과열": bool(r_now and r_now > 75),
                "RSI 70위→하락 전환": bool(r_now and r_prev and r_prev > 70 and r_now < r_prev),
                "15봉 신고가 없음": no_high >= 15, "고점 대비 −0.5% 되돌림": hi > ent and c <= hi * 0.995,
                "고점 대비 −1% 되돌림": hi > ent and c <= hi * 0.99,
                "수익 +1% 도달": s.h[k] >= ent * 1.01, "수익 +2% 도달": s.h[k] >= ent * 1.02,
                "진입 60분 경과": k - i0 >= 60, "진입 120분 경과": k - i0 >= 120, "지수 ETF VWAP 아래": idx_below,
            }
            for n_, on in cond.items():
                if on and n_ not in fired:
                    fired.add(n_)
                    rec[n_]["fire"] += 1
                    rec[n_][per].append((last - c) / c * 100)
                    rec_at[n_][per].append((c - ent) / ent * 100)
                    if nxt_open:
                        rec[n_][per + "_ovn"].append((nxt_open - c) / c * 100)
        for n_ in sig_names:
            if n_ not in fired:
                rec[n_][per + "_no"].append((last - ent) / ent * 100)
    L.append("| 신호 | 켜진 비율 | 켜질 때 손익(중앙) | 잔여→종가 IS | 잔여→종가 OOS | 잔여→다음날 시가 IS/OOS | 안 켜진 거래 전체 손익 IS/OOS |")
    L.append("|---|---|---|---|---|---|---|")
    m_ = lambda v: f"{sum(v)/len(v):+.2f}" if v else "—"
    for n_ in sig_names:
        r = rec[n_]
        at = rec_at[n_]["IS"] + rec_at[n_]["OOS"]
        L.append(f"| {n_} | {100*r['fire']/len(E):.0f}% | {st.median(at) if at else 0:+.2f} | {m_(r['IS'])} (n={len(r['IS'])}) | {m_(r['OOS'])} (n={len(r['OOS'])}) | "
                 f"{m_(r['IS_ovn'])} / {m_(r['OOS_ovn'])} | {m_(r['IS_no'])} / {m_(r['OOS_no'])} |")

    # 신호를 청산 규칙으로 썼을 때 vs 당일 종가/다음날 시가
    L.append("\n### 신호를 청산으로 쓰면 (손절 당일 저가 + 신호 시 청산, 없으면 종가; 순 평균 IS / OOS)\n")
    L.append("| 청산 신호 | KR: 신호 없으면 다음날 시가 | 신호 없으면 당일 종가 |\n|---|---|---|" if market == "KR"
             else "| 청산 신호 | 신호 없으면 당일 종가 | 신호 없으면 다음날 시가 |\n|---|---|---|")
    base_close = (agg([sim(e["p"], e["entry"], day_low=e["day_low"]) for e in E if e["is"]]), agg([sim(e["p"], e["entry"], day_low=e["day_low"]) for e in E if not e["is"]]))
    base_next = (agg([sim(e["p"], e["entry"], day_low=e["day_low"], to_next_open=True) for e in E if e["is"]]),
                 agg([sim(e["p"], e["entry"], day_low=e["day_low"], to_next_open=True) for e in E if not e["is"]]))
    L.append(f"| (신호 없음 기준) | {f2(base_next[0])} / {f2(base_next[1])} | {f2(base_close[0])} / {f2(base_close[1])} |" if market == "KR"
             else f"| (신호 없음 기준) | {f2(base_close[0])} / {f2(base_close[1])} | {f2(base_next[0])} / {f2(base_next[1])} |")
    for n_ in ["VWAP 이탈(종가<VWAP)", "VWAP 밴드 이탈", "신호봉 저점 이탈(종가)", "거래량 소진(3봉≤정점 40%)", "EMA9<EMA20 데드크로스",
               "고점 대비 −1% 되돌림", "수익 +1% 도달", "지수 ETF VWAP 아래", "15봉 신고가 없음"]:
        res = {}
        for mode in ("next", "close"):
            vals = {"IS": [], "OOS": []}
            for e in E:
                s = syms[e["sym"]]; a, b = s.days[e["date"]]; i0 = e["i"]; ent = e["entry"]
                rsi = rsi_cache[e["sym"]]
                peak = 0.0
                for k in range(a, i0):
                    if s.mod[k] - lab.REG[market][0] >= 10 and s.rvol[k]:
                        peak = max(peak, s.rvol[k])
                hi = ent; no_high = 0; out = None
                idx_a = idx.days.get(e["date"]) if idx else None
                for k in range(i0, b):
                    c = s.c[k]
                    if k > i0 and s.l[k] <= e["day_low"]:
                        out = (min(s.o[k], e["day_low"]) - ent) / ent * 100; break
                    if s.rvol[k] and s.mod[k] - lab.REG[market][0] >= 10:
                        peak = max(peak, s.rvol[k])
                    if s.h[k] > hi:
                        hi, no_high = s.h[k], 0
                    else:
                        no_high += 1
                    band = max(0.15, s.atr[k] * lab.BAND_MULT[market])
                    recent = [r for r in s.rvol[max(a, k - 2):k + 1] if r is not None]
                    idx_below = False
                    if idx_a:
                        j = idx_a[0] + (k - a)
                        if idx_a[0] <= j < idx_a[1]:
                            idx_below = idx.c[j] < idx.vwap[j]
                    on = {"VWAP 이탈(종가<VWAP)": c < s.vwap[k], "VWAP 밴드 이탈": c < s.vwap[k] * (1 - band / 100),
                          "신호봉 저점 이탈(종가)": c < e["sig_low"],
                          "거래량 소진(3봉≤정점 40%)": bool(recent) and peak >= 2.5 and sum(recent) / len(recent) <= 0.4 * peak,
                          "EMA9<EMA20 데드크로스": s.ema9[k] < s.ema20[k], "고점 대비 −1% 되돌림": hi > ent and c <= hi * 0.99,
                          "수익 +1% 도달": s.h[k] >= ent * 1.01, "지수 ETF VWAP 아래": idx_below, "15봉 신고가 없음": no_high >= 15}[n_]
                    if on and k > i0:
                        px = ent * 1.01 if n_ == "수익 +1% 도달" else (s.o[k + 1] if k + 1 < b else c)
                        out = (px - ent) / ent * 100; break
                if out is None:
                    lastpx = e["p"]["next_open"] if (mode == "next" and e["p"]["next_open"]) else s.c[b - 1]
                    out = (lastpx - ent) / ent * 100
                vals["IS" if e["is"] else "OOS"].append(out)
            res[mode] = (agg(vals["IS"]), agg(vals["OOS"]))
        first, second = (res["next"], res["close"]) if market == "KR" else (res["close"], res["next"])
        L.append(f"| {n_} | {f2(first[0])} / {f2(first[1])} | {f2(second[0])} / {f2(second[1])} |")

    out = DATA_DIR / "results" / f"market_report_{market}.md"
    out.write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L))
    print(f"\n→ {out}")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "ALL"
    for m in (["KR", "US"] if arg == "ALL" else [arg]):
        run(m)
