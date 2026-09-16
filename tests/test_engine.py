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
    monkeypatch.setattr(E, "DATA_DIR", tmp_path)      # CSV 를 프로젝트 루트에 쓰지 않는다
    cap = sc.CaptureNotifier()
    client = client or sc.FakeClient(candles or sc.scenario_candles())
    eng = E.SignalEngine(client, cap, True, watchlist or sc.WATCHLIST, store)
    return eng, cap


def _mask_rsi(rows):
    return [[lvl, label, re.sub(r"RSI [\d.]+", "RSI *", body)] for lvl, label, body in rows]


def push_bar(eng, close, volume=1000, **kw):
    """가짜 클라이언트에 다음 1분 완성봉을 붙인다 (고가/저가는 종가 ±0.05)."""
    last = datetime.fromisoformat(eng.client.candles[-1]["timestamp"])
    kw.setdefault("high", close + 0.05)
    kw.setdefault("low", close - 0.05)
    eng.client.candles = eng.client.candles + [bar(last + timedelta(minutes=1), close, volume, **kw)]


def test_engine_scenario_matches_original(monkeypatch, tmp_path):
    eng, cap = make_engine(monkeypatch, tmp_path)
    sent = sc.drive(eng, cap)
    assert [s[0] for s in sent] == [g[0] for g in GOLDEN]
    assert _mask_rsi(sent) == _mask_rsi(GOLDEN)


def test_holding_alerts_keep_account_lines_separate(monkeypatch, tmp_path):
    """보유 중 알림의 손익·평단·수량은 account 로 분리된다 — 공개 채널에는 시장 근거(body)만 간다."""
    eng, cap = make_engine(monkeypatch, tmp_path)
    sc.run_steps(eng, range(4))
    sell = cap.signals[-1]
    assert sell.kind == "SELL" and "손익" not in sell.body and sell.account == "손익 -0.6%  (평단 100.8 → 현재 100.2)"
    entry = cap.signals[0]
    assert entry.kind == "ENTRY" and entry.account is None                # 매수 신호는 시장 근거뿐


def test_market_summary_keeps_holdings_in_account_lines(monkeypatch, tmp_path):
    """시황 본문은 전 종목의 시장 상태만, 내 보유 현황(수량·손익·청산 대기)은 account 로 분리된다."""
    eng, cap = make_engine(monkeypatch, tmp_path)
    held = {"AAA": {"qty": 10.0, "avg": 100.0}}
    eng.evaluate("AAA", {"AAA": 100.9}, held)
    eng.last_summary = datetime.now(timezone.utc) - timedelta(hours=1)
    eng.market_summary(["AAA"], held)
    summary = cap.signals[-1]
    assert summary.kind == "SUMMARY" and "보유" not in summary.body and "테스트  기준선 위" in summary.body
    assert summary.account.startswith("\n내 보유\n🟢 테스트  보유 10주 +0.90%")


def test_engine_state_transitions(monkeypatch, tmp_path):
    eng, cap = make_engine(monkeypatch, tmp_path)
    states = []
    for i in range(len(sc.STEPS)):
        sc.run_steps(eng, [i])
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
    sc.run_steps(eng, range(3))                 # 매수 신호 → 보유. 신호봉 저점 100.3 이 손절선
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
    # 복원이 없으면 '방금 매수'로 오인해 그 봉 저점(100.1)이 손절선이 되어 이 종가에선 침묵한다.
    eng2.client.candles = eng2.client.candles + [sc.STEP_BARS[3]]
    eng2.evaluate("AAA", {"AAA": 100.2}, {"AAA": {"qty": 10.0, "avg": 100.8}})
    assert [s[0] for s in cap2.sent] == ["🔴 매도하세요"]
    # 감시 목록에 없는 종목의 저장 상태는 무시한다
    saved["state"]["ZZZ"] = {"state": "보유"}
    DBM.save_engine_status(store, [], [], saved["state"], {})
    eng3, _ = make_engine(monkeypatch, tmp_path, store=store)
    assert "ZZZ" not in eng3.state and eng3.state["AAA"] == "보유"


# --- 매수 신호는 정규장 봉에서만 ---------------------------------------------------

