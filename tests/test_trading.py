"""정책·실행기 — 게이트, 한도, 중복 방지, dry 체결, 자동 차단, 엔진 연동."""
from datetime import datetime, timedelta, timezone

import alertbot.engine as E
import alertbot.market_hours as MH
import alertbot.trading.executor as X
from alertbot import db as DBM
from alertbot.models import Signal
from alertbot.trading.broker import BrokerError, DryRunBroker, OrderState
from alertbot.trading.models import OrderIntent
from alertbot.trading.policy import RiskPolicy
from tests import scenario as sc
from tests.test_notify import Recorder
from alertbot.notify.dispatcher import Dispatcher

HARD = {"KRW": 2_000_000, "USD": 2_000}
CFG = {"market": "KR", "leaders": None, "inverse": False, "pair": None, "auto_trade": True, "auto_amount": 500_000}


def settings(**over):
    s = dict(DBM.SETTING_DEFAULTS)
    s["autotrade_enabled"] = "1"
    s.update({k: str(v) for k, v in over.items()})
    return s


def buy(price=100.0, qty=10, symbol="AAA"):
    return OrderIntent.create("dry", symbol, "KR", "BUY", "ENTRY", "LIMIT", price, qty, bar_key="b1")


def sell(kind="SELL", symbol="AAA"):
    return OrderIntent.create("dry", symbol, "KR", "SELL", kind, "MARKET", 100.0, 10, ref_avg=98.0)


def ctx(**over):
    base = {"settings": settings(), "cfg": CFG, "regular": True, "holdings": {}, "open_intents": [],
            "orders_today": [], "realized_pnl_today": {"KRW": 0.0, "USD": 0.0}, "buying_power": lambda c: 10_000_000,
            "last_price": None}
    base.update(over)
    return base


def test_policy_gates_and_limits():
    p = RiskPolicy(HARD)
    assert p.check(buy(), ctx()) == (True, "ok")
    assert p.check(buy(), ctx(settings=settings(autotrade_enabled=0)))[1] == "disabled"
    assert p.check(buy(), ctx(cfg={**CFG, "auto_trade": False}))[1] == "symbol-not-auto"
    assert p.check(buy(), ctx(regular=False))[1] == "outside-regular-hours"
    assert p.check(buy(qty=0), ctx())[1] == "quantity-below-1"
    assert p.check(buy(), ctx(holdings={"AAA": {"qty": 5, "avg": 99}}))[1] == "already-holding"
    open_buy = [{"symbol": "AAA", "side": "BUY", "status": "sent"}]
    assert p.check(buy(), ctx(open_intents=open_buy))[1] == "open-buy-exists"
    assert p.check(buy(price=150_000, qty=10), ctx())[1].startswith("amount-over-limit")      # 150만 > 100만 설정
    assert p.check(buy(price=250_000, qty=10), ctx(settings=settings(max_order_amount_krw=5_000_000)))[1] \
        .startswith("amount-over-limit(2e+06")                                                  # 하드캡 200만
    held3 = {s: {"qty": 1, "avg": 1} for s in ("X", "Y", "Z")}
    assert p.check(buy(), ctx(holdings=held3))[1] == "max-positions"
    many = [{"symbol": "Q", "side": "BUY", "status": "filled"}] * 20
    assert p.check(buy(), ctx(orders_today=many))[1] == "max-orders-per-day"
    assert p.check(buy(), ctx(realized_pnl_today={"KRW": -300_000, "USD": 0}))[1].startswith("daily-loss-limit")
    assert p.check(buy(price=104.0), ctx(last_price=100.0))[1] == "price-sanity"
    assert p.check(buy(price=101.0), ctx(last_price=100.0)) == (True, "ok")
    assert p.check(buy(), ctx(buying_power=lambda c: 500))[1].startswith("insufficient-buying-power")

    def boom(c):
        raise RuntimeError("api down")
    assert p.check(buy(), ctx(buying_power=boom))[1].startswith("buying-power-unknown")


