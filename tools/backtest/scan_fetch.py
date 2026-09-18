"""시장 전체 일봉 내려받기 (우승자 스캔용). 재실행하면 이미 받은 심볼은 건너뛴다.

  python tools/backtest/scan_fetch.py 1            # 1차: 유니버스 전 종목 1페이지(200봉)
  python tools/backtest/scan_fetch.py 3 liquid     # 2차: 유동성 필터를 넘은 종목만 3페이지(600봉)로 다시
유니버스: data/backtest/universe/universe.csv (build_universe 로 만든다). 출력: data/backtest/daily/{심볼}.json
"""
import csv
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from alertbot.config import CLIENT_ID, CLIENT_SECRET          # noqa: E402
from alertbot.toss_client import TossReadOnlyClient           # noqa: E402
from tools.backtest.universe import DATA_DIR, daily_path      # noqa: E402
from tools.backtest.fetch_hist import fetch_daily             # noqa: E402

UNI = DATA_DIR / "universe" / "universe.csv"
OUT = DATA_DIR / "daily"
LIQ = {"KR": (1_000, 1_000_000_000), "US": (3.0, 10_000_000)}   # (최저가, 20일 평균 거래대금) — 우승자들의 유동성 하한 근사


def load_universe():
    with UNI.open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def liquid(sym: str, market: str) -> bool:
    p = daily_path(sym)
    if not p.exists():
        return False
    rows = json.loads(p.read_text(encoding="utf-8"))
    if len(rows) < 60:
        return False
    tail = rows[-20:]
    px = float(tail[-1]["closePrice"])
    tv = sum(float(r["closePrice"]) * float(r["volume"]) for r in tail) / len(tail)
    lo_px, lo_tv = LIQ[market]
    return px >= lo_px and tv >= lo_tv


LIQ_FILE = DATA_DIR / "universe" / "liquid.csv"


def build_liquid():
    """유동성 통과 목록을 한 번 계산해 저장한다 (7,600개 JSON 파싱이 느려 매번 하지 않는다)."""
    rows = load_universe()
    keep = [r for r in rows if liquid(r["sym"], r["market"])]
    with LIQ_FILE.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["sym", "market", "name", "exchange"]); w.writeheader(); w.writerows(keep)
    print("유동성 통과", {m: sum(r["market"] == m for r in keep) for m in ("KR", "US")}, "/ 전체", len(rows))
    return keep


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "liquid":
        build_liquid(); return
    pages = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    only_liquid = len(sys.argv) > 2 and sys.argv[2] == "liquid"
    OUT.mkdir(parents=True, exist_ok=True)
    client = TossReadOnlyClient(CLIENT_ID, CLIENT_SECRET)
    rows = load_universe()
    if only_liquid:
        rows = list(csv.DictReader(LIQ_FILE.open(encoding="utf-8-sig"))) if LIQ_FILE.exists() else build_liquid()
    todo = []
    for r in rows:
        p = daily_path(r["sym"])
        if only_liquid:
            if p.exists() and p.stat().st_size > 80_000:          # 이미 600봉 근처 (200봉 ≈ 30KB)
                continue
        elif p.exists():
            continue
        todo.append(r)
    print(f"대상 {len(todo)} / 유니버스 {len(rows)} (pages={pages}, liquid={only_liquid})", flush=True)
    t0, fails = time.time(), 0
    for n, r in enumerate(todo, 1):
        sym = r["sym"]
        try:
            bars = fetch_daily(client, sym, pages)
        except Exception as e:
            print(sym, "오류", e, flush=True); fails += 1
            time.sleep(5)
            continue
        daily_path(sym).write_text(json.dumps(bars, ensure_ascii=False), encoding="utf-8")
        if not bars:
            fails += 1
        if n % 100 == 0:
            el = time.time() - t0
            print(f"{n}/{len(todo)} {el/60:.1f}분 경과, 예상 잔여 {el/n*(len(todo)-n)/60:.0f}분, 빈 응답 {fails}", flush=True)
    print("DONE", len(todo), "빈 응답", fails)


if __name__ == "__main__":
    main()
