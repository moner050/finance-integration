"""관리 프로세스 — python run.py 하나로 엔진·Binance 워커·백오피스를 띄우고 지킨다.

- 자식은 각자의 run_*.py 를 그대로 실행한다. 새 프로세스 그룹(Windows)·세션(POSIX)이라 콘솔 Ctrl+C 는 여기만 받는다
  — 주문을 내는 도중의 워커에 KeyboardInterrupt 가 떨어지지 않는다.
- 자식 출력은 파이프로 받아 [이름] 을 붙여 찍는다. 콘솔이 막혀도(Windows 콘솔 선택 모드) 줄을 버릴 뿐 자식·관리 루프는 멈추지 않는다
  (2026-09-16 Binance 워커가 콘솔 쓰기에서 6시간 멈춘 사고).
- 끄기 = 자식 stdin 을 닫는다. alertbot/lifecycle.py 가 사이클 사이에서 끝내고, 유예가 지나도 살아 있으면 kill.
- 비정상 종료는 5·30·120·300초 뒤 다시 켠다. heartbeat 가 끊긴 채 살아 있는 워커(멈춤)도 다시 켠다.
  그 알림은 공개 채널로 보내지 않고 로그·신호 이력·운영 화면에만 남긴다.
- 제어: 백오피스 '운영' 화면·python run.py start|stop|restart <서비스> 가 alert_services.request 를 쓰면 몇 초 안에 처리한다.
"""

import logging
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from . import db
from .config import BASE_DIR, DATA_DIR
from .models import Signal

log = logging.getLogger("supervisor")

SERVICES = {"engine": "run_engine.py", "binance": "run_binance.py", "backoffice": "run_backoffice.py"}
ACTIONS = ("start", "stop", "restart")
GRACE_SEC = {"engine": 120, "binance": 60, "backoffice": 15}   # 멈춤 요청 뒤 kill 까지 — 주문 도중 끊기지 않게 한 사이클보다 넉넉히
STALE_SEC = {"engine": 900, "binance": 600}                    # heartbeat 가 이만큼 끊기면 멈춘 것으로 본다 (백오피스는 보지 않는다)
BACKOFF_SEC = (5, 30, 120, 300)
HEALTHY_SEC = 300          # 이만큼 돌다 죽었으면 연속 실패를 처음부터 센다
TICK_SEC = 1
SYNC_SEC = 5               # 요청·감시견·관리 프로세스 heartbeat 주기
LEASE_SEC = 30             # 관리 프로세스 heartbeat 가 이 안이면 다른 run.py 가 돌고 있는 것
GAP_SEC = 60               # 틱 사이가 이보다 길면 PC 절전 복귀 — 감시견 기준 시각을 다시 잡는다
TAIL_LINES = 30
ALERT_LINES = 5
QUEUE_MAX = 2000
USAGE = "사용법: python run.py                 전부 실행\n" \
        "        python run.py status          상태\n" \
        "        python run.py start|stop|restart engine|binance|backoffice"


def iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="microseconds")


def parse(value):
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


class Output:
    """자식·관리 프로세스 출력 → 콘솔. 넣는 쪽은 절대 막히지 않는다 — 콘솔이 밀리면 버리고 개수만 센다."""

    def __init__(self, maxsize: int = QUEUE_MAX):
        self.q = queue.Queue(maxsize)
        self.dropped = 0

    def put(self, line: str):
        try:
            self.q.put_nowait(line)
        except queue.Full:
            self.dropped += 1

    def run_printer(self):
        shown = 0
        while True:
            line = self.q.get()
            if self.dropped != shown:
                n, shown = self.dropped - shown, self.dropped
                self._print(f"(출력 {n}줄 생략 — 콘솔이 밀렸다)")
            self._print(line)

    @staticmethod
    def _print(line: str):
        try:
            print(line, flush=True)
        except (OSError, ValueError):
            pass


class QueueHandler(logging.Handler):
    """관리 프로세스 로그 → 출력 큐 (콘솔에 직접 쓰지 않는다)."""

    def __init__(self, out: Output):
        super().__init__()
        self.out = out

    def emit(self, record):
        try:
            self.out.put(self.format(record))
        except Exception:
            pass


