"""익절 시점 연구 — 랩 셋업의 진입은 고정하고 청산만 바꿔 어느 시점의 익절이 기대값을 높이는지 본다.

  A 경로: 진입 뒤 N분·당일 종가·다음날 시가 수익률과 최대이익(MFE)/최대손실(MAE) 분포
  B 목표×손절 격자(당일 마감)   C 시간 제한   D 추적 손절·본전 이동   E 분할 익절   F 표본 내 상위 → 표본 외   G 셋업·거래량별 최선
사용: python tools/backtest/exit_study.py [KR|US|ALL] → DATA_DIR/results/exit_report_{시장}.md
"""
import statistics as st
import sys
from collections import defaultdict
from itertools import product
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.backtest import lab                                   # noqa: E402
from tools.backtest.universe import DATA_DIR                     # noqa: E402

COST = lab.COST
SETUPS = ("RVOL_BREAKOUT", "ORB30", "ORB60", "PULLBACK")       # 표본이 충분한 셋업만


def path(s: lab.Sym, e: dict) -> dict:
    """진입 봉부터 당일 끝까지의 (o,h,l,c) 와 다음날 시가·종가."""
    a, b = s.days[e["date"]]
    i = e["i"]
    di = s.dates.index(e["date"])
    nxt = s.dates[di + 1] if di + 1 < len(s.dates) else None
    return {"o": s.o[i:b], "h": s.h[i:b], "l": s.l[i:b], "c": s.c[i:b], "atr": s.atr[i] or 0.3,
            "next_open": s.o[s.days[nxt][0]] if nxt else None, "next_close": s.c[s.days[nxt][1] - 1] if nxt else None}


def sim(p: dict, ent: float, stop_pct=None, day_low=None, target=None, tmax=None, trail_on=None, trail=None, trail_atr=None,
        be_after=None, part_at=None, part_frac=0.5, to_next_open=False) -> float:
    """청산 규칙 하나로 손익 %. 손절·추적은 봉 저가, 목표·부분 익절은 봉 고가, 시간 제한은 그 봉 종가.
    stop_pct: 진입 대비 −%. day_low: 절대가. target: +%. tmax: 진입 뒤 봉 수. trail_on/trail: 수익 trail_on% 뒤 고점 −trail% 추적.
    trail_atr: 고점 − k×ATR%. be_after: 수익 x% 뒤 손절을 진입가로. part_at/part_frac: +x% 에 일부 익절, 나머지는 규칙대로.
    to_next_open: 당일 살아남으면 다음날 시가에 청산 (아니면 당일 종가)."""
    stop = None
    if stop_pct is not None:
        stop = ent * (1 - stop_pct / 100)
    if day_low is not None:
        stop = max(stop or 0, day_low) if stop else day_low
    tgt = ent * (1 + target / 100) if target else None
    realized, frac, hi = 0.0, 1.0, ent
    n = len(p["c"])
    for k in range(n):
        o, h, l, c = p["o"][k], p["h"][k], p["l"][k], p["c"][k]
        s_ = stop
        if be_after is not None and hi >= ent * (1 + be_after / 100):
            s_ = max(s_ or 0, ent)
        if trail is not None and hi >= ent * (1 + (trail_on or 0) / 100):
            s_ = max(s_ or 0, hi * (1 - trail / 100))
        if trail_atr is not None and hi > ent:
            s_ = max(s_ or 0, hi * (1 - trail_atr * p["atr"] / 100))
        if s_ is not None and l <= s_ and k > 0:
            px = min(o, s_)
            return realized + frac * (px - ent) / ent * 100
        if part_at is not None and frac == 1.0 and h >= ent * (1 + part_at / 100):
            realized, frac = part_frac * part_at, 1.0 - part_frac
        if tgt is not None and h >= tgt:
            return realized + frac * (tgt - ent) / ent * 100
        hi = max(hi, h)
        if tmax is not None and k + 1 >= tmax:
            return realized + frac * (c - ent) / ent * 100
    last = p["next_open"] if (to_next_open and p["next_open"]) else p["c"][-1]
    return realized + frac * (last - ent) / ent * 100


def agg(v, cost=COST):
    if not v:
        return None
    net = [x - cost for x in v]
    w = sum(1 for x in net if x > 0)
    return {"n": len(net), "wr": round(100 * w / len(net), 1), "avg": round(sum(net) / len(net), 3), "med": round(st.median(net), 2)}


