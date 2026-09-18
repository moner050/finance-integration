"""코인 신호 포지션 장부 — 진입 후보 뒤의 손절·보유 한도 청산 알림, 시황 진행 줄, 재시작 복원, 워커 연결(추가매수 제목)."""
from datetime import datetime, timedelta, timezone

from alertbot import db
from alertbot.binance_book import SignalBook, hold_text
from alertbot.binance_crash import CrashWorker
from alertbot.binance_follow import FollowWorker
from tests.test_binance_crash import STEP, Recorder, make_bars, make_h4
from tests.test_binance_follow import SHORT_STOP_MULT, SPEC_1D_SHORT, daily_short

T = datetime(2026, 9, 16, 3, 0, tzinfo=timezone.utc)      # 12:00 KST


def make_book(store=None, mark=None):
    q = {"mark": mark}
    rec = Recorder()
    book = SignalBook(store, rec, fetch_mark=lambda s: q["mark"])
    return book, rec, q


def test_long_signal_stops_on_mark_and_shows_progress():
    store = db.DB.sqlite().init_schema()
    book, rec, q = make_book(store, mark=7.05)
    book.opened("CRASH_BUY", "ETCUSDT", "long", "ETCUSDT 5분봉", "급락 매수 5분봉", 7.1, 6.887, 8, T)
    assert book.is_open("CRASH_BUY", "ETCUSDT") and not book.is_open("CRASH_BUY", "BTCUSDT")
    assert book.status_line("CRASH_BUY", "ETCUSDT", T) == "🔵 매수 신호 진행 중 — 신호가 7.100, 손절 6.887"     # 마크 조회 전
    assert book.poll(T + timedelta(minutes=5)) == [] and rec.sent == []         # 손절선 위, 한도 전 → 침묵
    line = book.status_line("CRASH_BUY", "ETCUSDT", T)
    assert line == "🔵 매수 신호 진행 중 — 신호가 7.100 대비 -0.70%, 손절 6.887"
    q["mark"] = 6.88
    sent = book.poll(T + timedelta(hours=2, minutes=18))
    assert [s.kind for s in sent] == ["SELL"] and sent[0].title == "🔴 손절하세요" and sent[0].label == "ETCUSDT 5분봉"
    assert sent[0].symbol == "ETCUSDT" and sent[0].severity == "action"
    body = sent[0].body
    assert body == "신호가 7.100 대비 -3.10% (2.3시간)\n마크 6.880 이 손절선 6.887 에 닿음"        # 주식 청산 알림처럼 첫 줄이 신호가 대비
    assert not book.is_open("CRASH_BUY", "ETCUSDT") and book.status_line("CRASH_BUY", "ETCUSDT", T) is None
    assert SignalBook(store, rec).open == {}                                     # 닫힌 신호는 저장에서도 빠진다


def test_time_exit_names_profit_or_cleanup_and_handles_short():
    book, rec, q = make_book(mark=7.3)
    book.opened("CRASH_BUY", "ETCUSDT", "long", "ETCUSDT 5분봉", "급락 매수 5분봉", 7.1, 6.887, 8, T)
    assert book.poll(T + timedelta(hours=7, minutes=59)) == []
    sent = book.poll(T + timedelta(hours=8))
    assert sent[0].kind == "EXIT_FULL" and sent[0].title == "🟢 전량 익절하세요"
    assert sent[0].body == "신호가 7.100 대비 +2.82% (8.0시간)\n보유 한도 8시간 도달"
    q["mark"] = 7.0
    book.opened("CRASH_BUY", "ETCUSDT", "long", "ETCUSDT 5분봉", "급락 매수 5분봉", 7.1, 6.887, 8, T)
    assert book.poll(T + timedelta(hours=9))[0].title == "🟢 전량 정리하세요"
    # 숏: 손절선은 위, 손익 부호는 반대
    q["mark"] = 16.0
    book.opened("CRASH_SHORT_1D", "ETCUSDT", "short", "ETCUSDT 일봉", "급락 추종 일봉", 15.5, 19.375, 480, T)
    assert book.poll(T) == [] and book.status_line("CRASH_SHORT_1D", "ETCUSDT", T).startswith("🔴 숏 신호 진행 중 — 신호가 15.500 대비 -3.23%")
    q["mark"] = 19.4
    sent = book.poll(T + timedelta(days=1))
    assert sent[0].kind == "SELL" and sent[0].title == "🔴 숏 손절하세요" and "신호가 15.500 대비 -25.16% (24.0시간)" in sent[0].body
    q["mark"] = 14.0
    book.opened("CRASH_SHORT_1D", "ETCUSDT", "short", "ETCUSDT 일봉", "급락 추종 일봉", 15.5, 19.375, 480, T)
    sent = book.poll(T + timedelta(days=20))
    assert sent[0].title == "🟢 숏 전량 익절하세요" and "보유 한도 20일 도달" in sent[0].body
    assert hold_text(168) == "7일" and hold_text(8) == "8시간" and hold_text(30) == "30시간"


