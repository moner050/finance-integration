"""저장소 테스트 — 메모리 SQLite 로 같은 함수를 돌린다 (MySQL 방언은 upsert 문만 다르다)."""
import pytest

from alertbot import db as DBM
from alertbot.models import Signal


def fresh():
    return DBM.DB.sqlite().init_schema()


SEED = {
    "SOXX": {"market": "US", "leaders": ["NVDA", "AVGO"], "inverse": False, "pair": None,
             "note": "레버리지 진입 시 SOXL"},
    "SOXL": {"market": "US", "leaders": None, "inverse": False, "pair": "SOXS", "hold_only": True},
    "005930": {"market": "KR", "leaders": None, "inverse": False, "pair": None, "name": "삼성전자"},
}


def test_watchlist_roundtrip():
    d = fresh()
    assert DBM.seed_watchlist(d, SEED) == 3
    assert DBM.seed_watchlist(d, SEED) == 0                    # 있으면 건너뛴다
    wl = DBM.load_watchlist(d)
    assert wl["SOXX"] == {"market": "US", "leaders": ["NVDA", "AVGO"], "inverse": False, "pair": None,
                          "hold_only": False, "name": None, "note": "레버리지 진입 시 SOXL",
                          "auto_trade": False, "auto_amount": 0.0, "day_trade": False}
    assert wl["SOXL"]["hold_only"] is True and wl["SOXL"]["pair"] == "SOXS" and wl["SOXL"]["leaders"] is None
    assert wl["005930"]["name"] == "삼성전자"

    v1 = DBM.watchlist_version(d)
    DBM.set_enabled(d, "SOXL", False)
    assert "SOXL" not in DBM.load_watchlist(d)
    assert "SOXL" in DBM.load_watchlist(d, enabled_only=False)
    v2 = DBM.watchlist_version(d)
    assert v2 != v1

    DBM.upsert_watch(d, " aapl ", "US", leaders=["msft", " "], pair="", note="")
    row = DBM.get_watch_row(d, "AAPL")
    assert row["leaders"] == ["MSFT"] and row["pair"] is None and row["note"] is None
    assert DBM.load_watchlist(d)["AAPL"]["leaders"] == ["MSFT"]
    v3 = DBM.watchlist_version(d)                               # 4행
    assert v3 != v2
    DBM.delete_watch(d, "AAPL")
    assert DBM.get_watch_row(d, "AAPL") is None
    assert DBM.watchlist_version(d) != v3                       # 삭제는 행 수로 잡힌다
    assert [r["symbol"] for r in DBM.list_watch_rows(d)] == ["005930", "SOXL", "SOXX"]

    with pytest.raises(ValueError):
        DBM.upsert_watch(d, "X", "JP")
    with pytest.raises(ValueError):
        DBM.upsert_watch(d, "  ", "US")


def test_engine_status_and_signal_log():
    d = fresh()
    assert DBM.load_engine_status(d) is None
    DBM.save_engine_status(d, ["SOXX"], [], {"SOXX": {"state": "보유"}}, {"SOXX": {"price": 1.0}})
    DBM.save_engine_status(d, ["SOXX"], ["OKLO"], {"SOXX": {"state": "관망"}}, {}, last_error="x")
    st = DBM.load_engine_status(d)
    assert st["pre"] == ["OKLO"] and st["state"]["SOXX"]["state"] == "관망"
    assert st["last_error"] == "x" and st["heartbeat_at"]

    DBM.log_signal(d, Signal("ENTRY", "🔵 매수하세요", "테스트", "본문", "SOXX"), {"telegram": "ok", "log": "skip"})
    DBM.log_signal(d, Signal("SUMMARY", "📊 시황", "10:00", "b"), {"telegram": "ok"})
    rows = DBM.recent_signals(d, limit=10)
    assert [r["kind"] for r in rows] == ["SUMMARY", "ENTRY"]
    assert rows[1]["results"] == {"telegram": "ok", "log": "skip"} and rows[1]["severity"] == "action"
    assert [r["kind"] for r in DBM.recent_signals(d, symbol="SOXX")] == ["ENTRY"]
    assert [r["kind"] for r in DBM.recent_signals(d, severity="info")] == ["SUMMARY"]
    assert DBM.recent_signals(d, limit=1)[0]["kind"] == "SUMMARY"


