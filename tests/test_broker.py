"""브로커 — 페이로드·헤더·멱등키·오류 매핑·호가 보정. 실제 API 는 부르지 않는다."""
import pytest

import alertbot.trading.broker as B
from alertbot.trading.models import OrderIntent


class FakeResp:
    def __init__(self, status=200, body=None, headers=None, text=""):
        self.status_code, self._body, self.headers, self.text = status, body, headers or {}, text

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeReader:
    def __init__(self, queue):
        self._token, self.account_seq = "TOK", "77"
        self.calls, self.queue = [], queue
        self.session = self

    def _ensure_token(self):
        pass

    def request(self, method, url, headers=None, params=None, json=None, timeout=None):
        self.calls.append((method, url, headers, params, json))
        return self.queue.pop(0)


def intent(side="BUY", order_type="LIMIT", price=50_150.0, qty=10, market="KR", symbol="005930"):
    return OrderIntent.create("live", symbol, market, side, "ENTRY" if side == "BUY" else "STOP", order_type, price, qty)


def test_round_price_and_tick():
    assert B.kr_tick(1_999) == 1 and B.kr_tick(50_000) == 100 and B.kr_tick(600_000) == 1_000
    assert B.round_price(50_150, "KR", "BUY") == 50_100.0        # 매수는 내림
    assert B.round_price(50_150, "KR", "SELL") == 50_200.0       # 매도는 올림
    assert B.round_price(12.345, "US", "BUY") == 12.34 and B.round_price(0.12345, "US", "SELL") == 0.1235
    assert B._fmt(50_100.0, "KR") == "50100" and B._fmt(12.5, "US") == "12.50" and B._fmt(0.1235, "US") == "0.1235"


def test_place_payload_headers_and_idempotency():
    reader = FakeReader([FakeResp(200, {"result": {"orderId": "o-1", "clientOrderId": "x"}})])
    client = B.TossOrderClient(reader)
    it = intent()
    assert client.place(it) == "o-1"
    method, url, headers, params, body = reader.calls[0]
    assert method == "POST" and url.endswith("/api/v1/orders")
    assert headers == {"Authorization": "Bearer TOK", "X-Tossinvest-Account": "77"}
    assert body == {"clientOrderId": it.intent_id, "symbol": "005930", "side": "BUY", "orderType": "LIMIT",
                    "timeInForce": "DAY", "quantity": "10", "price": "50150"}
    assert len(it.intent_id) <= 36 and it.intent_id.replace("-", "").replace("_", "").isalnum()


def test_market_sell_has_no_price():
    reader = FakeReader([FakeResp(200, {"result": {"orderId": "o-2"}})])
    B.TossOrderClient(reader).place(intent("SELL", "MARKET", 100.0, 3))
    assert "price" not in reader.calls[0][4] and reader.calls[0][4]["side"] == "SELL"


def test_tick_size_retry_uses_nearest_price():
    err = {"error": {"code": "invalid-request", "message": "호가 단위", "data": {"field": "price", "tickSize": "5",
                                                                            "nearestPrices": ["50145", "50150"]}}}
    reader = FakeReader([FakeResp(400, err), FakeResp(200, {"result": {"orderId": "o-3"}})])
    it = intent(price=50_147.0)
    assert B.TossOrderClient(reader).place(it) == "o-3"
    assert reader.calls[1][4]["price"] == "50150" and it.price == 50150.0     # 매수는 위쪽 근접가


def test_errors_map_to_broker_error_without_retry():
    err = {"error": {"code": "insufficient-buying-power", "message": "주문 가능 금액이 부족합니다."}}
    reader = FakeReader([FakeResp(422, err)])
    with pytest.raises(B.BrokerError) as e:
        B.TossOrderClient(reader).place(intent())
    assert e.value.code == "insufficient-buying-power" and len(reader.calls) == 1


def test_rate_limit_retries_once(monkeypatch):
    monkeypatch.setattr(B.time, "sleep", lambda s: None)
    reader = FakeReader([FakeResp(429, {}, {"Retry-After": "0"}), FakeResp(200, {"result": {"cashBuyingPower": "1500000"}})])
    assert B.TossOrderClient(reader).buying_power("KRW") == 1_500_000.0
    assert reader.calls[1][3] == {"currency": "KRW"} and len(reader.calls) == 2


def test_get_and_cancel():
    reader = FakeReader([
        FakeResp(200, {"result": {"orderId": "o-1", "status": "PARTIAL_FILLED",
                                  "execution": {"filledQuantity": "4", "averageFilledPrice": "50100"}}}),
        FakeResp(200, {"result": {"orderId": "o-1"}}),
        FakeResp(409, {"error": {"code": "already-filled", "message": "x"}}),
        FakeResp(200, {"result": {"sellableQuantity": "12"}}),
    ])
    client = B.TossOrderClient(reader)
    st = client.get("o-1")
    assert (st.status, st.filled_qty, st.avg_price, st.raw_status) == ("open", 4.0, 50100.0, "PARTIAL_FILLED")
    client.cancel("o-1")
    client.cancel("o-1")                     # already-filled 는 조용히 넘긴다
    assert client.sellable_quantity("005930") == 12.0
    assert reader.calls[1][1].endswith("/api/v1/orders/o-1/cancel")


def test_no_account_blocks_orders():
    reader = FakeReader([])
    reader.account_seq = None
    with pytest.raises(B.BrokerError) as e:
        B.TossOrderClient(reader).place(intent())
    assert e.value.code == "no-account" and reader.calls == []


def test_dry_run_broker_fills_immediately():
    d = B.DryRunBroker()
    it = intent()
    oid = d.place(it)
    st = d.get(oid)
    assert st.status == "filled" and st.filled_qty == 10 and st.avg_price == it.price
    assert d.buying_power("KRW") == float("inf")