def spawn(script: str) -> subprocess.Popen:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1", "ALERT_SUPERVISED": "1"}
    kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    return subprocess.Popen([sys.executable, "-u", script], cwd=BASE_DIR, env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw)


def pump(name: str, stream, tail: deque, out: Output):
    """자식 출력 한 줄씩 → 꼬리 + 출력 큐. 여기서 죽으면 파이프가 차서 자식이 막히므로 어떤 예외도 삼킨다."""
    while True:
        try:
            raw = stream.readline()
        except Exception:
            return
        if not raw:
            return
        try:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            tail.append(line)
            out.put(f"[{name}] {line}")
        except Exception:
            pass


class Child:
    def __init__(self, name: str):
        self.name = name
        self.proc = None
        self.state = "stopped"          # running | stopping | stopped | backoff
        self.wanted = True              # stop 요청으로 꺼 두면 False — 자동 재시작하지 않는다
        self.started_at = None
        self.stop_deadline = None
        self.restart_after_stop = False
        self.fails = 0
        self.next_start = None
        self.restarts = 0
        self.watch_from = None
        self.tail = deque(maxlen=TAIL_LINES)


class Supervisor:
    def __init__(self, store, notifier, spawn=spawn, out: Output = None, host: str = None, pid: int = None, clock=None):
        self.store, self.notify, self.spawn = store, notifier, spawn
        self.out = out or Output()
        self.host, self.pid = host or socket.gethostname(), pid or os.getpid()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.children = {name: Child(name) for name in SERVICES}
        self.last_tick = self.last_sync = None

    # -- 기동·종료 ----------------------------------------------------------------
    def boot(self, now: datetime):
        db.ensure_services(self.store, [*SERVICES, "supervisor"])
        for name in SERVICES:                       # 꺼져 있던 동안 쌓인 요청은 버린다 — run.py 는 늘 셋 다 켠다
            db.update_service(self.store, name, state="stopped", pid=None, request=None, restarts=0)
        db.update_service(self.store, "supervisor", state="running", pid=self.pid, host=self.host, started_at=iso(now),
                          heartbeat_at=iso(now), request=None)
        for c in self.children.values():
            self.start(c, now, count=False)
        self.last_tick = self.last_sync = now

    def shutdown(self, force: bool = False):
        """모든 자식에 멈춤 요청 → 유예까지 기다린 뒤 남은 것은 kill. force 면 곧바로 kill."""
        alive = [c for c in self.children.values() if c.proc is not None and c.proc.poll() is None]
        for c in alive:
            self._close_stdin(c)
            c.state = "stopping"
        grace = 0 if force or not alive else max(GRACE_SEC[c.name] for c in alive)
        deadline = self.clock() + timedelta(seconds=grace)
        while any(c.proc.poll() is None for c in alive) and self.clock() < deadline:
            time.sleep(TICK_SEC)
        for c in alive:
            if c.proc.poll() is None:
                log.warning("%s 강제 종료", c.name)
                c.proc.kill()
        for c in self.children.values():
            code = None
            if c.proc is not None:
                try:
                    code = c.proc.wait(timeout=5)
                except Exception:
                    pass
                log.info("%s 종료 (코드 %s)", c.name, code)
            if c.proc is not None or c.state != "stopped":
                self._write(c, state="stopped", pid=None, exit_code=code, last_output="\n".join(c.tail))
            c.proc, c.state = None, "stopped"
        try:
            db.update_service(self.store, "supervisor", state="stopped", pid=None)
        except Exception as e:
            log.warning("관리 프로세스 상태 기록 실패: %s", e)

    # -- 자식 --------------------------------------------------------------------
    def start(self, c: Child, now: datetime, count: bool = True):
        c.tail.clear()
        try:
            c.proc = self.spawn(SERVICES[c.name])
        except OSError as e:
            c.tail.append(f"실행 실패: {e}")
            c.started_at = now
            self._crashed(c, now, None)
            return
        c.state, c.wanted, c.started_at, c.stop_deadline = "running", True, now, None
        c.watch_from = now + timedelta(seconds=STALE_SEC.get(c.name, 0))
        if count:
            c.restarts += 1
        if getattr(c.proc, "stdout", None) is not None:
            threading.Thread(target=pump, args=(c.name, c.proc.stdout, c.tail, self.out), daemon=True,
                             name=f"pump-{c.name}").start()
        log.info("%s 시작 (pid %s)", c.name, c.proc.pid)
        self._write(c, state="running", pid=c.proc.pid, host=self.host, started_at=iso(now), exit_code=None,
                    restarts=c.restarts)

    def stop(self, c: Child, now: datetime, restart: bool = False):
        if c.proc is None:                          # 이미 안 돈다 (꺼짐·재시작 대기)
            if restart:
                c.fails = 0
                self.start(c, now)
            else:
                c.state, c.next_start = "stopped", None
                self._write(c, state="stopped")
            return
        c.restart_after_stop = c.restart_after_stop or restart
        if c.state == "stopping":
            return
        self._close_stdin(c)
        c.state, c.stop_deadline = "stopping", now + timedelta(seconds=GRACE_SEC[c.name])
        log.info("%s 멈춤 요청 — 사이클을 마치길 최대 %d초 기다린다", c.name, GRACE_SEC[c.name])
        self._write(c, state="stopping")

    @staticmethod
    def _close_stdin(c: Child):
        try:
            c.proc.stdin.close()
        except (OSError, ValueError, AttributeError):
            pass

    def tick(self, now: datetime):
        if self.last_tick is not None and (now - self.last_tick).total_seconds() > GAP_SEC:
            log.info("틱 간격 %d초 — 절전 복귀로 보고 감시견 기준을 다시 잡는다", (now - self.last_tick).total_seconds())
            self._rebase_watch(now)
        self.last_tick = now
        for c in self.children.values():
            if c.proc is not None:
                code = c.proc.poll()
                if code is not None:
                    self._exited(c, now, code)
                elif c.state == "stopping" and now >= c.stop_deadline:
                    log.warning("%s 유예 %d초가 지나 강제 종료", c.name, GRACE_SEC[c.name])
                    c.proc.kill()
            if c.state == "backoff" and c.wanted and now >= c.next_start:
                self.start(c, now)
        if self.last_sync is None or (now - self.last_sync).total_seconds() >= SYNC_SEC:
            self.last_sync = now
            self.sync(now)

    def _exited(self, c: Child, now: datetime, code: int):
        c.proc = None
        output = "\n".join(c.tail)
        if c.state == "stopping":                   # 요청한 종료
            c.state = "stopped"
            log.info("%s 종료 (코드 %s)", c.name, code)
            self._write(c, state="stopped", pid=None, exit_code=code, last_output=output)
            if c.restart_after_stop:
                c.restart_after_stop, c.fails = False, 0
                self.start(c, now)
            return
        self._crashed(c, now, code)

    def _crashed(self, c: Child, now: datetime, code):
        ran = (now - c.started_at).total_seconds() if c.started_at else 0
        c.fails = 1 if ran >= HEALTHY_SEC else c.fails + 1
        wait = BACKOFF_SEC[min(c.fails, len(BACKOFF_SEC)) - 1]
        c.state, c.next_start = "backoff", now + timedelta(seconds=wait)
        log.warning("%s 비정상 종료 (코드 %s, 연속 %d회) — %d초 뒤 다시 켠다", c.name, code, c.fails, wait)
        self._write(c, state="backoff", pid=None, exit_code=code, last_output="\n".join(c.tail))
        if c.fails in (1, 4):                       # 재시작이 반복돼도 알림은 두 번만
            tail = "\n".join(list(c.tail)[-ALERT_LINES:])
            self._alert(f"{c.name} 가 비정상 종료했다 (코드 {code}, 연속 {c.fails}회) — {wait}초 뒤 다시 켠다"
                        + (f"\n{tail}" if tail else ""))

    # -- 요청·감시견 ---------------------------------------------------------------
    def sync(self, now: datetime):
        try:
            rows = db.list_services(self.store)
        except Exception as e:
            log.warning("서비스 상태 조회 실패 — 요청·감시견은 다음 주기에: %s", e)
            self._rebase_watch(now)                 # DB 가 죽어 있던 동안은 워커 heartbeat 도 못 남겼다
            return
        for name, c in self.children.items():
            row = rows.get(name) or {}
            action = row.get("request")
            if action:
                log.info("%s %s 요청 (%s)", name, action, row.get("requested_by") or "-")
                self.handle(c, action, now)
                try:
                    db.clear_request(self.store, name, row.get("requested_at"))
                except Exception as e:
                    log.warning("요청 정리 실패: %s", e)
            limit = STALE_SEC.get(name)
            if limit and c.state == "running" and c.proc is not None and now >= c.watch_from:
                last = max(t for t in (parse(row.get("heartbeat_at")), c.started_at) if t)
                idle = (now - last).total_seconds()
                if idle > limit:
                    self._alert(f"{name} heartbeat 가 {idle / 60:.0f}분째 없다 — 멈춘 것으로 보고 다시 켠다")
                    self.stop(c, now, restart=True)
        try:
            db.update_service(self.store, "supervisor", state="running", pid=self.pid, host=self.host, heartbeat_at=iso(now))
        except Exception as e:
            log.warning("관리 프로세스 heartbeat 기록 실패: %s", e)

    def handle(self, c: Child, action: str, now: datetime):
        if action == "start":
            if c.proc is None:
                c.fails = 0
                self.start(c, now)
            elif c.state == "stopping":
                c.restart_after_stop = True
        elif action == "stop":
            c.wanted = False
            c.restart_after_stop = False
            self.stop(c, now)
        elif action == "restart":
            self.stop(c, now, restart=True)

    def _rebase_watch(self, now: datetime):
        for c in self.children.values():
            if c.name in STALE_SEC:
                c.watch_from = max(c.watch_from or now, now + timedelta(seconds=STALE_SEC[c.name]))

    # -- 기록·알림 ---------------------------------------------------------------
    def _write(self, c: Child, **fields):
        try:
            db.update_service(self.store, c.name, **fields)
        except Exception as e:                      # 기록 실패가 관리 루프를 멈추면 안 된다
            log.warning("%s 상태 기록 실패: %s", c.name, e)

    def _alert(self, body: str):
        log.warning(body)
        try:
            self.notify.send(Signal("SYSTEM", "⚪ 시스템", "run.py 관리 프로세스", body))
        except Exception as e:
            log.warning("알림 실패: %s", e)