def test_settings_and_orders():
    d = fresh()
    st = DBM.get_settings(d)
    assert "autotrade_enabled" not in st and st["max_positions"] == "3"          # live 스위치는 계정별 (alert_accounts)
    DBM.set_setting(d, "max_positions", "5")
    assert DBM.get_settings(d)["max_positions"] == "5"
    for key in ("nope", "autotrade_enabled", "binance_trade_enabled"):
        with pytest.raises(ValueError):
            DBM.set_setting(d, key, 1)

    DBM.upsert_watch(d, "AAA", "KR", auto_trade=True, auto_amount=500000)
    assert DBM.load_watchlist(d)["AAA"]["auto_trade"] is True and DBM.load_watchlist(d)["AAA"]["auto_amount"] == 500000.0

    row = {"intent_id": "AAA-1", "mode": "dry", "symbol": "AAA", "market": "KR", "side": "BUY", "kind": "ENTRY",
           "order_type": "LIMIT", "price": 100.0, "quantity": 10, "amount": 1000.0, "bar_key": "t1", "ref_avg": None,
           "status": "sent", "reason": None, "order_id": "o1", "filled_qty": 0, "avg_price": None, "pnl": None,
           "created_at": "2026-09-09T01:00:00+00:00", "updated_at": None}
    DBM.insert_order(d, row)
    assert [o["intent_id"] for o in DBM.open_orders(d)] == ["AAA-1"]
    assert DBM.open_orders(d, "BBB") == []
    DBM.update_order(d, "AAA-1", status="filled", filled_qty=10, avg_price=99.5)
    got = DBM.get_order(d, "AAA-1")
    assert got["status"] == "filled" and got["avg_price"] == 99.5 and got["updated_at"]
    assert DBM.open_orders(d) == []
    assert len(DBM.orders_since(d, "2026-09-09T00:00:00+00:00", mode="dry")) == 1
    assert DBM.orders_since(d, "2026-09-10T00:00:00+00:00") == []
    assert DBM.recent_orders(d, 5)[0]["price"] == 100.0


def test_reset_paper_backs_up_and_clears_only_dry(tmp_path):
    d = fresh()
    base = {"symbol": "AAA", "market": "KR", "side": "BUY", "kind": "ENTRY", "order_type": "LIMIT", "price": 100.0,
            "quantity": 1, "amount": 100.0, "bar_key": None, "ref_avg": None, "status": "filled", "reason": None,
            "order_id": "o", "filled_qty": 1, "avg_price": 100.0, "pnl": None, "created_at": "2026-09-09T01:00:00+00:00",
            "updated_at": None}
    DBM.insert_order(d, {**base, "intent_id": "dry-1", "mode": "dry"})
    DBM.insert_order(d, {**base, "intent_id": "live-1", "mode": "live"})
    DBM.insert_binance_position(d, {"mode": "dry", "strategy": "CRASH_BUY", "symbol": "ETCUSDT", "side": "long", "qty": 1,
                                    "entry_price": 7.0, "notional": 7.0, "leverage": 2, "stop": 6.8,
                                    "deadline": "2026-09-09T09:00:00+00:00", "status": "open",
                                    "opened_at": "2026-09-09T01:00:00+00:00", "funding": 0})
    DBM.save_engine_status(d, [], [], {"AAA": {"state": "보유", "last_seen": {"avg": 90, "qty": 380}},
                                       "BBB": {"state": "관망"}}, {})
    (tmp_path / "trade_log.csv").write_text("closed_at\n", encoding="utf-8-sig")

    out = DBM.reset_paper(d, tmp_path, stamp="t1")
    assert out == {"alert_orders": 1, "alert_binance_positions": 1, "last_seen": 1, "trade_log": "trade_log.paper-bak-t1.csv"}
    assert [o["intent_id"] for o in DBM.recent_orders(d)] == ["live-1"]                 # live 는 남는다
    assert DBM.binance_positions(d) == []
    assert d.fetchone("SELECT COUNT(*) AS n FROM alert_orders_paper_bak_t1")["n"] == 1
    assert d.fetchone("SELECT COUNT(*) AS n FROM alert_binance_positions_paper_bak_t1")["n"] == 1
    assert "last_seen" not in DBM.load_engine_status(d)["state"]["AAA"]
    assert not (tmp_path / "trade_log.csv").exists() and (tmp_path / "trade_log.paper-bak-t1.csv").exists()
    assert DBM.reset_paper(d, tmp_path, stamp="t2") == {"alert_orders": 0, "alert_binance_positions": 0, "last_seen": 0,
                                                         "trade_log": None}


