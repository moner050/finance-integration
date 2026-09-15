"""Binance dry 자동매매 — 진입 크기·한도, 마크 손절·보유 한도 종료, 펀딩 정산, 워커 연결."""
from datetime import datetime, timedelta, timezone

import pytest

from alertbot import db
from alertbot.binance_crash import CrashWorker
from alertbot.binance_follow import FollowWorker
from alertbot.binance_trade import DryTrader
from alertbot.config import BINANCE_TRADE_CAPITAL, BINANCE_TRADE_FEE, BINANCE_TRADE_LEVERAGE

T = datetime(2026, 9, 15, 3, 0, tzinfo=timezone.utc)
RES = {"stop": 97.0, "open_time": 1_780_000_000_000, "funding": 0.0001}


class Recorder:
    def __init__(self):
        self.sent = []

    def send(self, signal, force=False):
        self.sent.append(signal)
        return {"test": "ok"}


@pytest.fixture
def store():
    d = db.DB.sqlite().init_schema()
    yield d
    d.close()


def make(store, price=100.0, nxt=1_000):
    q = {"price": price, "mark": price, "next": nxt, "settled": (nxt, 0.0001)}
    rec = Recorder()
    t = DryTrader(store, rec, fetch_price=lambda s: q["price"],
                  fetch_premium=lambda s: {"mark": q["mark"], "next_funding": q["next"]},
                  fetch_settled_funding=lambda s: q["settled"])
    return t, q, rec


def test_entry_sizes_by_capital_and_leverage(store):
    t, q, rec = make(store)
    p = t.on_entry("CRASH_BUY", "ETCUSDT", "long", RES, 5, now=T)
    assert p["status"] == "open" and abs(p["notional"] - BINANCE_TRADE_CAPITAL * 2) < 1          # 유효 2배
    assert p["entry_price"] == 100 * 1.0005 and p["deadline"] == (T + timedelta(hours=5)).isoformat(timespec="seconds")
    assert p["stop"] == 97.0 and p["next_funding"] == 1_000 and p["signal_bar"] == RES["open_time"]
    assert rec.sent[-1].kind == "BN_ENTRY" and rec.sent[-1].body.startswith("[DRY]") and "× 2배" in rec.sent[-1].body
    assert t.on_entry("CRASH_BUY", "ETCUSDT", "long", RES, 5, now=T) is None                     # 같은 전략은 하나만
    assert rec.sent[-1].kind == "BN_SKIP" and "열려 있다" in rec.sent[-1].body
    assert len(db.binance_positions(store, status="open")) == 1


def test_funding_gate_halves_size(store):
    t, q, rec = make(store)
    p = t.on_entry("SURGE_ENTRY", "BTCUSDT", "long", dict(RES, funding=0.0005), 168, now=T)
    assert abs(p["notional"] - BINANCE_TRADE_CAPITAL * 1.0 / 2) < 1 and "절반" in rec.sent[-1].body
    s = t.on_entry("CRASH_SHORT_1D", "ETCUSDT", "short", dict(RES, stop=125.0, funding=-0.0005), 480, now=T)
    assert abs(s["notional"] - BINANCE_TRADE_CAPITAL * 0.5 / 2) < 1


def test_stop_on_mark_and_pnl(store):
    t, q, rec = make(store)
    p = t.on_entry("CRASH_BUY", "ETCUSDT", "long", RES, 5, now=T)
    q["mark"], q["price"] = 97.5, 96.0                                   # 마크는 손절 위 — 최종가가 낮아도 유지
    assert t.poll(T + timedelta(minutes=1)) == []
    q["mark"], q["price"] = 96.9, 96.8                                   # 마크 손절 도달 → 최종가 - 슬리피지로 종료
    closed = t.poll(T + timedelta(minutes=2))
    assert len(closed) == 1 and closed[0]["exit_reason"] == "stop"
    exit_px = 96.8 * (1 - 0.0005)
    gross = (exit_px - p["entry_price"]) * p["qty"]
    fees = (p["entry_price"] + exit_px) * p["qty"] * BINANCE_TRADE_FEE
    assert abs(closed[0]["pnl"] - (gross - fees)) < 1e-3 and closed[0]["pnl"] < 0      # 손익은 소수 4자리로 저장
    assert rec.sent[-1].kind == "BN_EXIT" and "손절" in rec.sent[-1].body
    assert db.binance_positions(store, status="open") == [] and db.binance_positions(store)[0]["status"] == "closed"