def fmt(a):
    return "—" if not a else f"{a['avg']:+.2f} (n={a['n']}, 승률 {a['wr']}%)"


def run(market: str):
    syms, entries = lab.collect([market], with_pnl=False)
    E = [e for e in entries if e["setup"] in SETUPS]
    for e in E:
        e["p"] = path(syms[e["sym"]], e)
    IS = [e for e in E if e["is"]]; OOS = [e for e in E if not e["is"]]
    L = [f"# 익절 시점 연구 — {market}  (진입 {len(E)}건: {', '.join(SETUPS)}, 표본 내 {len(IS)} / 표본 외 {len(OOS)}, 비용 왕복 {COST}%)\n"]

    # A. 경로
    L.append("## A. 진입 뒤 경로 (비용 전, 진입가 대비 %)\n")
    L.append("| 시점 | 평균 | 중앙 | 양수 비율 |\n|---|---|---|---|")
    for lab_, f in (("5분", lambda e: e["p"]["c"][4] if len(e["p"]["c"]) > 4 else None), ("15분", lambda e: e["p"]["c"][14] if len(e["p"]["c"]) > 14 else None),
                    ("30분", lambda e: e["p"]["c"][29] if len(e["p"]["c"]) > 29 else None), ("60분", lambda e: e["p"]["c"][59] if len(e["p"]["c"]) > 59 else None),
                    ("120분", lambda e: e["p"]["c"][119] if len(e["p"]["c"]) > 119 else None), ("당일 종가", lambda e: e["p"]["c"][-1]),
                    ("다음날 시가", lambda e: e["p"]["next_open"]), ("다음날 종가", lambda e: e["p"]["next_close"])):
        v = [(f(e) - e["entry"]) / e["entry"] * 100 for e in E if f(e)]
        L.append(f"| {lab_} | {sum(v)/len(v):+.2f} | {st.median(v):+.2f} | {100*sum(x>0 for x in v)/len(v):.0f}% |")
    L.append("\n최대이익(MFE)·최대손실(MAE), 진입 뒤 구간별 중앙값 / 평균:\n")
    L.append("| 구간 | MFE 중앙 | MFE 평균 | MAE 중앙 | MAE 평균 | MFE≥0.5% | ≥1% | ≥1.5% | ≥2% | ≥3% |\n|---|---|---|---|---|---|---|---|---|---|")
    for lab_, m in (("30분", 30), ("60분", 60), ("120분", 120), ("당일", 10_000)):
        mfe = [(max(e["p"]["h"][:m]) - e["entry"]) / e["entry"] * 100 for e in E]
        mae = [(min(e["p"]["l"][:m]) - e["entry"]) / e["entry"] * 100 for e in E]
        hits = " | ".join(f"{100*sum(x >= t for x in mfe)/len(mfe):.0f}%" for t in (0.5, 1, 1.5, 2, 3))
        L.append(f"| {lab_} | {st.median(mfe):+.2f} | {sum(mfe)/len(mfe):+.2f} | {st.median(mae):+.2f} | {sum(mae)/len(mae):+.2f} | {hits} |")
    # 목표가에 먼저 닿을 확률 (손절 −1% / 당일 저가 기준)
    L.append("\n목표에 먼저 닿는 비율 (당일 안, 손절보다 먼저): 손절 −1% 기준 / 당일 저가 기준\n")
    L.append("| 목표 | −1% 손절 | 당일 저가 손절 | 손익분기 승률(비용 포함) |\n|---|---|---|---|")
    for t in (0.5, 0.75, 1.0, 1.5, 2.0, 3.0):
        a = sum(1 for e in E if _first_hit(e, t, e["entry"] * 0.99)) / len(E) * 100
        b = sum(1 for e in E if _first_hit(e, t, e["day_low"])) / len(E) * 100
        be = (1 + COST) / (t + 1) * 100      # 목표 t, 손절 −1 일 때 순 기대값 0 이 되는 승률
        L.append(f"| +{t}% | {a:.0f}% | {b:.0f}% | {be:.0f}% |")

    results = []                                                  # (이름, IS agg, OOS agg, 전체 agg)

    def add(name, **kw):
        vi = [sim(e["p"], e["entry"], day_low=e["day_low"] if kw.get("_dl") else None, **{k: v for k, v in kw.items() if k != "_dl"}) for e in IS]
        vo = [sim(e["p"], e["entry"], day_low=e["day_low"] if kw.get("_dl") else None, **{k: v for k, v in kw.items() if k != "_dl"}) for e in OOS]
        results.append((name, agg(vi), agg(vo), agg(vi + vo)))
        return results[-1]

    # B. 목표 × 손절
    L.append("\n## B. 목표가 × 손절 (당일 마감, 순 평균 % · 전체 / 표본 내 / 표본 외)\n")
    stops = [("당일저가", {"_dl": True}), ("−0.5%", {"stop_pct": 0.5}), ("−1%", {"stop_pct": 1.0}), ("−1.5%", {"stop_pct": 1.5}),
             ("−2%", {"stop_pct": 2.0}), ("없음", {})]
    targets = [None, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0]
    L.append("| 손절 \\ 목표 | " + " | ".join("없음" if t is None else f"+{t}%" for t in targets) + " |")
    L.append("|---|" + "---|" * len(targets))
    for sn, sk in stops:
        cells = []
        for t in targets:
            r = add(f"B 목표 {t} · 손절 {sn}", target=t, **sk)
            cells.append(f"{r[3]['avg']:+.2f} ({r[1]['avg']:+.2f}/{r[2]['avg']:+.2f})")
        L.append(f"| {sn} | " + " | ".join(cells) + " |")
    L.append("\n승률 (같은 격자, 전체):\n")
    L.append("| 손절 \\ 목표 | " + " | ".join("없음" if t is None else f"+{t}%" for t in targets) + " |")
    L.append("|---|" + "---|" * len(targets))
    idx = {r[0]: r for r in results}
    for sn, _ in stops:
        L.append(f"| {sn} | " + " | ".join(f"{idx[f'B 목표 {t} · 손절 {sn}'][3]['wr']}%" for t in targets) + " |")

    # C. 시간 제한
    L.append("\n## C. 시간 제한 (손절 당일 저가; 목표 없음 / +1%; 순 평균 전체 (IS/OOS))\n")
    L.append("| 보유 한도 | 목표 없음 | 목표 +1% | 목표 +1.5% |\n|---|---|---|---|")
    for lab_, tm in (("30분", 30), ("60분", 60), ("120분", 120), ("180분", 180), ("당일 종가", None), ("다음날 시가", "next")):
        cells = []
        for t in (None, 1.0, 1.5):
            kw = {"_dl": True, "target": t}
            if tm == "next":
                kw["to_next_open"] = True
            else:
                kw["tmax"] = tm
            r = add(f"C {lab_} · 목표 {t}", **kw)
            cells.append(f"{r[3]['avg']:+.2f} ({r[1]['avg']:+.2f}/{r[2]['avg']:+.2f})")
        L.append(f"| {lab_} | " + " | ".join(cells) + " |")

    # D. 추적 · 본전
    L.append("\n## D. 추적 손절·본전 이동 (손절 당일 저가, 당일 마감)\n")
    L.append("| 규칙 | 전체 | 표본 내 | 표본 외 |\n|---|---|---|---|")
    rules = [("본전 이동: +0.5% 뒤", {"be_after": 0.5}), ("본전 이동: +1% 뒤", {"be_after": 1.0}),
             ("추적 0.3%: +0.5% 뒤", {"trail_on": 0.5, "trail": 0.3}), ("추적 0.5%: +0.5% 뒤", {"trail_on": 0.5, "trail": 0.5}),
             ("추적 0.5%: +1% 뒤", {"trail_on": 1.0, "trail": 0.5}), ("추적 1.0%: +1% 뒤", {"trail_on": 1.0, "trail": 1.0}),
             ("추적 0.5%: 즉시", {"trail_on": 0.0, "trail": 0.5}), ("추적 1.0%: 즉시", {"trail_on": 0.0, "trail": 1.0}),
             ("ATR 추적 1배", {"trail_atr": 1.0}), ("ATR 추적 2배", {"trail_atr": 2.0}), ("ATR 추적 3배", {"trail_atr": 3.0}),
             ("추적 0.5%(+1% 뒤) + 목표 +3%", {"trail_on": 1.0, "trail": 0.5, "target": 3.0}),
             ("본전(+1%) + 목표 +2%", {"be_after": 1.0, "target": 2.0})]
    for name, kw in rules:
        r = add(f"D {name}", _dl=True, **kw)
        L.append(f"| {name} | {fmt(r[3])} | {fmt(r[1])} | {fmt(r[2])} |")

    # E. 분할
    L.append("\n## E. 분할 익절 (절반을 +x% 에, 나머지는 규칙대로; 손절 당일 저가)\n")
    L.append("| 규칙 | 전체 | 표본 내 | 표본 외 |\n|---|---|---|---|")
    for pa in (0.5, 0.7, 1.0, 1.5):
        for rest_name, rest in (("나머지 종가", {}), ("나머지 본전", {"be_after": pa}), ("나머지 추적 0.5%", {"trail_on": pa, "trail": 0.5}),
                                ("나머지 목표 +2%", {"target": 2.0}), ("나머지 다음날 시가", {"to_next_open": True})):
            r = add(f"E 절반 +{pa}% · {rest_name}", _dl=True, part_at=pa, **rest)
            L.append(f"| 절반 +{pa}% · {rest_name} | {fmt(r[3])} | {fmt(r[1])} | {fmt(r[2])} |")

    # F. 표본 내 상위 → 표본 외
    L.append("\n## F. 표본 내 순 평균 상위 10 → 표본 외 (사후 선택 확인)\n")
    L.append("| 규칙 | 표본 내 | 표본 외 |\n|---|---|---|")
    for name, vi, vo, va in sorted(results, key=lambda r: -r[1]["avg"])[:10]:
        L.append(f"| {name} | {fmt(vi)} | {fmt(vo)} |")
    L.append("\n표본 외 상위 10 (참고):\n")
    L.append("| 규칙 | 표본 내 | 표본 외 |\n|---|---|---|")
    for name, vi, vo, va in sorted(results, key=lambda r: -r[2]["avg"])[:10]:
        L.append(f"| {name} | {fmt(vi)} | {fmt(vo)} |")

    # G. 셋업·거래량 구간별 — 대표 규칙 몇 개
    L.append("\n## G. 셋업·거래량 구간별 (순 평균 전체 (IS/OOS))\n")
    reps = [("당일 종가", {"_dl": True}), ("목표 +1%", {"_dl": True, "target": 1.0}), ("목표 +2%", {"_dl": True, "target": 2.0}),
            ("추적 0.5%(+1%뒤)", {"_dl": True, "trail_on": 1.0, "trail": 0.5}), ("절반 +1%·나머지 본전", {"_dl": True, "part_at": 1.0, "be_after": 1.0}),
            ("다음날 시가", {"_dl": True, "to_next_open": True})]
    L.append("| 구간 | n | " + " | ".join(n for n, _ in reps) + " |")
    L.append("|---|---|" + "---|" * len(reps))
    groups = [(sp, lambda e, sp=sp: e["setup"] == sp) for sp in SETUPS]
    groups += [("RVOL 2~4", lambda e: 2 <= e["rvol"] < 4), ("RVOL 4~8", lambda e: 4 <= e["rvol"] < 8), ("RVOL 8+", lambda e: e["rvol"] >= 8),
               ("개장 10~60분", lambda e: 10 <= e["mfo"] < 60), ("60~240분", lambda e: 60 <= e["mfo"] < 240), ("240분+", lambda e: e["mfo"] >= 240),
               ("감시 12종목", lambda e: e["watch"]), ("확장 20종목", lambda e: not e["watch"])]
    for gname, cond in groups:
        g = [e for e in E if cond(e)]
        if len(g) < 30:
            continue
        cells = []
        for _, kw in reps:
            kw2 = {k: v for k, v in kw.items() if k != "_dl"}
            vi = [sim(e["p"], e["entry"], day_low=e["day_low"], **kw2) for e in g if e["is"]]
            vo = [sim(e["p"], e["entry"], day_low=e["day_low"], **kw2) for e in g if not e["is"]]
            a, ai, ao = agg(vi + vo), agg(vi), agg(vo)
            cells.append(f"{a['avg']:+.2f} ({ai['avg'] if ai else 0:+.2f}/{ao['avg'] if ao else 0:+.2f})")
        L.append(f"| {gname} | {len(g)} | " + " | ".join(cells) + " |")

    out = DATA_DIR / "results" / f"exit_report_{market}.md"
    out.write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L))
    print(f"\n→ {out}")


def _first_hit(e, target_pct, stop_px):
    ent, p = e["entry"], e["p"]
    tgt = ent * (1 + target_pct / 100)
    for k in range(len(p["c"])):
        if k > 0 and p["l"][k] <= stop_px:
            return False
        if p["h"][k] >= tgt:
            return True
    return False


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "ALL"
    for m in (["KR", "US"] if arg == "ALL" else [arg]):
        run(m)
