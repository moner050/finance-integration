"""엔진 회귀 테스트 — 분리본이 원본 scalping_alert.py 와 같은 알림을 내는지.

golden_engine.json 은 원본 모듈에 tests/scenario.py 를 그대로 돌려 만든 기록이다.
"""
import json
from pathlib import Path

import alertbot.engine as E
from tests import scenario as sc

GOLDEN = json.loads(Path(__file__).with_name("golden_engine.json").read_text(encoding="utf-8"))


def make_engine(monkeypatch, tmp_path, candles=None):
    monkeypatch.setattr(E, "now_local", sc.fixed_now_local)
    monkeypatch.setattr(E, "BASE_DIR", tmp_path)      # CSV 를 프로젝트 루트에 쓰지 않는다
    cap = sc.CaptureNotifier()
    eng = E.SignalEngine(sc.FakeClient(candles or sc.scenario_candles()), cap, True, sc.WATCHLIST)
    return eng, cap


def test_engine_scenario_matches_original(monkeypatch, tmp_path):
    eng, cap = make_engine(monkeypatch, tmp_path)
    sent = sc.drive(eng, cap)
    assert [s[0] for s in sent] == [g[0] for g in GOLDEN]
    assert sent == GOLDEN


def test_engine_state_transitions(monkeypatch, tmp_path):
    eng, cap = make_engine(monkeypatch, tmp_path)
    states = []
    for price, holdings in sc.STEPS:
        eng.evaluate("AAA", {"AAA": price}, holdings)
        states.append(eng.state["AAA"])
    assert states == ["진입대기", "진입대기", "보유", "청산대기", "관망"]
    assert "AAA" not in eng.stop_ref          # 청산 후 손절선 정리
    assert (tmp_path / "trade_log.csv").read_text(encoding="utf-8-sig").count("\n") == 2   # 헤더 + 1건
