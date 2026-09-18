"""result_KR.json / result_US.json 집계 — 승률·평균·합계·손익비, 종목·사유·월별, 실매매 근사(비용 반영)."""
import json, sys
from collections import Counter, defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo
from datetime import datetime

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.backtest.universe import DATA_DIR   # noqa: E402

HERE = DATA_DIR / "results"
KST = ZoneInfo("Asia/Seoul")

# 실매매 근사 비용(%, 왕복): 매수 지정가 버퍼 0.3(신호가×1.003 에 체결 가정) + 수수료/세금 + 매도 시장가 슬리피지 0.05
BUFFER, SLIP = 0.3, 0.05
FEE = {"KR": 0.18, "KR_ETF": 0.03, "US": 0.2}      # KR 주식: 수수료 0.015%×2 + 농특세 0.15% / KR ETF: 수수료만 / US: 0.1%×2
ETF_KR = {"114800"}


def cost(t):
    fee = FEE["KR_ETF"] if t["ticker"] in ETF_KR else FEE[t["market"]]
    return BUFFER + fee + SLIP


def stats(rows, key="pnl"):
    if not rows:
        return None
    p = [r[key] for r in rows]
    wins = [x for x in p if x > 0]
    losses = [x for x in p if x <= 0]
    pf = (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else float("inf")
    return {"n": len(p), "win": len(wins), "wr": round(100 * len(wins) / len(p), 1), "avg": round(sum(p) / len(p), 2),
            "sum": round(sum(p), 2), "pf": round(pf, 2), "avg_win": round(sum(wins) / len(wins), 2) if wins else 0,
            "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0, "max": round(max(p), 2), "min": round(min(p), 2),
            "med": round(sorted(p)[len(p) // 2], 2)}


def fmt(s):
    if not s:
        return "—"
    return (f"{s['n']:>4}건 | 승률 {s['wr']:>5}% | 평균 {s['avg']:+.2f}% | 합계 {s['sum']:+.2f}% | 손익비 {s['pf']} | "
            f"평균이익 {s['avg_win']:+.2f} / 평균손실 {s['avg_loss']:+.2f} | 최대 {s['max']:+.2f} / 최소 {s['min']:+.2f}")


TAG = sys.argv[2] if len(sys.argv) > 2 else ""


def load(market):
    p = HERE / f"result_{market}{TAG}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def main():
    since = sys.argv[1] if len(sys.argv) > 1 else "2026-06-15"
    all_trades, out = [], {}
    for m in ("KR", "US"):
        r = load(m)
        if not r:
            continue
        tr = [t for t in r["trades"] if t["opened_at"][:10] >= since]
        for t in tr:
            t["net"] = round(t["pnl"] - cost(t), 2)
            t["fee_only"] = round(t["pnl"] - (cost(t) - BUFFER), 2)
            t["overnight"] = t["opened_at"][:10] != t["closed_at"][:10]
        sig = [s for s in r["signals"] if s["at"][:10] >= since]
        dates = [d for d in r["dates"] if d >= since]
        out[m] = {"trades": tr, "signals": sig, "dates": dates, "open": r["open"]}
        all_trades += tr

    print(f"=== 기간 {since} ~ 2026-09-17 (프로파일 워밍업 세션 제외) ===\n")
    for m, d in out.items():
        tr, sig = d["trades"], d["signals"]
        kinds = Counter(s["kind"] for s in sig)
        print(f"## {m}  세션 {len(d['dates'])}일, 종목 {len(set(t['ticker'] for t in tr) | set(s['symbol'] for s in sig if s['symbol']))}")
        print("  신호 건수:", dict(kinds.most_common()))
        entries = [s for s in sig if s["kind"] == "ENTRY"]
        upgraded = sum(1 for s in entries if "승격" in s["body"])
        print(f"  확정 매수 {len(entries)}건 (대기→승격 {upgraded}), 매수 대기 최초 {sum(1 for s in sig if s['kind']=='ENTRY_WATCH' and '대기 신호가' not in s['body'])}건")
        print("  신호 기준(체결·비용 없음):", fmt(stats(tr)))
        print("  수수료·슬리피지만(버퍼 없음):", fmt(stats(tr, "fee_only")))
        print("  실매매 근사(버퍼·수수료·슬리피지):", fmt(stats(tr, "net")))
        print("  당일 청산만:", fmt(stats([t for t in tr if not t["overnight"]])))
        print("  오버나이트:", fmt(stats([t for t in tr if t["overnight"]])))
        if tr:
            ov = [t for t in tr if t["overnight"]]
            hold = sorted(t["hold_min"] for t in tr)
            print(f"  보유 시간 중앙 {hold[len(hold)//2]:.0f}분, 오버나이트 {len(ov)}건 (그 손익 합 {sum(t['pnl'] for t in ov):+.2f}%)")
            print(f"  세션당 청산 {len(tr)/len(d['dates']):.2f}건")
        print("  종목별:")
        by = defaultdict(list)
        for t in tr:
            by[(t["ticker"], t["label"])].append(t)
        for (tk, lb), rows in sorted(by.items(), key=lambda kv: -sum(x["pnl"] for x in kv[1])):
            print(f"    {lb:<10}({tk}) {fmt(stats(rows))} | 순 {stats(rows,'net')['sum']:+.2f}%")
        print("  청산 사유별:")
        by = defaultdict(list)
        for t in tr:
            by[t["reason"]].append(t)
        for k, rows in sorted(by.items(), key=lambda kv: -len(kv[1])):
            print(f"    {k:<6} {fmt(stats(rows))}")
        print("  월별:")
        by = defaultdict(list)
        for t in tr:
            by[t["opened_at"][:7]].append(t)
        for k, rows in sorted(by.items()):
            print(f"    {k} {fmt(stats(rows))} | 순 {stats(rows,'net')['sum']:+.2f}%")
        if d["open"]:
            print("  아직 열린 신호:", [(o["label"], o["entry"], o["mark"], o["pnl"]) for o in d["open"]])
        print()

    print("## 전체")
    print("  신호 기준:", fmt(stats(all_trades)))
    print("  실매매 근사:", fmt(stats(all_trades, "net")))
    # 누적 곡선 (건당 같은 금액, 단순 합)
    seq = sorted(all_trades, key=lambda t: t["closed_at"])
    cum, peak, mdd = 0.0, 0.0, 0.0
    for t in seq:
        cum += t["net"]
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    print(f"  실매매 근사 누적(단순 합) {cum:+.2f}%, 최대 낙폭 {mdd:+.2f}% (건당 같은 금액)")
    # 만원 단위 예: 건당 100만원 / 1,000달러 (VIRTUAL_AMOUNT)
    krw = sum(t["net"] for t in all_trades if t["market"] == "KR") / 100 * 1_000_000
    usd = sum(t["net"] for t in all_trades if t["market"] == "US") / 100 * 1_000
    print(f"  가상 장부 금액 기준(건당 KR 100만원 / US 1,000달러): KR {krw:+,.0f}원, US {usd:+,.0f}달러")
    (HERE / "trades_all.json").write_text(json.dumps({"since": since, "trades": all_trades, "out": {m: {"dates": d["dates"], "open": d["open"], "signals": Counter(s["kind"] for s in d["signals"])} for m, d in out.items()}}, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