def test_policy_sell_rules():
    p = RiskPolicy(HARD)
    held = {"AAA": {"qty": 10, "avg": 98}}
    assert p.check(sell(), ctx(holdings=held)) == (True, "ok")
    assert p.check(sell(), ctx())[1] == "nothing-to-sell"
    assert p.check(sell(), ctx(holdings=held, open_intents=[{"symbol": "AAA", "side": "SELL", "status": "open"}]))[1] == "open-sell-exists"
    many = [{"symbol": "Q", "side": "BUY", "status": "filled"}] * 20
    assert p.check(sell("SELL"), ctx(holdings=held, orders_today=many))[1] == "max-orders-per-day"
    assert p.check(sell("STOP"), ctx(holdings=held, orders_today=many)) == (True, "ok")      # 손절은 면제
    # 손실 한도는 매도를 막지 않는다
    assert p.check(sell("EXIT_FULL"), ctx(holdings=held, realized_pnl_today={"KRW": -900_000, "USD": 0})) == (True, "ok")


# --- 실행기 -------------------------------------------------------------------

def make_exec(store=None, broker=None, mode="dry", enabled=True):
    store = store or DBM.DB.sqlite().init_schema()
    DBM.seed_watchlist(store, {"AAA": CFG})
    DBM.set_setting(store, "autotrade_enabled", 1 if enabled else 0)
    rec = Recorder("telegram")
    ex = X.Executor(store, broker or DryRunBroker(), mode, Dispatcher([rec]))
    return ex, store, rec


def snap(price=100.0, bar="b1", cfg=None):
    return {"cfg": cfg or CFG, "market": "KR", "price": price, "bar_key": bar}


def test_executor_dry_buy_then_fill_and_dedupe():
    ex, store, rec = make_exec()
    it = ex.on_signal(Signal("ENTRY", "🔵 매수하세요", "AAA", "b", "AAA"), snap(100.0), {})
    assert it.status == "sent" and it.order_type == "LIMIT" and it.price == 100.0 and it.quantity == 5000  # 50만/100
    assert [s.kind for s in rec.got] == ["ORDER_SENT"] and rec.got[0].body.startswith("[DRY] ")
    # 같은 신호봉의 반복 알림은 무시
    assert ex.on_signal(Signal("ENTRY", "🔵 매수하세요", "AAA", "b", "AAA"), snap(100.5), {}) is None
    assert len(DBM.recent_orders(store)) == 1
    fills = ex.reconcile()
    assert fills == [] and DBM.get_order(store, it.intent_id)["status"] == "filled"
    assert rec.got[-1].kind == "ORDER_FILLED"
    # 열린 의도가 없어졌으니 다른 봉의 신호는 다시 만들 수 있지만, 보유 중이면 정책이 막는다
    it2 = ex.on_signal(Signal("ENTRY", "🔵 매수하세요", "AAA", "b", "AAA"), snap(101.0, "b2"), {"AAA": {"qty": 5000, "avg": 100}})
    assert it2.status == "rejected" and it2.reason == "already-holding"


def test_executor_sell_fill_records_pnl():
    ex, store, rec = make_exec()
    held = {"AAA": {"qty": 5000.0, "avg": 100.0}}
    it = ex.on_signal(Signal("STOP", "🔴 손절하세요", "AAA", "b", "AAA"), snap(95.0), held)
    assert it.status == "sent" and it.order_type == "MARKET" and it.quantity == 5000 and it.ref_avg == 100.0
    fills = ex.reconcile()
    assert fills == [("AAA", 5000.0, 95.0)]
    row = DBM.get_order(store, it.intent_id)
    assert row["status"] == "filled" and row["pnl"] == -25000.0
    assert "실현손익 -25,000" in rec.got[-1].body


def test_executor_kill_switch_and_unrelated_kinds():
    ex, store, rec = make_exec(enabled=False)
    it = ex.on_signal(Signal("ENTRY", "🔵 매수하세요", "AAA", "b", "AAA"), snap(), {})
    assert it.status == "rejected" and it.reason == "disabled" and rec.got == []
    assert ex.on_signal(Signal("EXIT_HALF", "🟡 절반", "AAA", "b", "AAA"), snap(), {"AAA": {"qty": 1, "avg": 1}}) is None
    assert ex.on_signal(Signal("ADDON", "🔵 추가", "AAA", "b", "AAA"), snap(), {}) is None


class FailingBroker(DryRunBroker):
    def __init__(self, code="insufficient-buying-power"):
        super().__init__()
        self.code = code

    def place(self, intent):
        raise BrokerError(self.code, "실패")


