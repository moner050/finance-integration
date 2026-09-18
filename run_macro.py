"""매크로 수집 워커 진입점 — 백오피스 홈(SOXX 매크로)의 시계열·캘린더를 채운다.

실행:  python run_macro.py   (보통은 python run.py 가 함께 띄우고 지킨다)
처음 한 번:  python -m alertbot.macro backfill   (3년치 — 등급 백분위·스파크라인에 필요)
FRED(미 금리·CPI·발표 일정)는 .env 의 ALERT_FRED_API_KEY(없으면 FRED_API_KEY)가 있어야 돈다. Yahoo(DXY·USD/JPY·SOXX·SPY)·MOF(JGB)는 키가 필요 없다.
알림은 보내지 않는다 — 실패는 로그와 백오피스 '매크로 관리' 화면에만.
"""

import logging

from alertbot import db, lifecycle
from alertbot.config import setup_logging
from alertbot.macro.sources import FRED_API_KEY
from alertbot.macro.worker import INTERVAL_MIN, MacroWorker, seed

log = logging.getLogger("macro")
POLL_SEC = 60


def main():
    setup_logging(None)
    store = db.connect()
    lifecycle.install(heartbeat=lambda: db.touch_service(store, "macro"))
    log.info("매크로 시드: %s", seed(store))
    worker = MacroWorker(store)
    log.info("매크로 수집 시작 — 주기(분) %s · FRED %s", INTERVAL_MIN, "on" if FRED_API_KEY else "off (ALERT_FRED_API_KEY·FRED_API_KEY 없음)")
    while lifecycle.running():
        try:
            worker.poll_once()
        except Exception as e:          # DB 끊김 등 — 다음 사이클에 다시
            log.warning("매크로 사이클 오류: %s", e)
        lifecycle.sleep(POLL_SEC)
    log.info("매크로 수집 종료 — 관리 프로세스의 멈춤 요청")


if __name__ == "__main__":
    main()
