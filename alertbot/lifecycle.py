"""워커 수명 — run.py(관리 프로세스)가 띄운 워커를 사이클 사이에서 멈추게 하고, 사이클마다 heartbeat 를 남긴다.

관리 프로세스는 자식의 stdin 파이프를 닫아 멈춤을 알린다(ALERT_SUPERVISED=1 일 때만 감시한다). 콘솔·시그널에 기대지 않아
Windows·Docker·작업 스케줄러에서 똑같이 돌고, 관리 프로세스가 어떻게 죽든 파이프가 닫혀 워커도 스스로 내려간다.
단독 실행(python run_engine.py 등)은 지금처럼 Ctrl+C 로 끈다.
"""

import logging
import os
import threading
import time

log = logging.getLogger("lifecycle")

_stop = threading.Event()
_heartbeat = None


def install(heartbeat=None, on_stop=None):
    """heartbeat 는 sleep 마다 메인 스레드에서 부른다(DB 연결은 스레드 안전이 아니다). on_stop 은 멈춤 요청 때 감시 스레드에서 한 번."""
    global _heartbeat
    _heartbeat = heartbeat
    if os.environ.get("ALERT_SUPERVISED") == "1":
        threading.Thread(target=_watch_stdin, args=(on_stop,), daemon=True, name="lifecycle-stdin").start()


def _watch_stdin(on_stop):
    # sys.stdin 이 아니라 os.read — 버퍼 객체의 잠금을 쥔 채 종료되면 인터프리터가 치명 오류를 낸다
    try:
        while os.read(0, 4096):          # 관리 프로세스는 아무것도 쓰지 않는다 — 파이프가 닫히기(EOF)만 기다린다
            pass
    except OSError:
        pass
    log.info("관리 프로세스의 멈춤 요청 — 이번 사이클을 마치고 끝낸다")
    _stop.set()
    if on_stop is not None:
        on_stop()


def running() -> bool:
    return not _stop.is_set()


def sleep(seconds: float):
    """heartbeat 한 번 + 대기. 멈춤 요청이 오면 1초 안에 돌아온다 (1초씩 자는 것은 단독 실행의 Ctrl+C 를 막지 않기 위해서다)."""
    if _heartbeat is not None:
        try:
            _heartbeat()
        except Exception as e:          # heartbeat 실패가 워커를 멈추면 안 된다
            log.warning("heartbeat 기록 실패: %s", e)
    end = time.monotonic() + seconds
    while not _stop.is_set():
        left = end - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(1.0, left))