def test_entry_only_on_regular_session_bar(monkeypatch, tmp_path):
    def at_1545(market):
        return datetime(2026, 3, 25, 15, 45, tzinfo=TZ["KR"]).astimezone(TZ[market])
    # 신호봉이 15:31 (KR 정규장 마감 뒤 시간외 봉) → 조건이 다 맞아도 매수 신호 없음
    eng, cap = make_engine(monkeypatch, tmp_path, candles=sc.scenario_candles(14, 30))
    monkeypatch.setattr(E, "now_local", at_1545)
    monkeypatch.setattr(MH, "now_local", at_1545)
    eng.evaluate("AAA", {"AAA": 100.8}, {})
    assert cap.sent == [] and eng.snapshots["AAA"]["regular"] is False
    # 같은 모양이 15:29 에 끝나면(정규장) 매수 신호가 난다
    eng2, cap2 = make_engine(monkeypatch, tmp_path, candles=sc.scenario_candles(14, 28))
    monkeypatch.setattr(E, "now_local", at_1545)
    monkeypatch.setattr(MH, "now_local", at_1545)
    eng2.evaluate("AAA", {"AAA": 100.8}, {})
    assert [x[0] for x in cap2.sent] == ["🔵 매수하세요"] and eng2.snapshots["AAA"]["regular"] is True


# --- hold_only 종목은 청산이 잡힐 때까지 평가 대상에 남는다 ------------------------

def test_hold_only_stays_active_until_exit_is_noticed(monkeypatch, tmp_path):
    watch = {"AAA": {**sc.WATCHLIST["AAA"], "hold_only": True}}
    eng, cap = make_engine(monkeypatch, tmp_path, watchlist=watch)
    held = {"AAA": {"qty": 10.0, "avg": 100.0}}
    eng._last_holdings = held
    eng.evaluate("AAA", {"AAA": 100.9}, held)                  # 보유 전환
    eng.evaluate("AAA", {"AAA": 90.0}, held)                   # -10% → 손절 → 청산대기
    assert eng.state["AAA"] == "청산대기"
    eng._last_holdings = {}                                    # 청산됨
    assert eng._active_tickers(["AAA"]) == ["AAA"]             # 상태가 남아 있으니 한 사이클 더 본다
    eng.evaluate("AAA", {"AAA": 90.0}, {})
    assert eng.state["AAA"] == "관망" and cap.sent[-1][0] == "✅ 손절 완료"
    assert eng._active_tickers(["AAA"]) == []                  # 이제는 보유할 때까지 조회하지 않는다
    # 다시 사면 새 포지션이다 — 옛 손절 사유가 붙지 않고 익절 유예가 다시 시작된다
    eng.evaluate("AAA", {"AAA": 101.5}, {"AAA": {"qty": 5.0, "avg": 100.8}})
    assert eng.state["AAA"] == "보유" and "AAA" in eng.entry_at and cap.sent[-1][0] == "✅ 손절 완료"


# --- 청산대기 복귀: 근거가 사라지면 보유로 돌아간다 -------------------------------

def test_exit_wait_recovers_to_holding(monkeypatch, tmp_path):
    eng, cap = make_engine(monkeypatch, tmp_path)
    sc.run_steps(eng, range(4))                                # 매수 → 보유 → 신호봉 저점 100.3 이탈 → 매도하세요
    held = {"AAA": {"qty": 10.0, "avg": 100.8}}
    assert eng.state["AAA"] == "청산대기" and cap.sent[-1][0] == "🔴 매도하세요"
    floor = round(100.3 * (1 + eng.snapshots["AAA"]["band"] / 100), 4)
    eng.pending["AAA"]["next_at"] = "2000-01-01T00:00:00+00:00"
    push_bar(eng, floor - 0.01)
    eng.evaluate("AAA", {"AAA": floor - 0.01}, held)           # 종가가 매도선 위지만 밴드 여유 안 → 아직 청산대기 (반복은 '대기')
    assert eng.state["AAA"] == "청산대기" and cap.sent[-1][0] == "🔴 매도 대기하세요"
    push_bar(eng, floor + 0.01)
    eng.evaluate("AAA", {"AAA": floor + 0.01}, held)           # 종가가 밴드만큼 넘어 회복 → 보유 복귀
    assert eng.state["AAA"] == "보유" and "AAA" not in eng.pending
    assert cap.sent[-1][0] == "⚪ 청산 신호 해제" and "매도선 100.3" in cap.sent[-1][2]