def test_executor_auto_disables_after_failures():
    ex, store, rec = make_exec(broker=FailingBroker())
    for i in range(3):
        it = ex.on_signal(Signal("ENTRY", "🔵 매수하세요", "AAA", "b", "AAA"), snap(100.0, f"b{i}"), {})
        assert it.status == "failed"
    assert DBM.get_settings(store)["autotrade_enabled"] == "0"
    assert [s.kind for s in rec.got][-2:] == ["ORDER_FAILED", "AUTOTRADE_DISABLED"]
    # 권한 오류는 한 번에 차단
    ex2, store2, rec2 = make_exec(broker=FailingBroker("prerequisite-required"))
    ex2.on_signal(Signal("ENTRY", "🔵 매수하세요", "AAA", "b", "AAA"), snap(), {})
    assert DBM.get_settings(store2)["autotrade_enabled"] == "0"


class SlowBroker(DryRunBroker):
    """체결되지 않는 지정가."""
    def place(self, intent):
        oid = super().place(intent)
        self.orders[oid] = OrderState(oid, "open", 0, None, "PENDING")
        return oid


def test_executor_cancels_stale_buy_and_cancels_buy_before_sell():
    ex, store, rec = make_exec(broker=SlowBroker())
    it = ex.on_signal(Signal("ENTRY", "🔵 매수하세요", "AAA", "b", "AAA"), snap(), {})
    assert ex.reconcile() == [] and DBM.get_order(store, it.intent_id)["status"] == "open"
    old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds")
    DBM.update_order(store, it.intent_id, created_at=old)
    ex.reconcile()
    assert DBM.get_order(store, it.intent_id)["status"] == "canceled" and rec.got[-1].kind == "ORDER_CANCELED"
    # 미결 매수가 있는 상태에서 매도 신호 → 매수를 먼저 취소하고 매도 접수
    it2 = ex.on_signal(Signal("ENTRY", "🔵 매수하세요", "AAA", "b", "AAA"), snap(100.0, "b9"), {})
    assert DBM.get_order(store, it2.intent_id)["status"] == "sent"
    it3 = ex.on_signal(Signal("SELL", "🔴 매도하세요", "AAA", "b", "AAA"), snap(99.0), {"AAA": {"qty": 3, "avg": 100}})
    assert it3.status == "sent" and DBM.get_order(store, it2.intent_id)["status"] == "canceled"


def test_engine_hooks_pass_signals_and_fill_prices(monkeypatch, tmp_path):
    store = DBM.DB.sqlite().init_schema()
    DBM.seed_watchlist(store, {"AAA": {**CFG, "name": "테스트"}})
    DBM.set_setting(store, "autotrade_enabled", 1)
    rec = Recorder("telegram")
    ex = X.Executor(store, DryRunBroker(), "dry", Dispatcher([rec]))
    monkeypatch.setattr(E, "now_local", sc.fixed_now_local)
    monkeypatch.setattr(MH, "now_local", sc.fixed_now_local)
    monkeypatch.setattr(X, "now_local", sc.fixed_now_local)
    monkeypatch.setattr(E, "BASE_DIR", tmp_path)
    cap = sc.CaptureNotifier()
    eng = E.SignalEngine(sc.FakeClient(sc.scenario_candles()), cap, True, DBM.load_watchlist(store), store, ex)
    assert ex.hours is eng.hours
    eng.evaluate("AAA", {"AAA": 100.8}, {})                       # ENTRY → dry 매수
    assert [s.kind for s in rec.got] == ["ORDER_SENT"]
    eng._reconcile_orders()
    assert rec.got[-1].kind == "ORDER_FILLED"
    for price, holdings in sc.STEPS[2:4]:                          # 보유 → 매도 신호
        eng.evaluate("AAA", {"AAA": price}, holdings)
    assert rec.got[-1].kind == "ORDER_SENT" and rec.got[-1].body.startswith("[DRY] SELL")
    eng._reconcile_orders()                                        # 체결가가 청산 메시지에 반영된다
    assert eng.last_seen["AAA"]["actual"] is True and eng.last_seen["AAA"]["price"] == 100.2
    eng.evaluate("AAA", {"AAA": 100.2}, {})
    assert "자동매매 체결가 기준" in cap.sent[-1][2]
