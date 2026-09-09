"""엔진 테스트.

golden_engine.json 은 분리 전 원본 모듈에 tests/scenario.py 를 그대로 돌려 만든 알림 기록이다.
감지기 수정 뒤에도 알림의 종류·순서·본문은 같아야 한다. 단 RSI 는 Wilder 방식으로
바꿨으므로(P2-2) 본문의 RSI 숫자만 비교에서 뺀다.
"""
import json
import re
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from alertbot import db as DBM
import alertbot.engine as E
import alertbot.market_hours as MH
from tests import scenario as sc
from tests.conftest import bar, kr_fixture, make_candles, TZ

GOLDEN = json.loads(Path(__file__).with_name("golden_engine.json").read_text(encoding="utf-8"))


def make_engine(monkeypatch, tmp_path, candles=None, client=None, watchlist=None, store=None):
    monkeypatch.setattr(E, "now_local", sc.fixed_now_local)
    monkeypatch.setattr(MH, "now_local", sc.fixed_now_local)   # 마감임박 판정도 고정 시각으로
    monkeypatch.setattr(E, "BASE_DIR", tmp_path)      # CSV 를 프로젝트 루트에 쓰지 않는다
    cap = sc.CaptureNotifier()
    client = client or sc.FakeClient(candles or sc.scenario_candles())
    eng = E.SignalEngine(client, cap, True, watchlist or sc.WATCHLIST, store)
    return eng, cap


def _mask_rsi(rows):
    return [[lvl, label, re.sub(r"RSI [\d.]+", "RSI *", body)] for lvl, label, body in rows]


def test_engine_scenario_matches_original(monkeypatch, tmp_path):
    eng, cap = make_engine(monkeypatch, tmp_path)
    sent = sc.drive(eng, cap)
    assert [s[0] for s in sent] == [g[0] for g in GOLDEN]
    assert _mask_rsi(sent) == _mask_rsi(GOLDEN)


def test_engine_state_transitions(monkeypatch, tmp_path):
    eng, cap = make_engine(monkeypatch, tmp_path)
    states = []
    for price, holdings in sc.STEPS:
        eng.evaluate("AAA", {"AAA": price}, holdings)
        states.append(eng.state["AAA"])
    assert states == ["진입대기", "진입대기", "보유", "청산대기", "관망"]
    assert "AAA" not in eng.stop_ref          # 청산 후 손절선 정리
    assert (tmp_path / "trade_log.csv").read_text(encoding="utf-8-sig").count("\n") == 2   # 헤더 + 1건


# --- P1-1 매수 판정은 신호봉 종가 기준 --------------------------------------------

def test_entry_requires_signal_bar_close_above_vwap(monkeypatch, tmp_path):
    candles = sc.scenario_candles()
    # 신호봉: 거래량 돌파 + 강봉이지만 종가는 기준선(≈100.05) 중립대. 현재가만 위로 튄 상황.
    last = bar(datetime(2026, 3, 25, 10, 1, tzinfo=TZ["KR"]), 100.05, 3000, high=100.1, low=100.0)
    candles[-1] = last
    eng, cap = make_engine(monkeypatch, tmp_path, candles=candles)
    eng.evaluate("AAA", {"AAA": 100.8}, {})
    assert cap.sent == []
    snap = eng.snapshots["AAA"]
    assert snap["pos"] == "above" and snap["pos_close"] == "neutral"
    assert eng.stats["AAA"].get("rvol") == 1 and eng.stats["AAA"].get("vwap") is None


# --- P0-3 전일 종가는 날짜로 고른다 ---------------------------------------------

def daily(dates, closes):
    return [bar(datetime.fromisoformat(d).replace(tzinfo=TZ["KR"]), c, 1) for d, c in zip(dates, closes)]


def test_prev_close_is_last_bar_before_session(monkeypatch, tmp_path):
    with_today = daily(["2026-03-23", "2026-03-24", "2026-03-25"], [90.0, 95.0, 100.0])
    eng, _ = make_engine(monkeypatch, tmp_path, client=sc.FakeClient(sc.scenario_candles(), daily=with_today))
    eng.refresh_prev_closes({"AAA": "KR"})
    assert eng.prev_close["AAA"] == 95.0

    # 프리마켓: 오늘 일봉이 아직 없어도 어제 종가여야 한다 (원본은 daily[-2] → 그저께 90)
    without_today = daily(["2026-03-23", "2026-03-24"], [90.0, 95.0])
    eng2, _ = make_engine(monkeypatch, tmp_path, client=sc.FakeClient(sc.scenario_candles(), daily=without_today))
    eng2.refresh_prev_closes({"AAA": "KR"})
    assert eng2.prev_close["AAA"] == 95.0
    assert eng2.prev_close_date["AAA"] == "2026-03-25"


# --- P0-1 세션 백필: 프로파일 이력으로 오늘 세션을 채운다 -------------------------

def test_profile_refresh_backfills_session_state(monkeypatch, tmp_path):
    cur, hist = kr_fixture()
    client = sc.FakeClient(cur, history=hist + cur)
    eng, _ = make_engine(monkeypatch, tmp_path, client=client)
    eng.refresh_volume_profile(["AAA"])
    ss = eng.sessions["AAA"]
    assert ss.session == "2026-03-25" and ss.peak == 6.0 and ss.vwap == 101.0896
    # 이후 30봉 창만 봐도 정점과 VWAP 은 세션 전체 값이다 (40번째 봉은 창 밖)
    client.candles = cur
    snap = eng._snapshot("AAA", {"AAA": 101.0})
    assert snap["peak"] == 6.0 and snap["vwap"] == 101.0896
    eng.client.candles = cur[-30:]
    snap2 = eng._snapshot("AAA", {"AAA": 101.0})
    assert snap2["peak"] == 6.0 and snap2["vwap"] == 101.0896