def test_stop_recovers_only_past_hysteresis(monkeypatch, tmp_path):
    eng, cap = make_engine(monkeypatch, tmp_path)
    held = {"AAA": {"qty": 10.0, "avg": 100.0}}
    eng.evaluate("AAA", {"AAA": 100.9}, held)                  # 보유
    eng.evaluate("AAA", {"AAA": 94.0}, held)                   # -6% → 손절하세요
    assert cap.sent[-1][0] == "🔴 손절하세요" and eng.pending["AAA"]["kind"] == "STOP"
    eng.pending["AAA"]["next_at"] = "2000-01-01T00:00:00+00:00"
    eng.evaluate("AAA", {"AAA": 96.5}, held)                   # -3.5%: 한도 위지만 회복폭 2% 미달 → 반복 (손절은 언제나 확정 제목)
    assert eng.state["AAA"] == "청산대기" and cap.sent[-1][0] == "🔴 손절하세요"
    eng.evaluate("AAA", {"AAA": 100.9}, held)                  # -3% 넘게 회복 + 매도선 위 → 보유 복귀
    assert eng.state["AAA"] == "보유" and cap.sent[-1][0] == "⚪ 청산 신호 해제"


def test_exit_wait_repeat_backs_off(monkeypatch, tmp_path):
    eng, cap = make_engine(monkeypatch, tmp_path)
    sc.run_steps(eng, range(4))
    held = {"AAA": {"qty": 10.0, "avg": 100.8}}
    first = eng.pending["AAA"]
    assert first["repeats"] == 0 and first["next_at"] > datetime.now(timezone.utc).isoformat()
    n = len(cap.sent)
    eng.evaluate("AAA", {"AAA": 100.2}, held)                  # 15분 안 → 침묵 (알림기 쿨다운과 별개로 엔진이 막는다)
    assert len(cap.sent) == n
    first["next_at"] = "2000-01-01T00:00:00+00:00"
    eng.evaluate("AAA", {"AAA": 100.2}, held)
    assert len(cap.sent) == n + 1 and "다음 알림 30분 뒤" in cap.sent[-1][2]
    due = datetime.fromisoformat(first["next_at"]) - datetime.now(timezone.utc)
    assert timedelta(minutes=29) < due <= timedelta(minutes=30) and first["repeats"] == 1
    first["repeats"], first["next_at"] = 6, "2000-01-01T00:00:00+00:00"    # 여러 번 반복한 뒤엔 상한
    eng.evaluate("AAA", {"AAA": 100.2}, held)
    assert f"다음 알림 {E.EXIT_REPEAT_MAX_MIN}분 뒤" in cap.sent[-1][2]


# --- 마감 전 정리는 당일 청산 종목에만 -------------------------------------------

def test_close_warn_only_for_day_trade_symbols(monkeypatch, tmp_path):
    def at_1510(market):
        return datetime(2026, 3, 25, 15, 10, tzinfo=TZ["KR"]).astimezone(TZ[market])
    held = {"AAA": {"qty": 10.0, "avg": 100.0}}
    eng, cap = make_engine(monkeypatch, tmp_path)
    monkeypatch.setattr(E, "now_local", at_1510)
    monkeypatch.setattr(MH, "now_local", at_1510)
    eng.evaluate("AAA", {"AAA": 100.9}, held)
    assert cap.sent == []                                      # 오버나잇 종목엔 마감 정리를 말하지 않는다
    watch = {"AAA": {**sc.WATCHLIST["AAA"], "day_trade": True}}
    eng2, cap2 = make_engine(monkeypatch, tmp_path, watchlist=watch)
    monkeypatch.setattr(E, "now_local", at_1510)
    monkeypatch.setattr(MH, "now_local", at_1510)
    eng2.evaluate("AAA", {"AAA": 100.9}, held)
    assert [s[0] for s in cap2.sent] == ["🟠 마감 전 정리"]


# --- 진입대기 만료 -----------------------------------------------------------------

