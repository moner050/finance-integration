"""백오피스 — 라우트와 DB 연동. 메모리 SQLite 를 앱의 저장소로 끼운다."""
import pytest
from fastapi.testclient import TestClient

import alertbot.backoffice.app as A
from alertbot import db as DBM
from alertbot.models import Signal
from tests.test_notify import Recorder


@pytest.fixture
def client(monkeypatch):
    store = DBM.DB.sqlite().init_schema()
    DBM.seed_watchlist(store, {
        "SOXX": {"market": "US", "leaders": ["NVDA"], "inverse": False, "pair": None},
        "SOXL": {"market": "US", "leaders": None, "inverse": False, "pair": "SOXS", "hold_only": True},
    })
    monkeypatch.setattr(A, "_store", store)
    return TestClient(A.app), store


def test_status_without_engine(client):
    c, _ = client
    r = c.get("/")
    assert r.status_code == 200 and "엔진 상태 기록이 없다" in r.text


def test_status_with_engine(client):
    c, store = client
    DBM.save_engine_status(store, ["SOXX"], [], {"SOXX": {"state": "보유", "stop_ref": 10.5}},
                           {"SOXX": {"label": "SOXX", "market": "US", "price": 11.0, "vwap": 10.8,
                                     "rvol": 2.1, "at": "2026-09-09T00:00:00+00:00"}})
    r = c.get("/partials/status")
    assert r.status_code == 200 and "보유" in r.text and "10.5" in r.text
    assert "초 전)" in r.text and "멈췄을 수" not in r.text          # heartbeat 가 방금이라 정상
    r = c.get("/")
    assert 'hx-get="/partials/status"' in r.text


def test_watchlist_crud(client):
    c, store = client
    r = c.get("/watchlist")
    assert "SOXX" in r.text and "SOXS" in r.text and 'class="warn" title=' in r.text   # SOXS 가 목록에 없어 페어 경고

    r = c.post("/watchlist", data={"symbol": "aapl", "market": "US", "name": "애플", "leaders": "msft, googl",
                                   "inverse": "1", "pair": "", "note": "", "enabled": "1"}, follow_redirects=False)
    assert r.status_code == 303
    row = DBM.get_watch_row(store, "AAPL")
    assert row["leaders"] == ["MSFT", "GOOGL"] and row["inverse"] == 1 and row["enabled"] == 1 and row["name"] == "애플"

    c.post("/watchlist/AAPL/toggle", follow_redirects=False)
    assert DBM.get_watch_row(store, "AAPL")["enabled"] == 0
    r = c.get("/watchlist?edit=aapl")
    assert "수정 저장" in r.text and "애플" in r.text and "MSFT,GOOGL" in r.text

    r = c.post("/watchlist", data={"symbol": "AAPL", "market": "JP"})
    assert r.status_code == 400 and "US 또는 KR" in r.text

    r = c.post("/watchlist", data={"symbol": "AAPL", "market": "KR", "name": "", "leaders": ""}, follow_redirects=False)
    assert DBM.get_watch_row(store, "AAPL")["enabled"] == 0        # 체크 안 하면 비활성 저장
    c.post("/watchlist/AAPL/delete", follow_redirects=False)
    assert DBM.get_watch_row(store, "AAPL") is None


def test_validate_uses_toss_prices(client, monkeypatch):
    c, _ = client

    class FakeToss:
        def __init__(self, *a):
            pass

        def get_prices(self, symbols):
            return {"AAPL": 190.5}
    monkeypatch.setattr(A, "TossReadOnlyClient", FakeToss)
    r = c.post("/watchlist/validate", data={"symbol": "aapl", "leaders": "zzzz"})
    assert "AAPL: ✅ 190.5" in r.text and "ZZZZ: ❌" in r.text
    assert "입력하세요" in c.post("/watchlist/validate", data={"symbol": " "}).text

    class Broken:
        def __init__(self, *a):
            raise SystemExit("403 — 허용 IP 미등록")
    monkeypatch.setattr(A, "TossReadOnlyClient", Broken)
    assert "조회 실패: 403" in c.post("/watchlist/validate", data={"symbol": "AAPL"}).text


def test_signals_page(client):
    c, store = client
    DBM.log_signal(store, Signal("ENTRY", "🔵 매수하세요", "SOXX", "현재가 1", "SOXX"), {"telegram": "ok", "whatsapp": "skip"})
    r = c.get("/signals?severity=action")
    assert "🔵 매수하세요" in r.text and "telegram: ok" in r.text and "whatsapp: skip" in r.text
    assert "기록이 없다" in c.get("/signals?severity=info").text
    assert "🔵 매수하세요" in c.get("/signals?symbol=soxx").text


def test_channel_test_send(client, monkeypatch):
    c, store = client
    rec = Recorder("telegram", "review")
    monkeypatch.setattr(A, "build_channels", lambda: [rec])
    r = c.post("/channels/telegram/test")
    assert "ok" in r.text and len(rec.got) == 1 and rec.got[0].kind == "SYSTEM"
    assert DBM.recent_signals(store)[0]["kind"] == "SYSTEM"
    assert "설정되어 있지 않다" in c.post("/channels/whatsapp/test").text
    r = c.get("/channels")
    assert r.status_code == 200 and "telegram" in r.text and "whatsapp" in r.text
