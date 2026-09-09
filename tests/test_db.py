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
                          "auto_trade": False, "auto_amount": 0.0}
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
    assert st["autotrade_enabled"] == "0" and st["max_positions"] == "3"
    DBM.set_setting(d, "autotrade_enabled", 1)
    DBM.set_setting(d, "max_positions", "5")
    assert DBM.get_settings(d)["autotrade_enabled"] == "1" and DBM.get_settings(d)["max_positions"] == "5"
    with pytest.raises(ValueError):
        DBM.set_setting(d, "nope", 1)

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
