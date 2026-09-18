"""백오피스 자동매매 화면 — 킬 스위치, 한도, 주문 취소, 권한 확인, 워치리스트 자동 필드."""
import pytest

import alertbot.backoffice.app as A
from alertbot import db as DBM
from alertbot import accounts as ACC
from alertbot import config, crypto
from alertbot.config import max_stop_pct
from tests.backoffice_login import logged_in


@pytest.fixture
def client(monkeypatch):
    store = DBM.DB.sqlite().init_schema()
    DBM.seed_watchlist(store, {"AAA": {"market": "KR", "leaders": None, "inverse": False, "pair": None}})
    monkeypatch.setattr(A, "_store", store)
    return logged_in(A, store), store


def test_trading_page_and_my_live_switches(client, monkeypatch):
    """전역 킬 스위치는 없다 — 계정마다 live 스위치·금액 배율·Binance 자본을 둔다. 키 없이는 켤 수 없다."""
    c, store = client
    monkeypatch.setattr(config, "MASTER_KEY", crypto.generate_key())
    me = ACC.get_by_email(store, "admin@example.com")["id"]
    r = c.get("/trading")
    assert r.status_code == 200 and "내 live 매매" in r.text and "공용 가상 장부" in r.text and "킬 스위치" not in r.text
    r = c.post("/trading/live", data={"toss_live": "1", "amount_scale": "1", "binance_capital": "0"})
    assert r.status_code == 400 and "텔레그램 키" in r.text and ACC.get(store, me)["toss_live"] is False
    ACC.save_keys(store, me, "telegram", {"bot_token": "1:T", "chat_id": "5"})
    assert "토스 키가 있어야" in c.post("/trading/live", data={"toss_live": "1", "amount_scale": "1", "binance_capital": "0"}).text
    ACC.save_keys(store, me, "toss", {"client_id": "i", "client_secret": "s"})
    r = c.post("/trading/live", data={"toss_live": "1", "amount_scale": "0.5", "binance_capital": "300"}, follow_redirects=False)
    assert r.status_code == 303
    acc = ACC.get(store, me)
    assert acc["toss_live"] is True and acc["binance_live"] is False and acc["amount_scale"] == 0.5 and acc["binance_capital"] == 300
    assert "Binance 키" in c.post("/trading/live", data={"binance_live": "1", "amount_scale": "1", "binance_capital": "300"}).text
    assert "배율" in c.post("/trading/live", data={"amount_scale": "11", "binance_capital": "0"}).text
    c.post("/trading/live", data={"amount_scale": "1", "binance_capital": "0"}, follow_redirects=False)     # 체크 해제 = 끄기
    assert ACC.get(store, me)["toss_live"] is False


def test_member_sees_only_own_live_records_and_cannot_change_limits(client):
    c, store = client
    member = logged_in(A, store, "member@example.com", "member")
    mid = ACC.get_by_email(store, "member@example.com")["id"]
    other = ACC.add_account(store, "other@example.com")
    base = {"symbol": "AAA", "market": "KR", "side": "BUY", "kind": "ENTRY", "order_type": "LIMIT", "price": 100, "quantity": 5,
            "amount": 500, "bar_key": None, "ref_avg": None, "status": "open", "reason": None, "filled_qty": 0, "avg_price": None,
            "pnl": None, "created_at": "2026-09-09T01:00:00+00:00", "updated_at": None, "mode": "live"}
    DBM.insert_order(store, {**base, "intent_id": "MINE-1", "order_id": "m1", "account_id": mid})
    DBM.insert_order(store, {**base, "intent_id": "OTHER-1", "order_id": "o1", "account_id": other})
    page = member.get("/trading").text
    assert "MINE-1" in page and "OTHER-1" not in page and "readonly" in page
    assert member.post("/trading/settings", data={"max_positions": "9", "max_orders_per_day": "1", "daily_loss_limit_krw": "1",
                                                   "daily_loss_limit_usd": "1", "max_order_amount_krw": "1",
                                                   "max_order_amount_usd": "1"}).status_code == 403
    assert member.post("/trading/orders/OTHER-1/cancel").status_code == 403          # 남의 주문은 못 취소한다
    admin_page = c.get("/trading").text
    assert "MINE-1" in admin_page and "OTHER-1" in admin_page and "member@example.com" in admin_page


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
    assert "AAA · 500,000" in c.get("/trading").text          # live 자동매매 종목 배지
    r = c.post("/watchlist", data={"symbol": "AAA", "market": "KR", "auto_amount": "-5"})
    assert r.status_code == 400 and "0 이상" in r.text


