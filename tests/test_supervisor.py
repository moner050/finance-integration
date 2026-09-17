"""관리 프로세스 — 가짜 자식으로 재시작 대기·요청·감시견·lease·종료, 실제 자식으로 콘솔이 막혀도 멈추지 않는지."""
import subprocess
import sys
import threading
from collections import deque
from datetime import datetime, timedelta, timezone

import pytest

from alertbot import db as DBM
from alertbot import supervisor as S
from alertbot.notify.base import Channel
from alertbot.notify.dispatcher import Dispatcher

T0 = datetime(2026, 9, 17, 0, 0, tzinfo=timezone.utc)


class Recorder(Channel):
    name = "rec"

    def __init__(self):
        super().__init__("info")
        self.got = []

    def send(self, signal):
        self.got.append(signal)
        return "ok"


class FakeStdin:
    def __init__(self, proc):
        self.proc = proc

    def close(self):
        self.proc.stdin_closed = True


class FakeProc:
    """stdin 이 닫히면 다음 poll 에 종료 코드 0 (exit_on_close=False 면 멈춘 워커처럼 안 끝난다)."""
    _pid = 1000

    def __init__(self, exit_on_close=True):
        FakeProc._pid += 1
        self.pid, self.code, self.stdin_closed, self.killed = FakeProc._pid, None, False, False
        self.exit_on_close, self.stdin, self.stdout = exit_on_close, FakeStdin(self), None

    def poll(self):
        if self.code is None and self.stdin_closed and self.exit_on_close:
            self.code = 0
        return self.code

    def kill(self):
        self.killed, self.code = True, -9

    def wait(self, timeout=None):
        return self.poll()


class Env:
    def __init__(self, hang=()):
        self.store = DBM.DB.sqlite().init_schema()
        self.rec = Recorder()
        self.hang = set(hang)
        self.procs = {name: [] for name in S.SERVICES}
        self.now = T0
        self.sup = S.Supervisor(self.store, Dispatcher([self.rec]), spawn=self.spawn, host="h", pid=1,
                                clock=lambda: self.now)
        self.sup.boot(T0)

    def spawn(self, script):
        name = next(n for n, s in S.SERVICES.items() if s == script)
        p = FakeProc(exit_on_close=name not in self.hang)
        self.procs[name].append(p)
        return p

    def at(self, sec):
        """그 시각으로 건너뛰어 한 번 틱 (60초 넘게 건너뛰면 관리 프로세스는 절전 복귀로 본다)."""
        self.now = T0 + timedelta(seconds=sec)
        self.sup.tick(self.now)

    def run(self, sec, step=S.SYNC_SEC):
        """실제처럼 step 초마다 틱하며 그 시각까지."""
        t = (self.now - T0).total_seconds()
        while t < sec:
            t = min(t + step, sec)
            self.at(t)

    def row(self, name):
        return DBM.list_services(self.store)[name]

    def alerts(self):
        return [s.body for s in self.rec.got if s.kind == "SYSTEM"]


def test_boot_starts_everything_and_records_rows():
    e = Env()
    assert all(len(ps) == 1 for ps in e.procs.values())
    for name in S.SERVICES:
        assert e.row(name)["state"] == "running" and e.row(name)["pid"] == e.procs[name][0].pid
    sup = e.row("supervisor")
    assert (sup["state"], sup["host"], sup["pid"]) == ("running", "h", 1)


def test_crash_restarts_with_backoff_and_alerts_only_first_and_fourth():
    e = Env()
    e.procs["engine"][-1].code = 1
    e.at(10)
    assert e.row("engine")["state"] == "backoff" and e.row("engine")["exit_code"] == 1
    e.at(14)
    assert len(e.procs["engine"]) == 1                       # 5초 대기 전
    e.at(15)
    assert len(e.procs["engine"]) == 2 and e.row("engine")["state"] == "running"
    for crash_at, wait in ((20, 30), (51, 120), (172, 300)):   # 연속 2·3·4번째 실패
        e.procs["engine"][-1].code = 1
        e.at(crash_at)
        e.at(crash_at + wait - 1)
        n = len(e.procs["engine"])
        e.at(crash_at + wait)
        assert len(e.procs["engine"]) == n + 1
    assert len(e.alerts()) == 2 and "연속 1회" in e.alerts()[0] and "연속 4회" in e.alerts()[1]
    e.procs["engine"][-1].code = 1                            # 5분 넘게 돌다 죽으면 처음부터 센다
    e.at(172 + 300 + S.HEALTHY_SEC)
    assert e.sup.children["engine"].fails == 1 and e.row("engine")["restarts"] == 4


