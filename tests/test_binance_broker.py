"""Binance 선물 서명 클라이언트 — 서명·헤더, 주문 파라미터(헤지 모드·알고 손절), 오류 변환, 수량·가격 반올림."""
import hashlib
import hmac
import urllib.parse

import pytest

from alertbot.binance_broker import BinanceFutures, BrokerError, _digits

FILTERS = {"BTCUSDT": {"qty_d": 3, "price_d": 1, "min_qty": 0.001, "min_notional": 50.0}}


class FakeResp:
    def __init__(self, status, payload):
        self.status_code, self._p = status, payload

    def json(self):
        return self._p


class FakeSession:
    def __init__(self, payload=None, status=200):
        self.calls, self.payload, self.status = [], payload if payload is not None else {}, status

    def request(self, method, url, params=None, headers=None, timeout=None):
        self.calls.append((method, url, dict(params), headers))
        return FakeResp(self.status, self.payload)


def make(payload, status=200):
    s = FakeSession(payload, status)
    b = BinanceFutures("key", "secret", session=s)
    b.filters = dict(FILTERS)
    return b, s


def test_market_open_signs_request_and_sends_hedge_params():
    b, s = make({"orderId": 77, "avgPrice": "100.5", "executedQty": "0.010", "status": "FILLED"})
    assert b.market_open("BTCUSDT", "long", 0.0104) == ("77", 100.5, 0.01)
    method, url, params, headers = s.calls[-1]
    assert method == "POST" and url.endswith("/fapi/v1/order") and headers["X-MBX-APIKEY"] == "key"
    sig = params.pop("signature")
    assert sig == hmac.new(b"secret", urllib.parse.urlencode(params).encode(), hashlib.sha256).hexdigest()
    assert params["side"] == "BUY" and params["positionSide"] == "LONG" and params["type"] == "MARKET"
    assert params["quantity"] == "0.010" and params["newOrderRespType"] == "RESULT" and "timestamp" in params


def test_close_and_stop_use_opposite_side_and_algo_endpoint():
    b, s = make({"orderId": 78, "avgPrice": "99", "executedQty": "0.01", "status": "FILLED"})
    b.market_close("BTCUSDT", "short", 0.01)
    assert s.calls[-1][2]["side"] == "BUY" and s.calls[-1][2]["positionSide"] == "SHORT"
    b, s = make({"algoId": 9001, "algoStatus": "NEW"})
    assert b.place_stop("BTCUSDT", "long", 96.96) == "9001"
    p = s.calls[-1][2]
    assert s.calls[-1][1].endswith("/fapi/v1/algoOrder") and p["algoType"] == "CONDITIONAL" and p["type"] == "STOP_MARKET"
    assert p["side"] == "SELL" and p["positionSide"] == "LONG" and p["triggerPrice"] == "97.0"
    assert p["closePosition"] == "true" and p["workingType"] == "MARK_PRICE" and "quantity" not in p
    b, s = make({"algoId": 9001, "algoStatus": "TRIGGERED", "actualOrderId": 5, "actualPrice": "96.9"})
    assert b.stop_status("9001") == {"triggered": True, "price": 96.9, "active": False}
    b.cancel_stop("9001")
    assert s.calls[-1][0] == "DELETE" and s.calls[-1][2]["algoId"] == "9001"


def test_errors_become_broker_error():
    b, s = make({"code": -4120, "msg": "stop order switch algo"}, status=400)
    with pytest.raises(BrokerError) as e:
        b.place_stop("BTCUSDT", "long", 97)
    assert e.value.code == "-4120"
    b, s = make({"orderId": 1, "avgPrice": "0", "executedQty": "0", "status": "EXPIRED"})
    with pytest.raises(BrokerError, match="not-filled"):
        b.market_open("BTCUSDT", "long", 0.01)


def test_setup_ignores_already_set_and_checks_leverage():
    b, s = make({"leverage": 3})
    answers = iter([FakeResp(400, {"code": -4059, "msg": "No need to change position side."}),
                    FakeResp(400, {"code": -4046, "msg": "No need to change margin type."}), FakeResp(200, {"leverage": 3})])
    s.request = lambda *a, **k: next(answers)
    b.setup(["BTCUSDT"], 3)
    b, s = make({"leverage": 5})
    with pytest.raises(BrokerError, match="leverage"):
        b.setup(["BTCUSDT"], 3)


def test_filters_and_rounding():
    b, s = make({"symbols": [{"symbol": "ETCUSDT", "filters": [
        {"filterType": "PRICE_FILTER", "tickSize": "0.001"}, {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01"},
        {"filterType": "MIN_NOTIONAL", "notional": "20"}]}]})
    b.load_filters(["ETCUSDT"])
    assert b.filters["ETCUSDT"] == {"qty_d": 2, "price_d": 3, "min_qty": 0.01, "min_notional": 20.0}
    assert b.round_qty("ETCUSDT", 267.3468) == 267.34 and b.round_price("ETCUSDT", 7.19053) == 7.191
    assert b.round_qty("BTCUSDT", 0.0129999) == 0.012 and _digits("1") == 0 and _digits("0.10") == 1
    with pytest.raises(BrokerError, match="unknown-symbol"):
        b.load_filters(["XXXUSDT"])