def check_lease(store, host: str, pid: int, now: datetime):
    """다른 run.py 가 돌고 있으면 사유 문자열, 아니면 None."""
    row = db.list_services(store).get("supervisor")
    if not row or row["state"] != "running":
        return None
    beat = parse(row.get("heartbeat_at"))
    if beat is None or (now - beat).total_seconds() >= LEASE_SEC or (row.get("host"), row.get("pid")) == (host, pid):
        return None
    return (f"다른 run.py 가 이미 돌고 있다 ({row.get('host')} pid {row.get('pid')}, heartbeat {(now - beat).total_seconds():.0f}초 전). "
            f"중복으로 켜면 알림·주문이 두 번 나간다 — python run.py status 로 확인할 것")


# -- 콘솔 명령 ---------------------------------------------------------------------

def _kst(value) -> str:
    t = parse(value)
    return t.astimezone(timezone(timedelta(hours=9))).strftime("%m-%d %H:%M:%S") if t else "-"


def _age(value, now) -> str:
    t = parse(value)
    return f"{(now - t).total_seconds():.0f}초 전" if t else "-"


def supervisor_alive(rows: dict, now: datetime) -> bool:
    """관리 프로세스 heartbeat 가 LEASE_SEC 안인가 — 백오피스 '운영' 화면도 같은 기준을 쓴다."""
    sup = rows.get("supervisor")
    beat = parse(sup.get("heartbeat_at")) if sup else None
    return bool(sup and sup["state"] == "running" and beat and (now - beat).total_seconds() < LEASE_SEC)