def test_manual_cancel_virtual_and_live_uses_that_accounts_keys(client, monkeypatch):
    c, store = client
    monkeypatch.setattr(config, "MASTER_KEY", crypto.generate_key())
    base = {"symbol": "AAA", "market": "KR", "side": "BUY", "kind": "ENTRY", "order_type": "LIMIT", "price": 100, "quantity": 5,
            "amount": 500, "bar_key": None, "ref_avg": None, "status": "open", "reason": None, "filled_qty": 0, "avg_price": None,
            "pnl": None, "created_at": "2026-09-09T01:00:00+00:00", "updated_at": None}
    DBM.insert_order(store, {**base, "intent_id": "AAA-x", "mode": "dry", "order_id": "dry-1"})
    assert "취소됨" in c.post("/trading/orders/AAA-x/cancel").text
    assert DBM.get_order(store, "AAA-x")["status"] == "canceled"
    assert "상태가 아니다" in c.post("/trading/orders/AAA-x/cancel").text

    owner = ACC.add_account(store, "owner@example.com")
    ACC.save_keys(store, owner, "toss", {"client_id": "owner-id", "client_secret": "owner-secret"})
    DBM.insert_order(store, {**base, "intent_id": "AAA-l", "mode": "live", "order_id": "T-9", "account_id": owner})
    used = {}

    class FakeToss:
        def __init__(self, cid, secret):
            used["keys"] = (cid, secret)

        def load_account(self):
            return True

    class FakeOrderClient:
        def __init__(self, cli):
            pass

        def cancel(self, order_id):
            used["order"] = order_id
    monkeypatch.setattr(A, "TossReadOnlyClient", FakeToss)
    import alertbot.trading.broker as B
    monkeypatch.setattr(B, "TossOrderClient", FakeOrderClient)
    assert "취소됨" in c.post("/trading/orders/AAA-l/cancel").text
    assert used == {"keys": ("owner-id", "owner-secret"), "order": "T-9"}            # 공용 키가 아니라 그 주문 계정의 키


def test_results_page_shows_live_then_dry_series(client, monkeypatch, tmp_path):
    c, store = client
    monkeypatch.setattr(A, "DATA_DIR", tmp_path)                      # 신호 성적 CSV 는 비어 있다
    base = {"market": "KR", "order_type": "MARKET", "price": 100.0, "quantity": 10, "amount": 1000, "bar_key": None,
            "reason": None, "order_id": "dry-1", "filled_qty": 10, "status": "filled", "updated_at": None}
    DBM.insert_order(store, {**base, "intent_id": "AAA-b", "mode": "dry", "symbol": "AAA", "side": "BUY", "kind": "ENTRY",
                             "ref_avg": None, "avg_price": 100.0, "pnl": None, "created_at": "2026-09-15T01:00:00+00:00"})
    DBM.insert_order(store, {**base, "intent_id": "AAA-s", "mode": "dry", "symbol": "AAA", "side": "SELL", "kind": "SELL",
                             "ref_avg": 100.0, "avg_price": 102.0, "pnl": 20.0, "created_at": "2026-09-15T02:00:00+00:00"})
    DBM.insert_order(store, {**base, "intent_id": "AAA-s2", "mode": "dry", "symbol": "AAA", "side": "SELL", "kind": "STOP",
                             "ref_avg": 100.0, "avg_price": 97.0, "pnl": -30.0, "created_at": "2026-09-16T02:00:00+00:00"})
    me = ACC.get_by_email(store, "admin@example.com")
    DBM.insert_order(store, {**base, "intent_id": "AAA-l", "mode": "live", "symbol": "AAA", "side": "SELL", "kind": "SELL",
                             "ref_avg": 100.0, "avg_price": 101.0, "pnl": 10.0, "created_at": "2026-09-16T03:00:00+00:00",
                             "account_id": me["id"]})
    ctx = A.results_context({**me, "session_csrf": ""})
    assert (ctx["dry"]["n"], ctx["dry"]["wins"], ctx["dry"]["rate"], ctx["dry"]["pnl"]["KRW"]) == (2, 1, 50, -10.0)
    assert [(d["day"], d["n"], d["cum"]["KRW"]) for d in ctx["dry"]["days"]] == [("09-15", 1, 20.0), ("09-16", 1, -10.0)]
    assert ctx["dry"]["trades"][0]["kind"] == "STOP" and ctx["dry"]["trades"][0]["pct"] == -3.0       # 최신순
    assert ctx["live"]["n"] == 1 and ctx["live"]["pnl"]["KRW"] == 10.0 and ctx["signals"]["n"] == 0
    r = c.get("/results")
    assert r.status_code == 200
    assert r.text.index("실전 매매 (live)") < r.text.index("모의 매매 (공용 가상 장부)") < r.text.index("신호 모의 성적")
    assert "닫힌 신호가 없다" in r.text and "-3.00%" in r.text
    # 합친 탭: 옛 주소는 새 탭으로 간다
    assert c.get("/summary", follow_redirects=False).status_code == 303 and "시황 시계열" in c.get("/status").text
    assert c.get("/channels", follow_redirects=False).headers["location"] == "/signals" and "테스트 발송" in c.get("/signals").text


def test_account_binance_leverage(client, monkeypatch):
    """Binance 격리 배율은 계정마다 따로 저장하고, 화면은 공용 배율과 내 배율의 손절을 나란히 보여 준다."""
    c, store = client
    monkeypatch.setattr(config, "MASTER_KEY", crypto.generate_key())
    me = ACC.get_by_email(store, "admin@example.com")["id"]
    assert ACC.get(store, me)["binance_leverage"] == 3                       # 기본값 = 공용 기준
    r = c.post("/trading/live", data={"amount_scale": "1", "binance_capital": "0", "binance_leverage": "5"},
               follow_redirects=False)
    assert r.status_code == 303 and ACC.get(store, me)["binance_leverage"] == 5
    for bad in ("0", "6", "x"):
        r = c.post("/trading/live", data={"amount_scale": "1", "binance_capital": "0", "binance_leverage": bad})
        assert r.status_code == 400 and "격리 배율" in r.text
    assert ACC.get(store, me)["binance_leverage"] == 5                       # 거절된 값은 저장되지 않는다
    body = c.get("/trading").text
    cap = max_stop_pct(5)
    assert "격리 <b>5배</b>" in body and f"{cap:.1f}%" in body                 # 손절 상한이 표에 보인다
    assert "25.0% →" in body and "다섯 전략 동시" in body                       # 일봉 숏은 상한으로 당겨진다고 표기
