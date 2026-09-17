"""통합 실행 — 엔진(run_engine.py)·Binance 워커(run_binance.py)·백오피스(run_backoffice.py)를 한 번에 띄우고 지킨다.

실행:  python run.py
제어:  다른 콘솔(또는 docker compose exec)에서
         python run.py status
         python run.py start|stop|restart engine|binance|backoffice
       백오피스 관리자 '운영' 화면에서도 같은 일을 한다.
끄기:  Ctrl+C — 워커가 사이클을 마치고 내려간다. 한 번 더 누르면 강제 종료. 콘솔 창을 닫으면 곧바로 강제 종료된다.
죽은 워커는 5·30·120·300초 뒤 다시 켜고, heartbeat 가 끊긴 채 멈춘 워커도 다시 켠다 (alertbot/supervisor.py).
"""

import sys

from alertbot import supervisor

if __name__ == "__main__":
    raise SystemExit(supervisor.main(sys.argv[1:]))