# --- P2-1 선행 모멘텀 기록 ------------------------------------------------------

def test_leader_momentum_from_price_history(monkeypatch, tmp_path):
    eng, _ = make_engine(monkeypatch, tmp_path)
    now = datetime.now(timezone.utc)
    eng.price_hist["NVDA"] = deque([(now - timedelta(minutes=7), 100.0), (now - timedelta(minutes=2), 103.0)])
    eng.price_hist["AVGO"] = deque([(now - timedelta(minutes=6), 200.0)])
    assert eng.leader_momentum(["NVDA", "AVGO"], {"NVDA": 101.0, "AVGO": 198.0}) == 0.0   # +1% / -1%
    assert eng.leader_momentum(["TSM"], {"TSM": 50.0}) is None                            # 이력 없음
    eng._record_prices({"NVDA": 102.0})
    assert eng.price_hist["NVDA"][-1][1] == 102.0


# --- Phase 4: 워치리스트 핫리로드 · 상태 영속 --------------------------------------

def store_with(watch):
    d = DBM.DB.sqlite().init_schema()
    DBM.seed_watchlist(d, watch)
    return d


def test_watchlist_hot_reload(monkeypatch, tmp_path):
    store = store_with(sc.WATCHLIST)
    eng, _ = make_engine(monkeypatch, tmp_path, store=store)
    eng._reload_watchlist()
    assert eng.tickers == ["AAA"]
    v = eng.watch_version
    DBM.upsert_watch(store, "BBB", "US", name="비비", leaders=["NVDA"])
    eng._reload_watchlist()
    assert eng.tickers == ["AAA", "BBB"] and eng.watchlist["BBB"]["leaders"] == ["NVDA"]
    assert eng.watch_version != v
    # 상태가 생긴 종목을 목록에서 빼면 상태도 지운다
    eng.evaluate("AAA", {"AAA": 100.8}, {})
    assert eng.state["AAA"] == "진입대기"
    DBM.set_enabled(store, "AAA", False)
    eng._reload_watchlist()
    assert eng.tickers == ["BBB"]
    assert "AAA" not in eng.state and "AAA" not in eng.pending and "AAA" not in eng.sessions
    # 버전이 같으면 다시 읽지 않는다
    eng.watchlist["BBB"]["name"] = "임시"
    eng._reload_watchlist()
    assert eng.watchlist["BBB"]["name"] == "임시"
    # DB 장애 → 이전 목록 유지
    def down(_):
        raise RuntimeError("db down")
    monkeypatch.setattr(DBM, "watchlist_version", down)
    eng._reload_watchlist()
    assert eng.tickers == ["BBB"]


def test_status_persist_and_restore_after_restart(monkeypatch, tmp_path):
    store = store_with(sc.WATCHLIST)
    eng, cap = make_engine(monkeypatch, tmp_path, store=store)
    for price, holdings in sc.STEPS[:3]:        # 매수 신호 → 보유. 신호봉 저점 100.3 이 손절선
        eng.evaluate("AAA", {"AAA": price}, holdings)
    assert eng.state["AAA"] == "보유" and eng.stop_ref["AAA"] == 100.3
    eng._save_status(["AAA"], [])
    saved = DBM.load_engine_status(store)
    assert saved["active"] == ["AAA"] and saved["state"]["AAA"]["state"] == "보유"
    assert saved["state"]["AAA"]["stop_ref"] == 100.3
    assert saved["state"]["AAA"]["session"]["session"] == "2026-03-25"
    assert saved["snapshots"]["AAA"]["vwap"] > 0 and "candles" not in saved["snapshots"]["AAA"]

    # 재시작: 손절선·진입 시각·세션 누적값이 그대로 살아난다
    eng2, cap2 = make_engine(monkeypatch, tmp_path, store=store)
    assert eng2.state["AAA"] == "보유" and eng2.stop_ref["AAA"] == 100.3
    assert eng2.entry_at["AAA"] == eng.entry_at["AAA"]
    assert (eng2.sessions["AAA"].vwap, eng2.sessions["AAA"].peak) == (eng.sessions["AAA"].vwap, eng.sessions["AAA"].peak)
    # 복원 덕분에 재시작 직후 저점 이탈이 곧바로 매도 알림이 된다.
    # 복원이 없으면 '방금 매수'로 오인해 밴드 기준으로 바뀌고 이 가격에선 침묵한다.
    eng2.evaluate("AAA", {"AAA": 100.2}, {"AAA": {"qty": 10.0, "avg": 100.8}})
    assert [s[0] for s in cap2.sent] == ["🔴 매도하세요"]
    # 감시 목록에 없는 종목의 저장 상태는 무시한다
    saved["state"]["ZZZ"] = {"state": "보유"}
    DBM.save_engine_status(store, [], [], saved["state"], {})
    eng3, _ = make_engine(monkeypatch, tmp_path, store=store)
    assert "ZZZ" not in eng3.state and eng3.state["AAA"] == "보유"
