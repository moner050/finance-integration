"""워커 수명 — 멈춤 요청(stdin 닫힘)·heartbeat·1초 단위 대기."""
import os
import subprocess
import sys
import threading
import time

import pytest

from alertbot import lifecycle
from alertbot.config import BASE_DIR


@pytest.fixture(autouse=True)
def clean():
    lifecycle._stop.clear()
    lifecycle._heartbeat = None
    yield
    lifecycle._stop.clear()
    lifecycle._heartbeat = None


def test_sleep_returns_soon_after_stop():
    threading.Timer(0.3, lifecycle._stop.set).start()
    t0 = time.monotonic()
    lifecycle.sleep(5)
    assert time.monotonic() - t0 < 2
    assert not lifecycle.running()


def test_heartbeat_once_per_sleep_and_errors_swallowed():
    calls = []

    def beat():
        calls.append(1)
        raise RuntimeError("db down")

    lifecycle.install(heartbeat=beat)
    lifecycle.sleep(0)
    lifecycle.sleep(0.01)
    assert len(calls) == 2 and lifecycle.running()


def test_standalone_run_does_not_watch_stdin(monkeypatch):
    monkeypatch.delenv("ALERT_SUPERVISED", raising=False)
    before = sum(t.name == "lifecycle-stdin" for t in threading.enumerate())
    lifecycle.install()
    assert sum(t.name == "lifecycle-stdin" for t in threading.enumerate()) == before


CHILD = (
    "import sys\n"
    "sys.path.insert(0, {root!r})\n"
    "from alertbot import lifecycle\n"
    "lifecycle.install(on_stop=lambda: print('on_stop', flush=True))\n"
    "print('ready', flush=True)\n"
    "while lifecycle.running():\n"
    "    lifecycle.sleep(30)\n"
    "print('bye', flush=True)\n"
)


def test_supervised_child_stops_when_stdin_closes():
    env = {**os.environ, "ALERT_SUPERVISED": "1", "PYTHONIOENCODING": "utf-8"}
    p = subprocess.Popen([sys.executable, "-c", CHILD.format(root=str(BASE_DIR))], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, cwd=BASE_DIR)
    assert p.stdout.readline().strip() == b"ready"
    t0 = time.monotonic()
    p.stdin.close()                                         # 관리 프로세스의 멈춤 요청
    out = p.stdout.read()
    assert p.wait(timeout=10) == 0
    assert time.monotonic() - t0 < 5                        # 30초 sleep 중이어도 곧바로 끝난다
    assert b"on_stop" in out and b"bye" in out
