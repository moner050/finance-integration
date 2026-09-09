"""엔진 워커 진입점 — 30초 폴링 루프. 주문은 없다.

실행:  python run_engine.py
"""

import logging

from alertbot.config import CLIENT_ID, CLIENT_SECRET, LOG_PATH, WATCH_HOLDINGS, WATCHLIST, setup_logging
from alertbot import db
from alertbot.engine import SignalEngine, log_timestamp_sample
from alertbot.models import Signal
from alertbot.notify import build_channels
from alertbot.notify.dispatcher import Dispatcher
from alertbot.toss_client import TossReadOnlyClient

log = logging.getLogger("scalper")


def main():
    setup_logging(LOG_PATH)
    if not CLIENT_ID or not CLIENT_SECRET:
        raise SystemExit("토스 인증 정보가 없다. 프로젝트 루트 .env 에 TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 을 넣을 것.")
    store = db.connect()            # MySQL. 못 붙으면 여기서 멈춘다 — 이력·상태 없이 돌리지 않는다
    cli = TossReadOnlyClient(CLIENT_ID, CLIENT_SECRET)
    log_timestamp_sample(cli, WATCHLIST)
    watch = WATCH_HOLDINGS and cli.load_account()
    if not watch:
        log.info("보유 조회 비활성 — ENTRY 알림만 나온다")
    notifier = Dispatcher(build_channels(), record=lambda s, r: db.log_signal(store, s, r))
    notifier.send(Signal("SYSTEM", "⚪ 시스템", "감시 시작", ", ".join(WATCHLIST)))
    SignalEngine(cli, notifier, watch, WATCHLIST).run()


if __name__ == "__main__":
    main()
