"""신호 포지션 모의 성적 — CSV 기록과 하루치 집계(승률·평균·합계·손익비·건별·진행 중), 코인 장부의 기록과 성적표."""
from datetime import datetime, timedelta, timezone

from alertbot.binance_book import SignalBook
from alertbot.tracking import SignalTradeLog, fmt_num, hold_min_text
from tests.test_binance_crash import Recorder

T = datetime(2026, 9, 16, 3, 0, tzinfo=timezone.utc)   # 12:00 KST = 09-15 23:00 뉴욕


def test_log_records_and_summarizes_per_market_day(tmp_path):
    log = SignalTradeLog(tmp_path / "s.csv")
    assert log.daily_summary("KR") == ""
    log.add("009150", "삼성전기", "KR", (T - timedelta(minutes=42)).isoformat(), 1344000.0, 1360000.0, 1.19, "익절", T)
    log.add("005930", "삼성전자", "KR", (T - timedelta(hours=3)).isoformat(), 251000.0, 248000.0, -1.2, "손절", T)
    log.add("OKLO", "오클로", "US", (T - timedelta(hours=1)).isoformat(), 51.25, 52.0, 1.46, "매도", T)   # 다른 시장은 섞이지 않는다
    rows = log.rows_on("KR", "2026-09-16")
    assert [r["label"] for r in rows] == ["삼성전기", "삼성전자"] and rows[0]["hold_min"] == 42.0
    lines = log.daily_summary("KR", ["삼성SDI 신호가 400,000 → 현재 404,000 +1.00%"], "2026-09-16").splitlines()
    assert lines[0] == "청산 2건: 1익절 1손절 (승률 50%)"
    assert lines[1] == "평균 -0.01% · 합계 -0.01%  (건당 같은 금액 기준)"
    assert lines[2] == "손익비 1:0.99  (평균 익절 +1.19% / 평균 손절 -1.20%)"
    assert lines[3] == "· 11:18 삼성전기 1,344,000 → 1,360,000 +1.19% 익절 (42분)"
    assert lines[4] == "· 09:00 삼성전자 251,000 → 248,000 -1.20% 손절 (3.0시간)"
    assert lines[5] == "진행 중 1건 — 청산 신호가 나올 때까지 계속 관리" and lines[6] == "· 삼성SDI 신호가 400,000 → 현재 404,000 +1.00%"
    assert lines[-1].startswith("※ 신호가 → 청산 신호 시점")
    assert log.daily_summary("US", (), "2026-09-15").startswith("청산 1건: 1익절 0손절 (승률 100%)")
    # 청산이 없어도 진행 중이 있으면 보낸다. 다른 날짜는 비어 있다
    assert log.daily_summary("KR", ["x"], "2026-09-15").startswith("오늘 청산된 신호 없음\n진행 중 1건")
    assert log.daily_summary("KR", [], "2026-09-15") == ""
    assert fmt_num(1344000.0) == "1,344,000" and fmt_num(7.1) == "7.1" and fmt_num(0.00012) == "0.00012"
    assert hold_min_text(42) == "42분" and hold_min_text(180) == "3.0시간"


def test_book_records_closed_signals_and_daily_report(tmp_path):
    trades = SignalTradeLog(tmp_path / "coin.csv")
    q = {"ETCUSDT": 7.3, "BTCUSDT": 80000.0}
    book = SignalBook(None, Recorder(), fetch_mark=lambda s: q[s], trades=trades)
    book.opened("CRASH_BUY", "ETCUSDT", "long", "ETCUSDT 5분봉", "급락 매수 5분봉", 7.1, 6.887, 8, T)
    book.opened("CRASH_SHORT_1D", "BTCUSDT", "short", "BTCUSDT 일봉", "급락 추종 일봉", 77200.0, 96500.0, 480, T)
    assert book.daily_report("2026-09-16").startswith("오늘 청산된 신호 없음\n진행 중 2건")
    sent = book.poll(T + timedelta(hours=8))                       # 롱은 보유 한도 → 익절, 숏은 아직
    assert [s.title for s in sent] == ["🟢 전량 익절하세요"]
    rows = trades.rows_on("BINANCE", "2026-09-16")
    assert [(r["label"], r["entry"], r["exit"], r["pnl"], r["reason"], r["hold_min"]) for r in rows] == \
        [("ETCUSDT 급락 매수 5분봉", 7.1, 7.3, 2.82, "익절", 480.0)]
    lines = book.daily_report("2026-09-16").splitlines()
    assert lines[0] == "청산 1건: 1익절 0손절 (승률 100%)"
    assert lines[2] == "· 12:00 ETCUSDT 급락 매수 5분봉 7.1 → 7.3 +2.82% 익절 (8.0시간)"
    assert lines[3] == "진행 중 1건 — 청산 신호가 나올 때까지 계속 관리"
    assert lines[4] == "· BTCUSDT 급락 추종 일봉 숏 신호가 77,200.0 → 마크 80,000.0 -3.63%"
    assert SignalBook(None, Recorder()).daily_report() == ""      # 기록기 없는 장부는 성적표가 없다