def test_book_survives_restart_and_price_failure():
    store = db.DB.sqlite().init_schema()
    book, rec, q = make_book(store, mark=None)
    book.opened("SURGE_ENTRY", "BTCUSDT", "long", "BTCUSDT 4시간봉", "급등 추종 4시간봉", 77200.0, 72000.0, 168, T)

    def boom(s):
        raise RuntimeError("timeout")
    book.fetch_mark = boom
    assert book.poll(T + timedelta(days=8)) == [] and book.is_open("SURGE_ENTRY", "BTCUSDT")   # 시세 실패면 판정 보류
    again = SignalBook(store, rec, fetch_mark=lambda s: 80000.0)
    assert again.is_open("SURGE_ENTRY", "BTCUSDT") and again.open["SURGE_ENTRY:BTCUSDT"]["deadline"] == "2026-09-23T03:00:00+00:00"
    sent = again.poll(T + timedelta(days=7))
    assert sent[0].title == "🟢 전량 익절하세요" and sent[0].body == "신호가 77,200.0 대비 +3.63% (7.0일)\n보유 한도 7일 도달"


def test_crash_worker_registers_signal_and_calls_repeat_an_addon():
    crash = make_bars(crash_bars=20)
    fetched = {"ETCUSDT": crash, "BTCUSDT": make_bars(close=78000)}
    book, rec, q = make_book(mark=7.0)
    w = CrashWorker(["ETCUSDT"], rec, fetch_bars=lambda s: fetched[s], fetch_fund=lambda s: None, fetch_h4=lambda s: make_h4(), book=book)
    t = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    sent = w.poll_once(t)
    assert sent[0].title == "🔵 급락 매수 후보" and "추가" not in sent[0].body
    pos = book.open["CRASH_BUY:ETCUSDT"]
    assert pos["price"] == crash[-1]["close"] and abs(pos["stop"] - crash[-1]["close"] * 0.97) < 1e-9 and pos["hold_hours"] == 8
    assert pos["label"] == "ETCUSDT 5분봉" and pos["deadline"] == "2026-09-15T20:00:00+00:00"
    line = w.status_lines(t)[0]
    assert line.startswith("급락 매수 5분봉 ETCUSDT  🔵 매수 신호 진행 중 — 신호가") and "부족" not in line
    # 쿨다운 뒤 또 급락 봉 → 신호가 아직 진행 중이니 '추가매수 후보'. 장부(보유 한도)는 이 봉 기준으로 갱신된다
    nxt = crash + [dict(crash[-1], open_time=crash[-1]["open_time"] + STEP, close_time=crash[-1]["close_time"] + STEP)]
    fetched["ETCUSDT"] = nxt
    sent = w.poll_once(t + timedelta(minutes=61))
    assert sent[0].title == "🔵 급락 추가매수 후보" and "진행 중 신호에 추가" in sent[0].body
    assert book.open["CRASH_BUY:ETCUSDT"]["deadline"] == "2026-09-15T21:01:00+00:00"
    # 손절로 신호가 닫히면 시황은 조건 줄로 돌아가고, 다음 급락은 다시 '매수 후보' 다
    q["mark"] = 6.0
    assert book.poll(t + timedelta(minutes=62))[0].title == "🔴 손절하세요"
    assert "신호 진행 중" not in w.status_lines(t + timedelta(minutes=62))[0]
    fetched["ETCUSDT"] = nxt + [dict(nxt[-1], open_time=nxt[-1]["open_time"] + STEP, close_time=nxt[-1]["close_time"] + STEP)]
    assert w.poll_once(t + timedelta(minutes=122))[0].title == "🔵 급락 매수 후보"


def test_follow_worker_registers_short_entry():
    bars = daily_short()
    book, rec, q = make_book(mark=15.0)
    ws = FollowWorker(SPEC_1D_SHORT, rec, fetch_bars=lambda s, iv, n: bars, fetch_fund=lambda s: None, book=book)
    assert [s.kind for s in ws.poll_once(T)] == ["CRASH_SHORT_1D"]
    pos = book.open["CRASH_SHORT_1D:ETCUSDT"]
    assert pos["side"] == "short" and pos["price"] == 15.5 and abs(pos["stop"] - 15.5 * SHORT_STOP_MULT) < 1e-9 and pos["hold_hours"] == 480
    assert pos["label"] == "ETCUSDT 일봉" and pos["name"] == "급락 추종 일봉"
    book.poll(T)
    line = ws.status_lines(T)[0]
    assert line == ("급락 추종 일봉 ETCUSDT  🔴 숏 신호 진행 중 — 신호가 15.500 대비 +3.23%, "
                    f"손절 {15.5 * SHORT_STOP_MULT:.3f}")
    # 관찰(watch) 단계는 장부에 올리지 않는다
    from alertbot.binance_follow import evaluate
    watch_i = next(k for k in range(400, 410) if (x := evaluate(bars[:k + 1], SPEC_1D_SHORT)) and x["stage"] == "watch")
    book2, rec2, _ = make_book()
    w2 = FollowWorker(SPEC_1D_SHORT, rec2, fetch_bars=lambda s, iv, n: bars[:watch_i + 1], fetch_fund=lambda s: None, book=book2)
    assert [s.kind for s in w2.poll_once(T)] == ["CRASH_WATCH_1D"] and book2.open == {}
