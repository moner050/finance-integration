"""Binance live 자동매매 — 가짜 브로커로 진입(시장가 + 손절 알고 주문), 계정 live 스위치, 손절 체결·수동 종료·보유 한도, 실패 처리,
계정별 자본·알림 경로, 계정 변경 반영(AccountTraders)."""
import math
from datetime import timedelta

import pytest

from alertbot import accounts as ACC
from alertbot import config, crypto, db
from alertbot.binance_broker import BrokerError
from alertbot.binance_crash import KST
from alertbot.binance_trade import AccountTraders, Trader, TraderGroup
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


def make(store, on=True, capital=1000.0, email="me@example.com"):
    q = {"mark": 100.0, "next": 1_000}
    rec, fb = Recorder(), FakeBroker()
    account_id = ACC.add_account(store, email)
    ACC.update_live(store, account_id, binance_live=on, binance_capital=capital)
    t = Trader(store, rec, "live", fb, fetch_price=lambda s: fb.price, account={"id": account_id, "email": email, "binance_capital": capital},
               fetch_premium=lambda s: {"mark": q["mark"], "next_funding": q["next"]}, fetch_settled_funding=lambda s: (1_000, 0.0))
    return t, fb, rec, q


def test_trader_requires_account_only_for_live(store):
    with pytest.raises(ValueError):
        Trader(store, Recorder(), "live", FakeBroker())
    with pytest.raises(ValueError):
        Trader(store, Recorder(), "dry", account={"id": 1, "email": "x", "binance_capital": 1.0})


def test_kill_switch_blocks_live_entry(store):
    t, fb, rec, q = make(store, on=False)
    assert t.on_entry("CRASH_BUY", "ETCUSDT", "long", RES, 5, now=T) is None
    assert rec.sent[-1].kind == "BN_SKIP" and "live 스위치 OFF" in rec.sent[-1].body and fb.orders == []
    assert rec.sent[-1].account_id == t.account_id                     # 계정 알림은 그 계정 채널로


def test_live_entry_places_market_and_algo_stop(store):
    t, fb, rec, q = make(store)
    p = t.on_entry("CRASH_BUY", "ETCUSDT", "long", dict(RES, stop=97.004), 5, now=T)
    assert fb.orders == [("open", "ETCUSDT", "long", 20.0)] and p["qty"] == 20.0 and p["entry_price"] == 100.0
    assert p["entry_order_id"] == "o1" and p["stop_order_id"] == "1" and fb.stops["1"]["px"] == 97.0 and p["stop"] == 97.0
    assert p["mode"] == "live" and p["account_id"] == t.account_id
    assert rec.sent[-1].kind == "BN_ENTRY" and rec.sent[-1].body.startswith("[LIVE]") and rec.sent[-1].account_id == t.account_id
    assert db.binance_positions(store, status="open")[0]["stop_order_id"] == "1"


def test_capital_and_books_are_per_account(store):
    """계정마다 자본이 다르고, 같은 전략도 계정마다 따로 연다 — 한도·일손실은 자기 장부만 센다."""
    small, fb1, rec1, _ = make(store, capital=100.0, email="small@example.com")
    big, fb2, rec2, _ = make(store, capital=2000.0, email="big@example.com")
    p1 = small.on_entry("CRASH_BUY", "ETCUSDT", "long", RES, 5, now=T)
    p2 = big.on_entry("CRASH_BUY", "ETCUSDT", "long", RES, 5, now=T)
    assert p1["notional"] == 200.0 and p2["notional"] == 4000.0                # 자본 × 유효 2배
    assert small.open_rows()[0]["id"] == p1["id"] and big.open_rows()[0]["id"] == p2["id"]
    assert small.on_entry("CRASH_BUY", "ETCUSDT", "long", RES, 5, now=T) is None     # 자기 장부의 같은 전략만 막는다
    assert "× 2배" in rec2.sent[-1].body and "자본 2,000" in rec2.sent[-1].body


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
    report = t.daily_report((T + timedelta(hours=480)).astimezone(KST).strftime("%Y-%m-%d"))
    assert report.startswith("종료 1건: 1익절") and "SURGE_ENTRY_1D" in report


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
    assert ACC.get(store, t.account_id)["binance_live"] is False and "차단" in rec.sent[-1].title   # 그 계정 스위치만 꺼진다
    assert len(db.binance_positions(store)) == 1


def test_live_trader_ignores_dry_rows(store):
    t, fb, rec, q = make(store)
    db.insert_binance_position(store, dict(mode="dry", strategy="CRASH_BUY", symbol="ETCUSDT", side="long", qty=1, entry_price=100,
                                           notional=100, leverage=1, stop=97, deadline=T.isoformat(), next_funding=1_000,
                                           funding=0, status="open", opened_at=T.isoformat()))
    assert t.poll(T + timedelta(hours=1)) == [] and fb.orders == []
    assert t.on_entry("CRASH_BUY", "ETCUSDT", "long", RES, 5, now=T) is not None       # 가상 행은 live 중복 판정에 안 잡힌다