def test_pending_entry_expires(monkeypatch, tmp_path):
    eng, cap = make_engine(monkeypatch, tmp_path)
    eng.evaluate("AAA", {"AAA": 100.8}, {})
    assert eng.state["AAA"] == "진입대기" and eng.pending["AAA"]["bar_key"] == eng.snapshots["AAA"]["bar_key"]
    assert cap.sent[0][0] == "🔵 매수하세요" and cap.signals[0].kind == "ENTRY" and "확인 3/3" in cap.sent[0][2]
    eng.evaluate("AAA", {"AAA": 100.85}, {})                   # 15분 안 → 반복 없음
    assert len(cap.sent) == 1
    eng.pending["AAA"]["next_at"] = "2000-01-01T00:00:00+00:00"
    eng.evaluate("AAA", {"AAA": 100.85}, {})                   # 반복 알림은 '매수 대기하세요' (review, 자동매매 대상 아님)
    assert cap.sent[-1][0] == "🔵 매수 대기하세요" and cap.signals[-1].kind == "ENTRY_WATCH" and "아직 미진입" in cap.sent[-1][2]
    eng.pending["AAA"]["at"] = (datetime.now(timezone.utc) - timedelta(minutes=E.ENTRY_PENDING_MAX_MIN)).isoformat()
    eng.evaluate("AAA", {"AAA": 100.85}, {})
    assert eng.state["AAA"] == "관망" and "AAA" not in eng.pending
    assert cap.sent[-1][0] == "⚪ 매수 신호 만료" and f"{E.ENTRY_PENDING_MAX_MIN}분 안에 진입하지 않음" in cap.sent[-1][2]


def test_pending_entry_expires_when_session_ends(monkeypatch, tmp_path):
    def at_1545(market):
        return datetime(2026, 3, 25, 15, 45, tzinfo=TZ["KR"]).astimezone(TZ[market])
    eng, cap = make_engine(monkeypatch, tmp_path, candles=sc.scenario_candles(14, 28))   # 신호봉 15:29 (정규장)
    monkeypatch.setattr(E, "now_local", at_1545)
    monkeypatch.setattr(MH, "now_local", at_1545)
    eng.evaluate("AAA", {"AAA": 100.8}, {})
    assert eng.state["AAA"] == "진입대기"
    # 다음 봉(15:30)은 시간외 봉 — 마감 뒤 반복 알림 대신 만료
    last = datetime.fromisoformat(eng.client.candles[-1]["timestamp"])
    eng.client.candles = eng.client.candles + [bar(last + timedelta(minutes=1), 100.9, 1000, high=100.95, low=100.8)]
    eng.evaluate("AAA", {"AAA": 100.9}, {})
    assert eng.state["AAA"] == "관망" and cap.sent[-1][0] == "⚪ 매수 신호 만료" and "정규장 종료" in cap.sent[-1][2]


# --- 매수 신호는 개장 직후·마감 임박에 내지 않는다 ---------------------------------

def test_entry_blocked_right_after_open_and_near_close(monkeypatch, tmp_path):
    # 개장 1분 뒤 봉(09:01). 프로파일을 줘서 거래량 돌파가 성립하게 한다 — 그래도 VWAP 이 봉 두 개라 보류
    profile = {f"{h:02d}:{m:02d}": [1000.0] * 3 for h in range(8, 10) for m in range(60)}
    eng, cap = make_engine(monkeypatch, tmp_path, candles=sc.scenario_candles(8, 0))
    eng.volume_profile["AAA"] = profile
    eng.evaluate("AAA", {"AAA": 100.8}, {})
    assert cap.sent == [] and eng.snapshots["AAA"]["since_open"] == 1
    monkeypatch.setattr(E, "OPEN_EXCLUDE_MIN", 0)                                  # 제외 구간이 없으면 신호가 난다
    eng2, cap2 = make_engine(monkeypatch, tmp_path, candles=sc.scenario_candles(8, 0))
    eng2.volume_profile["AAA"] = profile
    eng2.evaluate("AAA", {"AAA": 100.8}, {})
    assert [s[0] for s in cap2.sent] == ["🔵 매수하세요"]
    # 마감 25분 전 (15:05, 신호봉 15:01) → 보류. 14:05 이면 정상 신호
    def at(h, m):
        return lambda market: datetime(2026, 3, 25, h, m, tzinfo=TZ["KR"]).astimezone(TZ[market])
    eng3, cap3 = make_engine(monkeypatch, tmp_path, candles=sc.scenario_candles(14, 0))
    monkeypatch.setattr(E, "now_local", at(15, 5))
    monkeypatch.setattr(MH, "now_local", at(15, 5))
    eng3.evaluate("AAA", {"AAA": 100.8}, {})
    assert cap3.sent == [] and eng3.snapshots["AAA"]["regular"] is True
    eng4, cap4 = make_engine(monkeypatch, tmp_path, candles=sc.scenario_candles(13, 0))
    monkeypatch.setattr(E, "now_local", at(14, 5))
    monkeypatch.setattr(MH, "now_local", at(14, 5))
    eng4.evaluate("AAA", {"AAA": 100.8}, {})
    assert [s[0] for s in cap4.sent] == ["🔵 매수하세요"]


