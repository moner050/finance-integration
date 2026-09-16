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
from tests.conftest import bar
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
    monkeypatch.setattr(E, "DATA_DIR", tmp_path)
    cap = sc.CaptureNotifier()
    eng = E.SignalEngine(sc.FakeClient(sc.scenario_candles()), cap, True, DBM.load_watchlist(store), store, ex)
    assert ex.hours is eng.hours
    eng.evaluate("AAA", {"AAA": 100.8}, {})                       # ENTRY → dry 매수
    assert [s.kind for s in rec.got] == ["ORDER_SENT"]
    eng._reconcile_orders()
    assert rec.got[-1].kind == "ORDER_FILLED"
    sc.run_steps(eng, [2, 3])                                      # 보유 → 매도 신호
    assert rec.got[-1].kind == "ORDER_SENT" and rec.got[-1].body.startswith("[DRY] SELL")
    eng._reconcile_orders()                                        # 체결가가 청산 메시지에 반영된다
    assert eng.last_seen["AAA"]["actual"] is True and eng.last_seen["AAA"]["price"] == 100.2
    eng.evaluate("AAA", {"AAA": 100.2}, {})
    assert "자동매매 체결가 기준" in cap.sent[-1][2]


def test_engine_repeat_entry_does_not_reorder(monkeypatch, tmp_path):
    """확정 신호는 한 번만 실행기에 간다(반복이 없다). 대기 신호는 승격될 때 원래 대기 신호봉으로 한 번 간다."""
    store = DBM.DB.sqlite().init_schema()
    DBM.seed_watchlist(store, {"AAA": {**CFG, "name": "테스트"}})
    DBM.set_setting(store, "autotrade_enabled", 1)
    ex = X.Executor(store, SlowBroker(), "dry", Dispatcher([Recorder("telegram")]))   # 체결되지 않는 지정가
    for mod in (E, MH, X):
        monkeypatch.setattr(mod, "now_local", sc.fixed_now_local)
    monkeypatch.setattr(E, "DATA_DIR", tmp_path)
    engine_rec = Recorder("engine")
    client = sc.FakeClient(sc.scenario_candles())
    eng = E.SignalEngine(client, Dispatcher([engine_rec]), True, DBM.load_watchlist(store), store, ex)   # 진짜 쿨다운
    eng.evaluate("AAA", {"AAA": 100.8}, {})                               # ENTRY → 매수 의도 1 (미체결)
    assert [o["bar_key"][11:16] for o in DBM.recent_orders(store)] == ["10:01"]
    last = datetime.fromisoformat(client.candles[-1]["timestamp"])
    client.candles = client.candles + [bar(last + timedelta(minutes=1), 100.9, 1000, high=100.95, low=100.8)]
    eng.evaluate("AAA", {"AAA": 100.9}, {})                               # 다음 봉, 쿨다운 안 → 알림도 실행기도 침묵
    assert len(DBM.recent_orders(store)) == 1 and len(engine_rec.got) == 1
    # 미체결 매수가 TTL 로 취소돼도 확정 신호는 반복 알림이 없으니 다시 사지 않는다 — 신호 포지션(보유)으로 관리만 이어진다
    intent_id = DBM.recent_orders(store)[0]["intent_id"]
    DBM.update_order(store, intent_id, created_at=(datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds"))
    eng._reconcile_orders()
    assert DBM.get_order(store, intent_id)["status"] == "canceled"
    eng.notify.last_sent.clear()                                          # 15분이 지난 것으로
    eng.evaluate("AAA", {"AAA": 100.9}, {})
    assert len(engine_rec.got) == 1 and len(DBM.recent_orders(store)) == 1 and eng.state["AAA"] == "보유"
    # 대기 신호는 실행기 대상이 아니고, 승격될 때 한 번 산다 — 의도의 신호봉은 원래 대기 신호봉(10:01)이다
    store2 = DBM.DB.sqlite().init_schema()
    DBM.seed_watchlist(store2, {"AAA": {**CFG, "name": "테스트"}})
    DBM.set_setting(store2, "autotrade_enabled", 1)
    weak = sc.scenario_candles()
    weak[-1] = bar(datetime.fromisoformat(weak[-1]["timestamp"]), 100.8, 2500, high=100.85, low=100.3)
    client2, rec2 = sc.FakeClient(weak), Recorder("engine")
    eng2 = E.SignalEngine(client2, Dispatcher([rec2]), True, DBM.load_watchlist(store2), store2,
                          X.Executor(store2, SlowBroker(), "dry", Dispatcher([Recorder("telegram")])))
    eng2.evaluate("AAA", {"AAA": 100.8}, {})
    assert rec2.got[-1].kind == "ENTRY_WATCH" and DBM.recent_orders(store2) == []
    client2.candles = weak + [bar(datetime.fromisoformat(weak[-1]["timestamp"]) + timedelta(minutes=1), 100.9, 4000,
                                  high=100.95, low=100.8)]
    eng2.evaluate("AAA", {"AAA": 100.9}, {})
    assert rec2.got[-1].kind == "ENTRY" and [o["bar_key"][11:16] for o in DBM.recent_orders(store2)] == ["10:01"]


def test_dry_fills_become_holdings_and_round_trip_to_report(monkeypatch, tmp_path):
    """dry 는 진짜 모의매매다: 가상 매수 체결이 엔진 보유로 잡혀 매도 신호에 전량 모의 매도가 나가고, 청산 완료·거래 기록·성적표까지 이어진다."""
    store = DBM.DB.sqlite().init_schema()
    DBM.seed_watchlist(store, {"AAA": {**CFG, "name": "테스트"}})
    DBM.set_setting(store, "autotrade_enabled", 1)
    rec = Recorder("telegram")
    ex = X.Executor(store, DryRunBroker(), "dry", Dispatcher([rec]))
    for mod in (E, MH, X):
        monkeypatch.setattr(mod, "now_local", sc.fixed_now_local)
    monkeypatch.setattr(E, "DATA_DIR", tmp_path)
    cap = sc.CaptureNotifier()
    eng = E.SignalEngine(sc.FakeClient(sc.scenario_candles()), cap, True, DBM.load_watchlist(store), store, ex)
    assert eng._with_dry({}) == {} and ex.dry_holdings() == {}
    eng.evaluate("AAA", {"AAA": 100.8}, {})                               # 매수하세요 → dry 지정가 매수 (다음 사이클에 체결)
    assert ex.dry_holdings() == {} and eng._with_dry({}) == {}
    eng._reconcile_orders()
    order = DBM.recent_orders(store)[0]
    held = eng._with_dry({})
    assert order["status"] == "filled" and held == {"AAA": {"qty": order["quantity"], "avg": order["price"], "market": "KR"}}
    assert held["AAA"]["qty"] >= 1 and held["AAA"]["avg"] > 100.8      # 수량 = 50만 ÷ 지정가(신호가 +0.3%)
    assert eng._with_dry({"BBB": {"qty": 1.0, "avg": 5.0}}) == {"BBB": {"qty": 1.0, "avg": 5.0}, **held}   # 실제 보유와 합친다
    eng.evaluate("AAA", {"AAA": 100.9}, held)                             # 보유로 관리 — 내 평단은 모의 체결가
    assert eng.state["AAA"] == "보유" and eng.last_seen["AAA"]["avg"] == order["price"]
    eng.client.candles = eng.client.candles + [sc.STEP_BARS[3]]
    eng.evaluate("AAA", {"AAA": 100.2}, eng._with_dry({}))               # 매도선 이탈 → 매도하세요 → dry 전량 시장가 매도
    sell = next(o for o in DBM.recent_orders(store) if o["side"] == "SELL")     # 매수와 같은 초에 생겨 정렬이 섞일 수 있다
    assert sell["quantity"] == held["AAA"]["qty"] and cap.signals[-1].kind == "SELL"
    assert cap.signals[-1].account.startswith("손익 ") and f"평단 {order['price']}" in cap.signals[-1].account
    eng._reconcile_orders()
    assert DBM.get_order(store, sell["intent_id"])["status"] == "filled" and ex.dry_holdings() == {}
    assert eng.last_seen["AAA"]["actual"] is True and eng.last_seen["AAA"]["price"] == 100.2
    eng.evaluate("AAA", {"AAA": 100.2}, eng._with_dry({}))               # 모의 보유가 사라짐 → 관망 + 청산 완료 + 거래 기록
    assert eng.state["AAA"] == "관망" and cap.sent[-1][0] == "✅ 손절 완료" and "자동매매 체결가 기준" in cap.sent[-1][2]
    assert eng.trades.daily_summary("KR").startswith("0익절 1손절 (승률 0%)")
    # live 실행기는 보유를 건드리지 않는다
    eng.executor = X.Executor(store, DryRunBroker(), "live", Dispatcher([rec]))
    assert eng._with_dry({"CCC": {"qty": 2.0, "avg": 1.0}}) == {"CCC": {"qty": 2.0, "avg": 1.0}}