def status_text(rows: dict, now: datetime) -> str:
    sup = rows.get("supervisor")
    lines = [f"관리 프로세스: {'실행 중' if supervisor_alive(rows, now) else '꺼져 있음'}"
             + (f" ({sup.get('host')} pid {sup.get('pid')}, heartbeat {_age(sup.get('heartbeat_at'), now)})" if sup else "")]
    for name in SERVICES:
        r = rows.get(name)
        if r is None:
            lines.append(f"  {name:<10} 기록 없음")
            continue
        lines.append(f"  {name:<10} {r['state']:<8} pid {r.get('pid') or '-'} · 시작 {_kst(r.get('started_at'))} KST · "
                     f"heartbeat {_age(r.get('heartbeat_at'), now)} · 재시작 {r.get('restarts') or 0}회 · "
                     f"종료 코드 {r.get('exit_code') if r.get('exit_code') is not None else '-'}"
                     + (f" · 대기 요청 {r['request']}" if r.get("request") else ""))
    return "\n".join(lines)


def _command(argv: list) -> int:
    store = db.connect()
    rows = db.list_services(store)
    now = datetime.now(timezone.utc)
    if argv[0] == "status":
        print(status_text(rows, now))
        return 0
    action, name = argv
    if name not in rows:
        print("run.py 가 한 번도 돌지 않아 서비스 기록이 없다 — 먼저 python run.py 로 실행할 것")
        return 1
    db.request_service(store, name, action, f"console@{socket.gethostname()}")
    print(f"{name} {action} 요청을 남겼다 — 실행 중인 run.py 가 {SYNC_SEC}초 안에 처리한다")
    if not supervisor_alive(rows, now):
        print("주의: 관리 프로세스가 꺼져 있다. 요청은 다음 실행 때 지워진다")
    return 0