# --- 매수 취소·매도 판정은 현재가 틱이 아니라 완성봉 종가 -----------------------------

def test_cancel_and_sell_judged_on_bar_close(monkeypatch, tmp_path):
    eng, cap = make_engine(monkeypatch, tmp_path)
    eng.evaluate("AAA", {"AAA": 100.8}, {})
    eng.pending["AAA"]["next_at"] = "2000-01-01T00:00:00+00:00"          # 반복 시각이 됐다고 치자
    eng.evaluate("AAA", {"AAA": 99.0}, {})                     # 현재가만 밴드 아래로 튐 → 취소 아님, 대기 반복
    assert eng.state["AAA"] == "진입대기" and "아직 미진입" in cap.sent[-1][2]
    push_bar(eng, 99.5)
    eng.evaluate("AAA", {"AAA": 99.5}, {})                     # 종가가 기준선 아래 → 취소
    assert eng.state["AAA"] == "관망" and cap.sent[-1][0] == "⚪ 매수 취소" and "종가 99.5" in cap.sent[-1][2]

    eng2, cap2 = make_engine(monkeypatch, tmp_path)
    sc.run_steps(eng2, range(3))                               # 보유, 손절선 100.3
    held = {"AAA": {"qty": 10.0, "avg": 100.8}}
    n = len(cap2.sent)
    eng2.evaluate("AAA", {"AAA": 99.0}, held)                  # 틱 이탈만으로는 매도 알림 없음
    assert eng2.state["AAA"] == "보유" and len(cap2.sent) == n
    push_bar(eng2, 100.2)                                      # 얕은 이탈(밴드 안), 거래량 없음 → 매도 대기
    eng2.evaluate("AAA", {"AAA": 100.2}, held)
    assert cap2.sent[-1][0] == "🔴 매도 대기하세요" and cap2.signals[-1].kind == "EXIT_WATCH"
    assert "종가 100.2가 매수 신호봉 저점 100.3" in cap2.sent[-1][2] and eng2.state["AAA"] == "청산대기"
    push_bar(eng2, 100.0)                                      # 밴드 폭보다 깊이 뚫림 → 매도 확정으로 승격
    eng2.evaluate("AAA", {"AAA": 100.0}, held)
    assert cap2.sent[-1][0] == "🔴 매도하세요" and cap2.signals[-1].kind == "SELL" and "대기 → 확정" in cap2.sent[-1][2]
    assert eng2.pending["AAA"]["kind"] == "SELL"


def test_shallow_break_with_volume_is_confirmed_sell(monkeypatch, tmp_path):
    eng, cap = make_engine(monkeypatch, tmp_path)
    sc.run_steps(eng, range(4))                                # 10:02 봉: 종가 100.2 (밴드 안) 이지만 거래량 3000 → 확정
    assert cap.signals[-1].kind == "SELL" and cap.sent[-1][0] == "🔴 매도하세요" and "매도 물량" in cap.sent[-1][2]


# --- 신호 없이 산 포지션의 손절선과 수익 구간 상향 ----------------------------------

