"""토스 1분봉·일봉 이력을 심볼별 JSON 으로 내려받는다 (백테스트 데이터).

사용: python tools/backtest/fetch_hist.py [SINCE=2026-04-01] [심볼,심볼,...]
  데이터 폴더: 환경변수 ALERT_BT_DATA (기본 data/backtest). 이미 있는 파일은 건너뛴다.
  1분봉은 200봉/페이지로 SINCE 날짜 전까지 페이징(종목당 1~4분), 일봉은 200개.
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from alertbot.config import CLIENT_ID, CLIENT_SECRET          # noqa: E402
from alertbot.toss_client import TossReadOnlyClient           # noqa: E402
from tools.backtest.universe import ALL_SYMBOLS, DATA_DIR     # noqa: E402


def fetch_minutes(client: TossReadOnlyClient, sym: str, since: str) -> list:
    out, before, fails = [], None, 0
    for _ in range(3000):
        params = {"symbol": sym, "interval": "1m", "count": 200}
        if before:
            params["before"] = before
        data = client._get("/api/v1/candles", params)
        if data is None:                        # 레이트리밋·통신 오류 — 같은 페이지를 기다렸다 다시 받는다 (잘린 파일을 남기지 않는다)
            fails += 1
            if fails > 30:
                raise RuntimeError(f"{sym}: 페이지 조회 30회 연속 실패")
            time.sleep(min(10 * fails, 60))
            continue
        fails = 0
        batch = client._items(data, "candles")
        if not batch:
            break
        out.extend(batch)
        inner = client._unwrap(data)
        before = inner.get("nextBefore") if isinstance(inner, dict) else None
        if not before or min(b["timestamp"] for b in batch)[:10] < since:
            break
    return client._sorted(out)


def fetch_daily(client: TossReadOnlyClient, sym: str, pages: int) -> list:
    """일봉을 200개씩 pages 번 거슬러 받는다 (count 최대 200, before 페이징). 추세 템플릿·52주 위치용."""
    out, before = [], None
    for _ in range(pages):
        params = {"symbol": sym, "interval": "1d", "count": 200}
        if before:
            params["before"] = before
        data = client._get("/api/v1/candles", params)
        batch = client._items(data, "candles")
        if not batch:
            break
        out.extend(batch)
        inner = client._unwrap(data)
        before = inner.get("nextBefore") if isinstance(inner, dict) else None
        if not before:
            break
    return client._sorted(out)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "daily":                 # python fetch_hist.py daily [페이지수=3] — 일봉만 다시 받아 덮어쓴다
        pages = int(sys.argv[2]) if len(sys.argv) > 2 else 3
        client = TossReadOnlyClient(CLIENT_ID, CLIENT_SECRET)
        for sym in ALL_SYMBOLS:
            rows = fetch_daily(client, sym, pages)
            if rows:
                (DATA_DIR / f"{sym}_1d.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
            print(sym, len(rows), rows[0]["timestamp"][:10] if rows else None, flush=True)
        print("DONE")
        return
    since = sys.argv[1] if len(sys.argv) > 1 else "2026-04-01"
    symbols = sys.argv[2].split(",") if len(sys.argv) > 2 else ALL_SYMBOLS
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    client = TossReadOnlyClient(CLIENT_ID, CLIENT_SECRET)
    for sym in symbols:
        p1m, p1d = DATA_DIR / f"{sym}.json", DATA_DIR / f"{sym}_1d.json"
        t0 = time.time()
        if not p1d.exists():
            daily = client.get_candles(sym, interval="1d", count=200)
            if not daily:
                print(sym, "일봉 없음 — 토스 미지원 심볼로 보고 건너뜀", flush=True)
                continue
            p1d.write_text(json.dumps(daily, ensure_ascii=False), encoding="utf-8")
        if p1m.exists():
            print(sym, "1분봉 있음 (건너뜀)", flush=True)
            continue
        bars = fetch_minutes(client, sym, since)
        p1m.write_text(json.dumps(bars, ensure_ascii=False), encoding="utf-8")
        print(sym, len(bars), bars[0]["timestamp"][:16] if bars else None, "->", bars[-1]["timestamp"][:16] if bars else None,
              f"{time.time() - t0:.0f}s", flush=True)
    print("DONE")


if __name__ == "__main__":
    main()
