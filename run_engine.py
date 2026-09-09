"""엔진 워커 진입점 — 30초 폴링 루프. 주문은 없다.

실행:  python run_engine.py
"""

import logging

from alertbot.config import (CLIENT_ID, CLIENT_SECRET, LOG_PATH, TG_CHATS, TG_TOKEN,
                             WATCH_HOLDINGS, WATCHLIST, setup_logging)
from alertbot.engine import SignalEngine, log_timestamp_sample
from alertbot.notify.dispatcher import Notifier
from alertbot.toss_client import TossReadOnlyClient

log = logging.getLogger("scalper")


def main():
    setup_logging(LOG_PATH)
    if not CLIENT_ID or not CLIENT_SECRET:
        raise SystemExit("토스 인증 정보가 없다. 프로젝트 루트 .env 에 TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 을 넣을 것.")
    cli = TossReadOnlyClient(CLIENT_ID, CLIENT_SECRET)
    log_timestamp_sample(cli, WATCHLIST)
    watch = WATCH_HOLDINGS and cli.load_account()
    if not watch:
        log.info("보유 조회 비활성 — ENTRY 알림만 나온다")
    notifier = Notifier()
    if TG_TOKEN and TG_CHATS:
        log.info("텔레그램 수신자 %d명", len(TG_CHATS))
        notifier.send("⚪ 시스템", "감시 시작", f"{', '.join(WATCHLIST)}")
    else:
        log.info("텔레그램 미설정 — 로그 파일에만 기록된다")
    SignalEngine(cli, notifier, watch, WATCHLIST).run()


if __name__ == "__main__":
    main()
