"""python -m alertbot.macro [seed|backfill|poll|status]

    seed      캘린더(FOMC·BOJ·일본 CPI·실적)·시나리오·BOJ 기준금리 시드 (이미 있으면 건너뜀)
    backfill  시드 + 3년치 시계열(Yahoo·FRED·MOF) + FRED 발표 일정 + 오늘 시나리오 기록
    poll      주기와 상관없이 모든 작업을 한 번
    status    시리즈별 행 수·최신일과 작업 결과
"""

import json
import sys

from .. import db
from ..config import setup_logging
from . import store
from .worker import MacroWorker, seed


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "status"
    setup_logging(None)
    d = db.connect()
    if cmd == "seed":
        print(seed(d))
    elif cmd == "backfill":
        print(seed(d))
        print(json.dumps(MacroWorker(d).backfill(), ensure_ascii=False, indent=1))
    elif cmd == "poll":
        print(json.dumps(MacroWorker(d).poll_once(force=True), ensure_ascii=False, indent=1))
    elif cmd == "status":
        for r in store.series_stats(d):
            print(f"{r['series_key']:<18} {r['n']:>5}행  {r['first']} ~ {r['last']}")
        print(json.dumps(store.load_jobs(d), ensure_ascii=False, indent=1))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