def test_trader_group_routes_paper_to_common_and_live_to_account(store):
    t, fb, rec, q = make(store)
    paper = Trader(store, rec, "dry", fetch_price=lambda s: fb.price, fetch_premium=lambda s: {"mark": q["mark"], "next_funding": q["next"]},
                   fetch_settled_funding=lambda s: (1_000, 0.0))
    g = TraderGroup([paper, t])
    rows = g.on_entry("CRASH_BUY", "ETCUSDT", "long", RES, 8, now=T)
    assert [r["mode"] for r in rows] == ["dry", "live"] and len(db.binance_positions(store, status="open")) == 2
    assert [(s.kind, s.account_id) for s in rec.sent[-2:]] == [("BN_ENTRY", None), ("BN_ENTRY", t.account_id)]
    fb.stops["1"].update(triggered=True, price=96.9, active=False)
    fb.pos[("ETCUSDT", "long")] = 0.0
    q["mark"], fb.price = 96.9, 96.8
    closed = g.poll(T + timedelta(minutes=1))
    assert sorted(c["mode"] for c in closed) == ["dry", "live"] and all(c["exit_reason"] == "stop" for c in closed)
    assert sorted((s.kind, s.account_id or 0) for s in rec.sent[-2:]) == [("BN_EXIT", 0), ("BN_EXIT", t.account_id)]

    class Boom:                                                          # 한쪽이 죽어도 다른 쪽은 돈다
        mode = "x"

        def on_entry(self, *a, **k):
            raise RuntimeError("down")

        def poll(self, now=None):
            raise RuntimeError("down")
    g2 = TraderGroup([Boom(), paper])
    assert g2.poll(T) == []
    assert [r["mode"] for r in g2.on_entry("SURGE_ENTRY", "BTCUSDT", "long", RES, 168, now=T) if r] == ["dry"]


def test_account_traders_follow_switches_and_keep_open_positions(store, monkeypatch):
    monkeypatch.setattr(config, "MASTER_KEY", crypto.generate_key())
    a = ACC.add_account(store, "a@example.com")
    for provider, fields in (("binance", {"api_key": "k", "api_secret": "s"}), ("telegram", {"bot_token": "1:T", "chat_id": "5"})):
        ACC.save_keys(store, a, provider, fields)
    built, fail, clock = [], [True], [0.0]

    def build(st, acc, symbols, lev):
        built.append(acc["id"])
        if fail[0]:
            return None
        return Trader(st, Recorder(), "live", FakeBroker(), account=acc)

    live = AccountTraders(store, ["ETCUSDT"], 3, build=build, clock=lambda: clock[0])
    assert live.refresh() == [] and built == []                                   # 스위치 OFF
    ACC.update_live(store, a, binance_live=True)
    assert live.refresh() == [] and built == []                                   # 자본 0 → 건너뜀
    ACC.update_live(store, a, binance_capital=500)
    assert live.refresh() == [] and built == [a]                                  # 준비 실패 (그 계정에만 알림)
    assert live.refresh() == [] and built == [a]                                  # 재시도 시각 전
    fail[0], clock[0] = False, 601.0
    [trader] = live.refresh()                                                     # 10분 뒤 다시 시도
    assert trader.capital == 500.0 and built == [a, a]
    db.insert_binance_position(store, dict(mode="live", account_id=a, strategy="CRASH_BUY", symbol="ETCUSDT", side="long", qty=1,
                                           entry_price=100, notional=100, leverage=2, stop=97, deadline=T.isoformat(),
                                           next_funding=1_000, funding=0, status="open", opened_at=T.isoformat()))
    ACC.update_live(store, a, binance_live=False)                                 # 스위치를 꺼도 열린 포지션이 있으면 감시는 계속
    assert live.refresh() == [trader]
    ACC.set_active(store, a, False)                                               # 계정을 중지해도 마찬가지
    assert live.refresh() == [trader]
    db.update_binance_position(store, db.binance_positions(store)[0]["id"], status="closed")
    assert live.refresh() == []                                                   # 포지션이 닫히면 치운다


def test_live_skips_paper_only_strategies(store):
    t, fb, rec, q = make(store)
    assert t.capital_total == 1000.0 * 4                                  # live 한도는 실제로 주문하는 전략만 센다
    res = dict(RES, stop=120.0, take_profit=80.0)
    assert t.on_entry("SCAN_FADE", "LSKUSDT", "short", res, 48, now=T, notify_skip=False) is None
    assert fb.orders == [] and rec.sent == [] and db.binance_positions(store) == []
