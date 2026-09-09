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
                          "hold_only": False, "name": None, "note": "레버리지 진입 시 SOXL"}
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

    DBM.log_signal(d, Signal("ENTRY", "🔵 매수하세요", "테스트", "본문", "SOXX"), {"telegram": "ok", "whatsapp": "skip"})
    DBM.log_signal(d, Signal("SUMMARY", "📊 시황", "10:00", "b"), {"telegram": "ok"})
    rows = DBM.recent_signals(d, limit=10)
    assert [r["kind"] for r in rows] == ["SUMMARY", "ENTRY"]
    assert rows[1]["results"] == {"telegram": "ok", "whatsapp": "skip"} and rows[1]["severity"] == "action"
    assert [r["kind"] for r in DBM.recent_signals(d, symbol="SOXX")] == ["ENTRY"]
    assert [r["kind"] for r in DBM.recent_signals(d, severity="info")] == ["SUMMARY"]
    assert DBM.recent_signals(d, limit=1)[0]["kind"] == "SUMMARY"