def test_books_are_scoped_by_account():
    """주문·포지션·신호 이력은 장부(account_id)로 갈린다 — None 은 공용 가상 장부, 숫자는 그 계정."""
    d = fresh()
    base = {"symbol": "AAA", "market": "KR", "side": "BUY", "kind": "ENTRY", "order_type": "LIMIT", "price": 100.0,
            "quantity": 1, "amount": 100.0, "bar_key": "b", "ref_avg": None, "status": "open", "reason": None,
            "order_id": "o", "filled_qty": 0, "avg_price": None, "pnl": None, "created_at": "2026-09-09T01:00:00+00:00",
            "updated_at": None}
    DBM.insert_order(d, {**base, "intent_id": "v", "mode": "dry"})
    DBM.insert_order(d, {**base, "intent_id": "a7", "mode": "live", "account_id": 7})
    DBM.insert_order(d, {**base, "intent_id": "a8", "mode": "live", "account_id": 8})
    assert [o["intent_id"] for o in DBM.open_orders(d)] == ["v"]
    assert [o["intent_id"] for o in DBM.open_orders(d, account_id=7)] == ["a7"]
    assert [o["intent_id"] for o in DBM.orders_since(d, "2026-09-09T00:00:00+00:00", "live", 8)] == ["a8"]
    assert sorted(o["intent_id"] for o in DBM.recent_orders(d, account_ids=[None, 7])) == ["a7", "v"]
    assert len(DBM.recent_orders(d)) == 3 and DBM.recent_orders(d, account_ids=[]) == []
    DBM.update_order(d, "v", status="filled", filled_qty=1, avg_price=100.0)
    DBM.update_order(d, "a7", status="filled", filled_qty=1, avg_price=100.0)
    assert DBM.dry_positions(d) == {"AAA": {"qty": 1.0, "avg": 100.0, "market": "KR"}}     # live 체결은 가상 보유에 섞이지 않는다

    DBM.log_signal(d, Signal("ORDER_FILLED", "✅ 체결", "AAA", "b", "AAA", account_id=7), {"telegram_account": "ok"})
    DBM.log_signal(d, Signal("ENTRY", "🔵 매수하세요", "AAA", "b", "AAA"), {"telegram_public": "ok"})
    assert [r["kind"] for r in DBM.recent_signals(d, account_ids=[None, 8])] == ["ENTRY"]
    assert [r["account_id"] for r in DBM.recent_signals(d)] == [None, 7]
    row = {"strategy": "CRASH_BUY", "symbol": "ETCUSDT", "side": "long", "qty": 1, "entry_price": 7.0, "notional": 7.0,
           "leverage": 2, "stop": 6.8, "deadline": "2026-09-09T09:00:00+00:00", "funding": 0, "status": "closed",
           "opened_at": "2026-09-09T01:00:00+00:00"}
    DBM.insert_binance_position(d, {**row, "mode": "dry"})
    pid = DBM.insert_binance_position(d, {**row, "mode": "live", "account_id": 7})
    DBM.update_binance_position(d, pid, pnl=-5.0, closed_at="2026-09-09T02:00:00+00:00")
    assert DBM.binance_pnl_since(d, "2026-09-09T00:00:00+00:00", "live", 7) == -5.0
    assert DBM.binance_pnl_since(d, "2026-09-09T00:00:00+00:00", "live") == 0.0
    assert [p["account_id"] for p in DBM.binance_positions(d, account_ids=[None])] == [None]
