"""엔진 워커 진입점 — 30초 폴링 루프.

실행:  python run_engine.py   (보통은 python run.py 가 엔진·Binance 워커·백오피스를 함께 띄우고 지킨다)
감시 종목은 MySQL alert_watchlist 에서 읽고, 백오피스가 바꾸면 다음 사이클에 반영된다.
공용 가상 장부는 늘 돈다 — 확정 신호마다 가상 체결해 실계좌와 격리된 모의매매 기록을 쌓는다.
AUTOTRADE_MODE=live 면 계정별 live 실행기도 돈다 (백오피스에서 토스 live 스위치를 켠 계정마다 그 계정 키로 실제 주문).
공용 토스 키(.env)는 시세·캘린더 조회에만 쓰고 계좌는 읽지 않는다.
"""

import logging

from alertbot.config import AUTOTRADE_MODE, CLIENT_ID, CLIENT_SECRET, LOG_PATH, SEED_WATCHLIST, setup_logging
from alertbot import db, lifecycle
from alertbot.engine import SignalEngine, log_timestamp_sample
from alertbot.notify import build_channels
from alertbot.notify.dispatcher import Dispatcher
from alertbot.toss_client import TossReadOnlyClient
from alertbot.trading.broker import DryRunBroker
from alertbot.trading.executor import Executor
from alertbot.trading.live import LiveExecutors

log = logging.getLogger("scalper")


def main():
    setup_logging(LOG_PATH)
    if not CLIENT_ID or not CLIENT_SECRET:
        raise SystemExit("토스 인증 정보가 없다. 프로젝트 루트 .env 에 TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 을 넣을 것.")
    store = db.connect()            # MySQL. 못 붙으면 여기서 멈춘다 — 이력·상태 없이 돌리지 않는다
    lifecycle.install(heartbeat=lambda: db.touch_service(store, "engine"))      # run.py 가 띄웠으면 멈춤 요청·heartbeat
    if not db.list_watch_rows(store):
        log.info("워치리스트가 비어 있어 초기 종목 %d개를 넣는다", db.seed_watchlist(store, SEED_WATCHLIST))
    watchlist = db.load_watchlist(store)
    if not watchlist:
        log.warning("활성 감시 종목이 없다 — 백오피스에서 종목을 추가하면 다음 사이클에 반영된다")

    cli = TossReadOnlyClient(CLIENT_ID, CLIENT_SECRET)
    log_timestamp_sample(cli, watchlist)
    notifier = Dispatcher(build_channels(), record=lambda s, r: db.log_signal(store, s, r))
    executor = Executor(store, DryRunBroker(), "dry", notifier)          # 공용 가상 장부
    live = LiveExecutors(store) if AUTOTRADE_MODE == "live" else None
    # 기동 사실은 공용 채널로 보내지 않는다 — 엔진이 '엔진 시작' 로그(대상 종목·가상 장부·live)를 남긴다
    SignalEngine(cli, notifier, watchlist, store, executor, live).run()


if __name__ == "__main__":
    main()