def test_stop_ref_from_first_seen_bar_and_trailing(monkeypatch, tmp_path):
    eng, cap = make_engine(monkeypatch, tmp_path)
    held = {"AAA": {"qty": 10.0, "avg": 98.5}}
    eng.evaluate("AAA", {"AAA": 100.9}, held)                  # 우리 신호 없이 보유 발견
    assert eng.stop_ref["AAA"] == 100.3 and eng.stop_src["AAA"] == "보유 확인 봉 저점"
    eng.stop_ref["AAA"] = 99.0                                 # 저점이 낮았다고 치자
    push_bar(eng, 99.8)                                        # 수익 +1.3%, 종가가 밴드 하단(≈99.93) 아래 (얕은 이탈 → 대기)
    eng.evaluate("AAA", {"AAA": 99.8}, held)
    assert cap.sent[-1][0] == "🔴 매도 대기하세요" and "저점 99.0 에서 상향" in cap.sent[-1][2]


# --- 불타기는 매수와 같은 돌파 기준, 손절선 상향 ------------------------------------

def test_addon_needs_strong_bar_and_raises_stop(monkeypatch, tmp_path):
    eng, cap = make_engine(monkeypatch, tmp_path)
    eng.stop_ref["AAA"], eng.stop_src["AAA"] = 99.0, "매수 신호봉 저점"
    eng.evaluate("AAA", {"AAA": 100.9}, {"AAA": {"qty": 10.0, "avg": 98.0}})    # +2.96%, 강봉 돌파
    assert cap.sent[-1][0] == "🔵 추가매수 검토" and "매도선 100.3 로 상향" in cap.sent[-1][2]
    assert eng.stop_ref["AAA"] == 100.3 and eng.stop_src["AAA"] == "추가매수 봉 저점"
    weak = sc.scenario_candles()
    weak[-1] = bar(datetime(2026, 3, 25, 10, 1, tzinfo=TZ["KR"]), 100.35, 3000, high=100.9, low=100.3)   # 윗꼬리
    eng2, cap2 = make_engine(monkeypatch, tmp_path, candles=weak)
    eng2.evaluate("AAA", {"AAA": 100.9}, {"AAA": {"qty": 10.0, "avg": 98.0}})
    assert eng2.snapshots["AAA"]["breakout"] is False and cap2.sent == []


# --- 거래량 소진은 최근 몇 봉 평균으로 ----------------------------------------------

def test_rvol_recent_averages_last_bars(monkeypatch, tmp_path):
    from alertbot.indicators import rvol_at
    eng, _ = make_engine(monkeypatch, tmp_path)
    snap = eng._snapshot("AAA", {"AAA": 100.8})
    c = snap["candles"]
    expected = [rvol_at(c, i, "KR")[0] for i in range(len(c) - E.FADE_BARS, len(c))]
    assert snap["rvol_recent"] == round(sum(expected) / len(expected), 2) and snap["rvol_recent"] < snap["rvol"]


# --- 정규장 봉이 아니면 손절 한도 외의 보유 판단은 하지 않는다 -----------------------

def test_holding_signals_silent_outside_regular_except_stop(monkeypatch, tmp_path):
    def at_1545(market):
        return datetime(2026, 3, 25, 15, 45, tzinfo=TZ["KR"]).astimezone(TZ[market])
    held = {"AAA": {"qty": 10.0, "avg": 100.0}}
    eng, cap = make_engine(monkeypatch, tmp_path, candles=sc.scenario_candles(14, 30))   # 마지막 봉 15:31 시간외
    monkeypatch.setattr(E, "now_local", at_1545)
    monkeypatch.setattr(MH, "now_local", at_1545)
    eng.stop_ref["AAA"], eng.stop_src["AAA"] = 101.0, "매수 신호봉 저점"
    eng.evaluate("AAA", {"AAA": 100.5}, held)                  # 종가 100.8 < 101 이지만 시간외 → 침묵
    assert eng.state["AAA"] == "보유" and cap.sent == []
    eng.evaluate("AAA", {"AAA": 94.0}, held)                   # 손절 한도는 시간외에도 안전망
    assert cap.sent[-1][0] == "🔴 손절하세요"
    eng2, cap2 = make_engine(monkeypatch, tmp_path, candles=sc.scenario_candles(14, 28))  # 마지막 봉 15:29 정규장
    monkeypatch.setattr(E, "now_local", at_1545)
    monkeypatch.setattr(MH, "now_local", at_1545)
    eng2.stop_ref["AAA"], eng2.stop_src["AAA"] = 101.0, "매수 신호봉 저점"
    eng2.evaluate("AAA", {"AAA": 100.5}, held)
    assert cap2.sent[-1][0] == "🔴 매도하세요"


