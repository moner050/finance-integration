"""Binance live 자동매매 — 가짜 브로커로 진입(시장가 + 손절 알고 주문), 킬 스위치, 손절 체결·수동 종료·보유 한도, 실패 처리."""
import math
from datetime import timedelta

import pytest

from alertbot import db
from alertbot.binance_broker import BrokerError
from alertbot.binance_trade import Trader
from tests.test_binance_trade import RES, T, Recorder, fixed_capital, store  # noqa: F401  (fixtures)


class FakeBroker:
    def __init__(self):
        self.orders, self.stops, self.pos, self.price = [], {}, {}, 100.0
        self.fail_open = self.fail_stop = False
        self._seq = 0

    def round_qty(self, s, q):
        return math.floor(q * 1000) / 1000

    def min_notional(self, s):
        return 20.0

    def round_price(self, s, p):
        return round(p, 2)

    def market_open(self, s, side, qty):
        if self.fail_open:
            raise BrokerError("-2019", "Margin is insufficient.")
        self.orders.append(("open", s, side, qty))
        self.pos[(s, side)] = qty
        return "o1", self.price, qty

    def place_stop(self, s, side, px):
        if self.fail_stop:
            raise BrokerError("-4120", "switch algo")
        self._seq += 1
        self.stops[str(self._seq)] = {"triggered": False, "price": 0.0, "active": True, "px": px}
        return str(self._seq)

    def stop_status(self, aid):
        return {k: v for k, v in self.stops[aid].items() if k != "px"}

    def cancel_stop(self, aid):
        self.stops[aid]["active"] = False

    def position_qty(self, s, side):
        return self.pos.get((s, side), 0.0)

    def market_close(self, s, side, qty):
        self.orders.append(("close", s, side, qty))
        self.pos[(s, side)] = 0.0
        return "o2", self.price, qty


def make(store, on=True):
    q = {"mark": 100.0, "next": 1_000}
    rec, fb = Recorder(), FakeBroker()
    t = Trader(store, rec, "live", fb, fetch_price=lambda s: fb.price,
               fetch_premium=lambda s: {"mark": q["mark"], "next_funding": q["next"]}, fetch_settled_funding=lambda s: (1_000, 0.0))
    if on:
        db.set_setting(store, "binance_trade_enabled", 1)
    return t, fb, rec, q


def test_kill_switch_blocks_live_entry(store):
    t, fb, rec, q = make(store, on=False)
    assert t.on_entry("CRASH_BUY", "ETCUSDT", "long", RES, 5, now=T) is None
    assert rec.sent[-1].kind == "BN_SKIP" and "킬 스위치" in rec.sent[-1].body and fb.orders == []


def test_live_entry_places_market_and_algo_stop(store):
    t, fb, rec, q = make(store)
    p = t.on_entry("CRASH_BUY", "ETCUSDT", "long", dict(RES, stop=97.004), 5, now=T)
    assert fb.orders == [("open", "ETCUSDT", "long", 20.0)] and p["qty"] == 20.0 and p["entry_price"] == 100.0
    assert p["entry_order_id"] == "o1" and p["stop_order_id"] == "1" and fb.stops["1"]["px"] == 97.0 and p["stop"] == 97.0
    assert p["mode"] == "live" and rec.sent[-1].kind == "BN_ENTRY" and rec.sent[-1].body.startswith("[LIVE]")
    assert db.binance_positions(store, status="open")[0]["stop_order_id"] == "1"


def test_stop_fill_manual_close_and_deadline(store):
    t, fb, rec, q = make(store)
    t.on_entry("CRASH_BUY", "ETCUSDT", "long", RES, 5, now=T)
    assert t.poll(T + timedelta(minutes=1)) == []
    fb.stops["1"].update(triggered=True, price=96.9, active=False)
    fb.pos[("ETCUSDT", "long")] = 0.0
    closed = t.poll(T + timedelta(minutes=2))
    assert closed[0]["exit_reason"] == "stop" and closed[0]["exit_price"] == 96.9 and "추정" in rec.sent[-1].body
    p2 = t.on_entry("SURGE_ENTRY", "BTCUSDT", "long", RES, 168, now=T)
    fb.pos[("BTCUSDT", "long")] = 0.0                                    # 거래소에서 직접 닫았다
    q["mark"] = 101.0
    closed = t.poll(T + timedelta(hours=1))
    assert closed[0]["exit_reason"] == "manual" and closed[0]["exit_price"] == 101.0
    p3 = t.on_entry("SURGE_ENTRY_1D", "BTCUSDT", "long", RES, 480, now=T)
    fb.price = 110.0
    assert t.poll(T + timedelta(hours=479)) == []
    closed = t.poll(T + timedelta(hours=480))
    assert closed[0]["exit_reason"] == "time" and fb.orders[-1] == ("close", "BTCUSDT", "long", p3["qty"])
    assert fb.stops[p3["stop_order_id"]]["active"] is False and closed[0]["pnl"] > 0


def test_stop_failure_is_retried_and_open_failures_disable(store):
    t, fb, rec, q = make(store)
    fb.fail_stop = True
    p = t.on_entry("CRASH_BUY", "ETCUSDT", "long", RES, 5, now=T)
    assert p["stop_order_id"] is None and any(s.kind == "BN_FAIL" for s in rec.sent) and "재시도" in rec.sent[-1].body
    assert t.poll(T + timedelta(minutes=1)) == [] and db.binance_positions(store, status="open")[0]["stop_order_id"] is None
    fb.fail_stop = False
    t.poll(T + timedelta(minutes=2))
    assert db.binance_positions(store, status="open")[0]["stop_order_id"] == "1"
    fb.fail_open = True
    for k in range(3):
        assert t.on_entry("SURGE_ENTRY", "BTCUSDT", "long", RES, 168, now=T + timedelta(hours=k)) is None
    assert db.get_settings(store)["binance_trade_enabled"] == "0" and "차단" in rec.sent[-1].title
    assert len(db.binance_positions(store)) == 1


def test_live_trader_ignores_dry_rows(store):
    t, fb, rec, q = make(store)
    db.insert_binance_position(store, dict(mode="dry", strategy="CRASH_BUY", symbol="ETCUSDT", side="long", qty=1, entry_price=100,
                                           notional=100, leverage=1, stop=97, deadline=T.isoformat(), next_funding=1_000,
                                           funding=0, status="open", opened_at=T.isoformat()))
    assert t.poll(T + timedelta(hours=1)) == [] and fb.orders == []
    assert t.on_entry("CRASH_BUY", "ETCUSDT", "long", RES, 5, now=T) is not None       # dry 행은 live 중복 판정에 안 잡힌다
