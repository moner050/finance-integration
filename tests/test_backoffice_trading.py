"""백오피스 자동매매 화면 — 킬 스위치, 한도, 주문 취소, 권한 확인, 워치리스트 자동 필드."""
import pytest
from fastapi.testclient import TestClient

import alertbot.backoffice.app as A
from alertbot import db as DBM
from alertbot.trading.broker import BrokerError


@pytest.fixture
def client(monkeypatch):
    store = DBM.DB.sqlite().init_schema()
    DBM.seed_watchlist(store, {"AAA": {"market": "KR", "leaders": None, "inverse": False, "pair": None}})
    monkeypatch.setattr(A, "_store", store)
    return TestClient(A.app), store


def test_trading_page_and_kill_switch(client):
    c, store = client
    r = c.get("/trading")
    assert r.status_code == 200 and "모드" in r.text and "OFF" in r.text and "미충족" in r.text
    c.post("/trading/toggle", follow_redirects=False)
    assert DBM.get_settings(store)["autotrade_enabled"] == "1"
    assert "ON" in c.get("/trading").text
    c.post("/trading/toggle", follow_redirects=False)
    assert DBM.get_settings(store)["autotrade_enabled"] == "0"


def test_trading_settings_form(client):
    c, store = client
    data = {"max_positions": "2", "max_orders_per_day": "10", "daily_loss_limit_krw": "100000",
            "daily_loss_limit_usd": "50", "max_order_amount_krw": "300000", "max_order_amount_usd": "500"}
    r = c.post("/trading/settings", data=data, follow_redirects=False)
    assert r.status_code == 303
    s = DBM.get_settings(store)
    assert s["max_positions"] == "2" and s["max_order_amount_krw"] == "300000"
    r = c.post("/trading/settings", data={**data, "max_positions": "-1"})
    assert r.status_code == 400 and "0 이상" in r.text


def test_watchlist_auto_fields(client):
    c, store = client
    c.post("/watchlist", data={"symbol": "AAA", "market": "KR", "enabled": "1", "auto_trade": "1",
                               "auto_amount": "500000"}, follow_redirects=False)
    item = DBM.load_watchlist(store)["AAA"]
    assert item["auto_trade"] is True and item["auto_amount"] == 500000.0
    r = c.get("/watchlist")
    assert "🤖 500,000" in r.text
    assert "AAA(500,000)" in c.get("/trading").text
    r = c.post("/watchlist", data={"symbol": "AAA", "market": "KR", "auto_amount": "-5"})
    assert r.status_code == 400 and "0 이상" in r.text


def test_manual_cancel_dry_and_permission_check(client, monkeypatch):
    c, store = client
    DBM.insert_order(store, {"intent_id": "AAA-x", "mode": "dry", "symbol": "AAA", "market": "KR", "side": "BUY",
                             "kind": "ENTRY", "order_type": "LIMIT", "price": 100, "quantity": 5, "amount": 500,
                             "bar_key": None, "ref_avg": None, "status": "open", "reason": None, "order_id": "dry-1",
                             "filled_qty": 0, "avg_price": None, "pnl": None,
                             "created_at": "2026-09-09T01:00:00+00:00", "updated_at": None})
    assert "미결 1" in c.get("/trading").text
    assert "취소됨" in c.post("/trading/orders/AAA-x/cancel").text
    assert DBM.get_order(store, "AAA-x")["status"] == "canceled"
    assert "상태가 아니다" in c.post("/trading/orders/AAA-x/cancel").text

    class FakeToss:
        def __init__(self, *a):
            self.account_seq = "1"

        def load_account(self):
            return True
    monkeypatch.setattr(A, "TossReadOnlyClient", FakeToss)

    class FakeOrderClient:
        def __init__(self, cli):
            pass

        def buying_power(self, ccy):
            raise BrokerError("prerequisite-required", "약관")
    import alertbot.trading.broker as B
    monkeypatch.setattr(B, "TossOrderClient", FakeOrderClient)
    assert "사전 자격 미충족" in c.post("/trading/check-permission").text
    FakeOrderClient.buying_power = lambda self, ccy: 1234567.0
    assert "1,234,567 KRW" in c.post("/trading/check-permission").text


def test_binance_kill_switch_toggle(client):
    c, store = client
    assert "Binance 킬 스위치" in c.get("/trading").text
    c.post("/trading/binance/toggle", follow_redirects=False)
    assert DBM.get_settings(store)["binance_trade_enabled"] == "1"
    c.post("/trading/binance/toggle", follow_redirects=False)
    assert DBM.get_settings(store)["binance_trade_enabled"] == "0"