def test_spawn_failure_goes_to_backoff():
    e = Env()

    def broken(script):
        raise OSError("no python")

    e.sup.spawn = broken
    e.procs["binance"][-1].code = 2
    e.at(1)
    e.at(6)
    assert e.sup.children["binance"].state == "backoff" and "실행 실패" in e.row("binance")["last_output"]


def test_stop_request_closes_stdin_without_restart_or_alert():
    e = Env()
    DBM.request_service(e.store, "binance", "stop", "admin@example.com")
    e.at(S.SYNC_SEC)
    p = e.procs["binance"][0]
    assert p.stdin_closed and e.row("binance")["state"] == "stopping" and e.row("binance")["request"] is None
    e.at(S.SYNC_SEC + 1)
    e.at(3600)
    assert e.row("binance")["state"] == "stopped" and len(e.procs["binance"]) == 1 and not e.alerts()
    DBM.request_service(e.store, "binance", "start", "admin@example.com")
    e.at(3600 + S.SYNC_SEC)
    assert len(e.procs["binance"]) == 2 and e.row("binance")["state"] == "running"


def test_grace_expiry_kills_a_worker_that_does_not_stop():
    e = Env(hang={"binance"})
    DBM.request_service(e.store, "binance", "stop", "x")
    e.at(S.SYNC_SEC)
    e.at(S.SYNC_SEC + S.GRACE_SEC["binance"] - 1)
    assert not e.procs["binance"][0].killed
    e.at(S.SYNC_SEC + S.GRACE_SEC["binance"])
    e.at(S.SYNC_SEC + S.GRACE_SEC["binance"] + 1)
    assert e.procs["binance"][0].killed and e.row("binance")["state"] == "stopped"


def test_restart_request_starts_again_right_after_exit():
    e = Env()
    DBM.request_service(e.store, "backoffice", "restart", "x")
    e.at(S.SYNC_SEC)
    e.at(S.SYNC_SEC + 1)
    assert len(e.procs["backoffice"]) == 2 and e.row("backoffice")["state"] == "running"
    assert e.row("backoffice")["restarts"] == 1


def test_newer_request_is_not_cleared():
    store = DBM.DB.sqlite().init_schema()
    DBM.ensure_services(store, ["engine"])
    DBM.request_service(store, "engine", "stop", "a")
    old = DBM.list_services(store)["engine"]["requested_at"]
    DBM.request_service(store, "engine", "restart", "b")
    DBM.clear_request(store, "engine", old)
    assert DBM.list_services(store)["engine"]["request"] == "restart"


def test_watchdog_restarts_a_frozen_worker_but_not_the_backoffice():
    e = Env()
    e.run(S.STALE_SEC["binance"] - 5)                       # 시작 직후는 보지 않는다
    assert len(e.procs["binance"]) == 1
    DBM.touch_service(e.store, "engine")                     # 엔진은 heartbeat 를 남긴다
    e.run(S.STALE_SEC["binance"] + S.SYNC_SEC)
    assert e.procs["binance"][0].stdin_closed and "heartbeat" in e.alerts()[0]
    e.run(S.STALE_SEC["binance"] + S.SYNC_SEC + 1, step=1)
    assert len(e.procs["binance"]) == 2
    assert len(e.procs["engine"]) == 1 and len(e.procs["backoffice"]) == 1


def test_watchdog_waits_after_sleep_resume_and_db_failure():
    e = Env()
    e.at(1)
    e.at(1 + 3600)                                           # 한 시간 틱 공백 = 절전 복귀
    assert len(e.procs["binance"]) == 1 and not e.alerts()
    e.at(1 + 3600 + S.STALE_SEC["binance"] - 10)
    assert len(e.procs["binance"]) == 1
    e.sup._rebase_watch(e.now)                               # DB 조회 실패도 같은 처리
    e.at(1 + 3600 + S.STALE_SEC["binance"] + S.SYNC_SEC)
    assert len(e.procs["binance"]) == 1