def test_time_exit_and_short_pnl(store):
    t, q, rec = make(store)
    p = t.on_entry("CRASH_SHORT_1D", "ETCUSDT", "short", dict(RES, stop=125.0), 480, now=T)
    assert p["entry_price"] == 100 * (1 - 0.0005) and abs(p["notional"] - BINANCE_TRADE_CAPITAL * 0.5) < 1
    q["price"] = q["mark"] = 90.0
    assert t.poll(T + timedelta(hours=479)) == []
    closed = t.poll(T + timedelta(hours=480))
    assert closed[0]["exit_reason"] == "time" and closed[0]["pnl"] > 0 and "보유 한도" in rec.sent[-1].body


def test_funding_settles_once_per_period(store):
    t, q, rec = make(store, nxt=1_000)
    p = t.on_entry("SURGE_ENTRY_1D", "BTCUSDT", "long", RES, 480, now=T)
    q["next"], q["settled"] = 2_000, (1_000, 0.0002)                    # 정산 시각을 지났다
    t.poll(T + timedelta(hours=1))
    row = db.binance_positions(store, status="open")[0]
    assert abs(row["funding"] - 0.0002 * p["notional"]) < 1e-6 and row["next_funding"] == 2_000
    t.poll(T + timedelta(hours=2))                                      # 같은 정산은 두 번 매기지 않는다
    assert abs(db.binance_positions(store, status="open")[0]["funding"] - 0.0002 * p["notional"]) < 1e-6
    q["price"] = q["mark"] = 100.0
    closed = t.poll(T + timedelta(hours=480))
    assert closed[0]["pnl"] < -0.0002 * p["notional"]                    # 펀딩이 손익에서 빠진다


def test_daily_loss_and_total_notional_block_entries(store, monkeypatch):
    t, q, rec = make(store)
    t.on_entry("CRASH_BUY", "ETCUSDT", "long", RES, 5, now=T)
    q["mark"] = q["price"] = 85.0                                        # -15% 손절 → 자본 합 4,000 의 6% 넘는 손실
    t.poll(T + timedelta(minutes=1))
    assert t.on_entry("SURGE_ENTRY", "BTCUSDT", "long", RES, 168, now=T + timedelta(hours=1)) is None
    assert "실현손익" in rec.sent[-1].body
    assert t.on_entry("SURGE_ENTRY", "BTCUSDT", "long", RES, 168, now=T + timedelta(days=1)) is not None   # 다음 날은 다시
    monkeypatch.setitem(BINANCE_TRADE_LEVERAGE, "SURGE_ENTRY_1D", 20.0)
    assert t.on_entry("SURGE_ENTRY_1D", "BTCUSDT", "long", RES, 480, now=T + timedelta(days=1)) is None
    assert "합산 명목" in rec.sent[-1].body


def test_price_fetch_failure_skips_without_position(store):
    t, q, rec = make(store)

    def boom(s):
        raise RuntimeError("timeout")
    t.fetch_price = boom
    assert t.on_entry("CRASH_BUY", "ETCUSDT", "long", RES, 5, now=T) is None
    assert rec.sent[-1].kind == "BN_SKIP" and "시세 조회 실패" in rec.sent[-1].body
    assert db.binance_positions(store) == []


class TraderStub:
    def __init__(self):
        self.calls = []

    def on_entry(self, *args):
        self.calls.append(args)


def test_workers_hand_entries_to_trader():
    from tests.test_binance_crash import make_bars
    from tests.test_binance_follow import SPEC_1D_SHORT, daily_short
    st, rec = TraderStub(), Recorder()
    fetched = {"ETCUSDT": make_bars(crash_bars=20), "BTCUSDT": make_bars(close=78000)}
    w = CrashWorker(["ETCUSDT"], rec, fetch_bars=lambda s: fetched[s], fetch_fund=lambda s: None, trader=st)
    w.poll_once(T)
    assert len(rec.sent) == 1 and [c[:3] for c in st.calls] == [("CRASH_BUY", "ETCUSDT", "long")]
    assert st.calls[0][3]["stop"] > 0 and st.calls[0][4] == 5 and st.calls[0][5] == T
    bars = daily_short()
    ws = FollowWorker(SPEC_1D_SHORT, rec, fetch_bars=lambda s, iv, n: bars[:n_], fetch_fund=lambda s: None, trader=st)
    for n_ in range(401, len(bars) + 1):                                 # 관찰 알림은 넘기지 않고 진입만 넘긴다
        ws.poll_once(T + timedelta(days=n_))
    kinds = [s.kind for s in rec.sent]
    assert kinds.count("CRASH_WATCH_1D") == 1 and kinds.count("CRASH_SHORT_1D") == 1
    assert [c[:3] for c in st.calls[1:]] == [("CRASH_SHORT_1D", "ETCUSDT", "short")] and st.calls[1][4] == 480