def run_forever() -> int:
    out = Output()
    threading.Thread(target=out.run_printer, daemon=True, name="printer").start()
    handler = QueueHandler(out)
    handler.setFormatter(logging.Formatter("[run.py] %(asctime)s [%(levelname)s] %(message)s"))
    file_handler = logging.FileHandler(DATA_DIR / "supervisor.log", encoding="utf-8-sig")
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[file_handler, handler])
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    host, pid = socket.gethostname(), os.getpid()
    stop = {"n": 0}

    def on_signal(signum, frame):
        stop["n"] += 1
        if stop["n"] >= 2:
            raise KeyboardInterrupt

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), on_signal)
    store = None
    while store is None and not stop["n"]:           # 자식보다 먼저 붙는다 — 스키마 변경을 한 곳에서
        try:
            store = db.connect()
        except Exception as e:
            log.warning("MySQL 연결 실패 — 10초 뒤 다시: %s", e)
            for _ in range(10):
                if stop["n"]:
                    break
                time.sleep(1)
    if store is None:
        return 1
    reason = check_lease(store, host, pid, datetime.now(timezone.utc))
    if reason:
        log.error(reason)
        time.sleep(0.5)
        return 1
    from .notify.dispatcher import Dispatcher
    # 비정상 종료·멈춤 재시작 알림은 공개 채널로 보내지 않는다 — 채널 없이 로그·신호 이력(DB)에만 남고, 사유는 운영 화면에서 본다
    sup = Supervisor(store, Dispatcher([], record=lambda s, r: db.log_signal(store, s, r)), out=out, host=host, pid=pid)
    sup.boot(datetime.now(timezone.utc))
    log.info("엔진·Binance·백오피스를 띄웠다. 끄기: Ctrl+C (한 번 더 누르면 강제). 제어: 다른 콘솔에서 python run.py status")
    try:
        while not stop["n"]:
            try:
                sup.tick(datetime.now(timezone.utc))
            except Exception as e:
                log.exception("관리 루프 오류: %s", e)
            time.sleep(TICK_SEC)
        log.info("끄는 중 — 워커가 사이클을 마칠 때까지 기다린다 (한 번 더 Ctrl+C 면 강제 종료)")
        sup.shutdown()
    except KeyboardInterrupt:
        log.warning("강제 종료")
        sup.shutdown(force=True)
    time.sleep(0.5)                                   # 마지막 줄이 콘솔에 찍힐 틈
    return 0


def main(argv: list) -> int:
    if not argv:
        return run_forever()
    if argv == ["status"] or (len(argv) == 2 and argv[0] in ACTIONS and argv[1] in SERVICES):
        return _command(argv)
    print(USAGE)
    return 2