def test_boot_clears_requests_left_while_down():
    store = DBM.DB.sqlite().init_schema()
    DBM.ensure_services(store, ["binance"])
    DBM.request_service(store, "binance", "stop", "x")
    procs = []
    sup = S.Supervisor(store, Dispatcher([]), spawn=lambda s: procs.append(FakeProc()) or procs[-1], host="h", pid=1,
                       clock=lambda: T0)
    sup.boot(T0)
    rows = DBM.list_services(store)
    assert rows["binance"]["request"] is None and rows["binance"]["state"] == "running" and len(procs) == 3


def test_lease():
    store = DBM.DB.sqlite().init_schema()
    assert S.check_lease(store, "me", 7, T0) is None
    DBM.ensure_services(store, ["supervisor"])
    DBM.update_service(store, "supervisor", state="running", host="other", pid=9, heartbeat_at=S.iso(T0))
    assert "이미 돌고 있다" in S.check_lease(store, "me", 7, T0 + timedelta(seconds=10))
    assert S.check_lease(store, "me", 7, T0 + timedelta(seconds=S.LEASE_SEC + 1)) is None
    assert S.check_lease(store, "other", 9, T0 + timedelta(seconds=10)) is None
    DBM.update_service(store, "supervisor", state="stopped")
    assert S.check_lease(store, "me", 7, T0 + timedelta(seconds=10)) is None


def test_shutdown_stops_everyone_and_kills_stragglers(monkeypatch):
    e = Env(hang={"engine"})
    ticks = iter(range(0, 100000, 60))
    e.sup.clock = lambda: T0 + timedelta(seconds=next(ticks))
    monkeypatch.setattr(S.time, "sleep", lambda s: None)
    e.procs["binance"][-1].code = 1
    e.at(1)                                                  # binance 는 재시작 대기 중
    e.sup.shutdown()
    assert e.procs["engine"][0].killed and not e.procs["backoffice"][0].killed
    rows = DBM.list_services(e.store)
    assert all(rows[n]["state"] == "stopped" for n in S.SERVICES) and rows["supervisor"]["state"] == "stopped"


def test_status_text_and_usage():
    e = Env()
    DBM.update_service(e.store, "supervisor", heartbeat_at=S.iso(T0))
    text = S.status_text(DBM.list_services(e.store), T0 + timedelta(seconds=3))
    assert text.startswith("관리 프로세스: 실행 중") and "engine" in text and "binance" in text
    assert S.status_text(DBM.list_services(e.store), T0 + timedelta(minutes=5)).startswith("관리 프로세스: 꺼져 있음")
    assert S.main(["bogus"]) == 2 and S.main(["stop", "everything"]) == 2


CHATTY = "import sys\nfor i in range(5000):\n    print('line', i, flush=True)\n"


def test_blocked_console_never_blocks_the_child():
    """2026-09-16 사고 회귀 — 콘솔이 안 받아도(출력 큐가 가득) 자식은 계속 쓰고 정상 종료한다."""
    out = S.Output(maxsize=10)                               # 프린터 없음 = 콘솔이 막힌 상태
    p = subprocess.Popen([sys.executable, "-c", CHATTY], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT)
    tail = deque(maxlen=S.TAIL_LINES)
    reader = threading.Thread(target=S.pump, args=("binance", p.stdout, tail, out), daemon=True)
    reader.start()
    assert p.wait(timeout=30) == 0
    reader.join(timeout=10)
    assert out.dropped > 0 and tail[-1] == "line 4999" and out.q.qsize() == 10


def test_output_keeps_counting_when_console_raises():
    out = S.Output(maxsize=2)
    for i in range(5):
        out.put(f"x{i}")
    assert out.dropped == 3


@pytest.mark.parametrize("argv", [["status"], ["restart", "binance"]])
def test_command_parsing_reaches_db(monkeypatch, argv, capsys):
    store = DBM.DB.sqlite().init_schema()
    monkeypatch.setattr(S.db, "connect", lambda: store)
    DBM.ensure_services(store, [*S.SERVICES, "supervisor"])
    assert S.main(argv) == 0
    printed = capsys.readouterr().out
    if argv[0] == "status":
        assert "관리 프로세스: 꺼져 있음" in printed
    else:
        assert DBM.list_services(store)["binance"]["request"] == "restart" and "주의" in printed