# --- EMA 역배열에서는 매수 신호를 내지 않는다 ------------------------------------------

def bear_candles():
    """60봉 하락(103→100.05) 뒤 강봉 돌파. EMA 9<20<50 역배열, 종가는 기준선 위."""
    t0 = datetime(2026, 3, 25, 9, 0, tzinfo=TZ["KR"])
    out = [bar(t0 + timedelta(minutes=i), round(103 - 0.05 * i, 2), 1000,
               high=round(103.1 - 0.05 * i, 2), low=round(102.9 - 0.05 * i, 2)) for i in range(60)]
    out.append(bar(t0 + timedelta(minutes=60), 100.1, 1500, high=100.2, low=100.0))
    out.append(bar(t0 + timedelta(minutes=61), 102.0, 3000, high=102.05, low=101.0))
    return out


def test_entry_skips_bear_ema(monkeypatch, tmp_path):
    assert E.ema_alignment(bear_candles()) == "역배열"
    eng, cap = make_engine(monkeypatch, tmp_path, candles=bear_candles())
    eng.evaluate("AAA", {"AAA": 102.0}, {})
    assert cap.sent == [] and eng.stats["AAA"].get("rvol") == 1 and eng.stats["AAA"].get("vwap") == 1
    monkeypatch.setattr(E, "ENTRY_SKIP_BEAR_EMA", False)
    eng2, cap2 = make_engine(monkeypatch, tmp_path, candles=bear_candles())
    eng2.evaluate("AAA", {"AAA": 102.0}, {})
    # 필터를 끄면 신호는 나지만 역배열·거래량 2.9배라 확인 항목이 모자라 '대기' 다
    assert [s[0] for s in cap2.sent] == ["🔵 매수 대기하세요"] and "EMA 역배열" in cap2.sent[0][2]
    assert cap2.signals[0].kind == "ENTRY_WATCH" and "EMA 정배열 ✗" in cap2.sent[0][2]


# --- 매수 확신도: 확인 항목이 모자라면 대기, 채워지면 승격 -----------------------------

def test_entry_watch_then_upgrade_when_confirmations_fill(monkeypatch, tmp_path):
    weak = sc.scenario_candles()
    weak[-1] = bar(datetime(2026, 3, 25, 10, 1, tzinfo=TZ["KR"]), 100.8, 2500, high=100.85, low=100.3)   # 2.4배: 요건은 되지만 강한 거래량 아님
    eng, cap = make_engine(monkeypatch, tmp_path, candles=weak)
    eng.evaluate("AAA", {"AAA": 100.8}, {})
    assert cap.signals[-1].kind == "ENTRY_WATCH" and cap.sent[-1][0] == "🔵 매수 대기하세요"
    assert "확인 2/3" in cap.sent[-1][2] and "거래량 3배↑ ✗" in cap.sent[-1][2] and eng.pending["AAA"]["strong"] is False
    push_bar(eng, 100.9, volume=4000, high=100.95, low=100.8)                 # 다음 봉에 거래량 3.9배가 붙음
    eng.evaluate("AAA", {"AAA": 100.9}, {})
    assert cap.signals[-1].kind == "ENTRY" and cap.sent[-1][0] == "🔵 매수하세요" and "승격" in cap.sent[-1][2]
    assert eng.pending["AAA"]["strong"] is True and eng.state["AAA"] == "진입대기"
    eng.pending["AAA"]["next_at"] = "2000-01-01T00:00:00+00:00"
    eng.evaluate("AAA", {"AAA": 100.9}, {})                                   # 그 뒤 반복은 다시 '대기' 제목
    assert cap.signals[-1].kind == "ENTRY_WATCH" and cap.sent[-1][0] == "🔵 매수 대기하세요"
