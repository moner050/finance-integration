"""엔진 워커 진입점 — 30초 폴링 루프. 주문은 없다.

실행:  python run_engine.py
감시 종목은 MySQL alert_watchlist 에서 읽고, 백오피스가 바꾸면 다음 사이클에 반영된다.
"""

import logging

from alertbot.config import (AUTOTRADE_MODE, CLIENT_ID, CLIENT_SECRET, LOG_PATH, SEED_WATCHLIST,
                             WATCH_HOLDINGS, setup_logging)
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
    if not db.list_watch_rows(store):
        log.info("워치리스트가 비어 있어 초기 종목 %d개를 넣는다", db.seed_watchlist(store, SEED_WATCHLIST))
    watchlist = db.load_watchlist(store)
    if not watchlist:
        log.warning("활성 감시 종목이 없다 — 백오피스에서 종목을 추가하면 다음 사이클에 반영된다")

    cli = TossReadOnlyClient(CLIENT_ID, CLIENT_SECRET)
    log_timestamp_sample(cli, watchlist)
    watch = WATCH_HOLDINGS and cli.load_account()
    if not watch:
        log.info("보유 조회 비활성 — ENTRY 알림만 나온다")
    notifier = Dispatcher(build_channels(), record=lambda s, r: db.log_signal(store, s, r))
    executor = build_executor(store, cli, notifier)
    notifier.send(Signal("SYSTEM", "⚪ 시스템", "감시 시작",
                         f"{', '.join(watchlist) or '(종목 없음)'}
자동매매: {AUTOTRADE_MODE}"))
    SignalEngine(cli, notifier, watch, watchlist, store, executor).run()


def build_executor(store, cli, notifier):
    """AUTOTRADE_MODE 에 따라 실행기를 만든다. off 면 None — 주문 코드가 아예 실행되지 않는다."""
    if AUTOTRADE_MODE == "off":
        log.info("자동매매 비활성(off) — 알림만 보낸다")
        return None
    from alertbot.trading.broker import DryRunBroker, TossOrderClient
    from alertbot.trading.executor import Executor
    broker = TossOrderClient(cli) if AUTOTRADE_MODE == "live" else DryRunBroker()
    return Executor(store, broker, AUTOTRADE_MODE, notifier)


if __name__ == "__main__":
    main()
