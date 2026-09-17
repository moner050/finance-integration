"""정책·실행기 — 게이트, 한도, 중복 방지, 가상 장부 체결, 계정별 live, 자동 차단, 엔진 연동."""
from datetime import datetime, timedelta, timezone

import pytest

import alertbot.engine as E
import alertbot.market_hours as MH
import alertbot.trading.executor as X
from alertbot import accounts as ACC
from alertbot import config, crypto
from alertbot import db as DBM
from alertbot.models import Signal
from alertbot.trading.broker import BrokerError, DryRunBroker, OrderState
from alertbot.trading.live import LiveExecutors
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
    s.update({k: str(v) for k, v in over.items()})
    return s


def buy(price=100.0, qty=10, symbol="AAA"):
    return OrderIntent.create("live", symbol, "KR", "BUY", "ENTRY", "LIMIT", price, qty, bar_key="b1")


def sell(kind="SELL", symbol="AAA"):
    return OrderIntent.create("live", symbol, "KR", "SELL", kind, "MARKET", 100.0, 10, ref_avg=98.0)


def ctx(**over):
    base = {"virtual": False, "live_on": True, "settings": settings(), "cfg": CFG, "regular": True, "holdings": {},
            "open_intents": [], "orders_today": [], "realized_pnl_today": {"KRW": 0.0, "USD": 0.0},
            "buying_power": lambda c: 10_000_000, "last_price": None}
    base.update(over)
    return base


def test_policy_gates_and_limits():
    p = RiskPolicy(HARD)
    assert p.check(buy(), ctx()) == (True, "ok")
    assert p.check(buy(), ctx(live_on=False))[1] == "disabled"
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


def test_policy_virtual_book_skips_account_limits_but_keeps_structure():
    """공용 가상 장부는 확정 신호를 전부 기록한다 — 스위치·종목 체크·금액·보유 수·횟수·손실·잔고 한도는 없고, 구조 검사만 한다."""
    p = RiskPolicy(HARD)
    v = dict(virtual=True, live_on=False, cfg={**CFG, "auto_trade": False}, buying_power=lambda c: 0,
             holdings={s: {"qty": 1, "avg": 1} for s in ("X", "Y", "Z")},
             orders_today=[{"symbol": "Q", "side": "BUY", "status": "filled"}] * 99,
             realized_pnl_today={"KRW": -9_999_999, "USD": 0})
    assert p.check(buy(price=1_344_000, qty=1), ctx(**v)) == (True, "ok")
    assert p.check(buy(), ctx(**{**v, "regular": False}))[1] == "outside-regular-hours"
    assert p.check(buy(qty=0), ctx(**v))[1] == "quantity-below-1"
    assert p.check(buy(), ctx(**{**v, "holdings": {"AAA": {"qty": 1, "avg": 1}}}))[1] == "already-holding"
    assert p.check(sell(), ctx(**{**v, "holdings": {}}))[1] == "nothing-to-sell"
    assert p.check(sell(), ctx(**{**v, "holdings": {"AAA": {"qty": 3, "avg": 1}}})) == (True, "ok")


# --- 실행기 -------------------------------------------------------------------

class AccountBroker(DryRunBroker):
    """계정 live 실행기용 가짜 — 그 계정 계좌의 보유를 돌려준다."""
    name = "fake-live"

    def __init__(self, holdings=None):
        super().__init__()
        self.held = holdings if holdings is not None else {}

    def holdings(self):
        return self.held


def make_exec(store=None, broker=None, live=False, live_on=True, scale=1.0, cfg=None):
    store = store or DBM.DB.sqlite().init_schema()
    if not DBM.get_watch_row(store, "AAA"):
        DBM.seed_watchlist(store, {"AAA": cfg or CFG})
    rec = Recorder("telegram")
    account = None
    if live:
        account_id = ACC.add_account(store, f"user{len(ACC.list_accounts(store))}@example.com")
        ACC.update_live(store, account_id, toss_live=live_on, amount_scale=scale)
        account = {"id": account_id, "email": f"user{account_id}", "amount_scale": scale}
    ex = X.Executor(store, broker or (AccountBroker() if live else DryRunBroker()), "live" if live else "dry",
                    Dispatcher([rec]), account=account)
    return ex, store, rec


def snap(price=100.0, bar="b1", cfg=None):
    return {"cfg": cfg or CFG, "market": "KR", "price": price, "bar_key": bar}


def test_executor_requires_account_only_for_live():
    store = DBM.DB.sqlite().init_schema()
    with pytest.raises(ValueError):
        X.Executor(store, DryRunBroker(), "live", Dispatcher([]))
    with pytest.raises(ValueError):
        X.Executor(store, DryRunBroker(), "dry", Dispatcher([]), account={"id": 1, "email": "x"})


def test_executor_dry_buy_then_fill_and_dedupe():
    ex, store, rec = make_exec()
    it = ex.on_signal(Signal("ENTRY", "🔵 매수하세요", "AAA", "b", "AAA"), snap(100.0), {})
    assert it.status == "sent" and it.order_type == "LIMIT" and it.price == 100.0 and it.quantity == 5000  # 50만/100
    # 가상 장부는 접수 즉시 체결이라 접수 알림(주문번호)은 공용 채널로 보내지 않는다 — 체결 알림만
    assert it.account_id is None and rec.got == []
    # 같은 신호봉의 반복 알림은 무시
    assert ex.on_signal(Signal("ENTRY", "🔵 매수하세요", "AAA", "b", "AAA"), snap(100.5), {}) is None
    assert len(DBM.recent_orders(store)) == 1
    fills = ex.reconcile()
    assert fills == [] and DBM.get_order(store, it.intent_id)["status"] == "filled"
    assert [s.kind for s in rec.got] == ["ORDER_FILLED"] and rec.got[0].body == "[DRY] BUY 5000주 @ 100"
    assert rec.got[0].account_id is None                                            # 가상 장부 알림은 공용 채널
    # 열린 의도가 없어졌으니 다른 봉의 신호는 다시 만들 수 있지만, 가상 보유 중이면 정책이 막는다
    it2 = ex.on_signal(Signal("ENTRY", "🔵 매수하세요", "AAA", "b", "AAA"), snap(101.0, "b2"), {"AAA": {"qty": 5000, "avg": 100}})
    assert it2.status == "rejected" and it2.reason == "already-holding"


def test_virtual_book_buys_every_confirmed_signal_with_default_amount():
    """체크 안 한 종목·금액 0 도 가상으로는 산다 — 기본 금액(KRW 100만), 한 주가 더 비싸면 1주."""
    plain = {**CFG, "auto_trade": False, "auto_amount": 0}
    ex, store, rec = make_exec(cfg=plain)
    it = ex.on_signal(Signal("ENTRY", "🔵 매수하세요", "AAA", "b", "AAA"), snap(100.0, cfg=plain), {})
    assert it.status == "sent" and it.quantity == 10_000                              # 100만 / 100
    it = ex.on_signal(Signal("ENTRY", "🔵 매수하세요", "BBB", "b", "BBB"), {**snap(1_344_000, "b7", cfg=plain)}, {})
    assert it.status == "sent" and it.quantity == 1


def test_executor_sell_fill_records_pnl():
    ex, store, rec = make_exec()
    held = {"AAA": {"qty": 5000.0, "avg": 100.0}}
    it = ex.on_signal(Signal("STOP", "🔴 손절하세요", "AAA", "b", "AAA"), snap(95.0), held)
    assert it.status == "sent" and it.order_type == "MARKET" and it.quantity == 5000 and it.ref_avg == 100.0
    fills = ex.reconcile()
    assert fills == [("AAA", 5000.0, 95.0)]
    row = DBM.get_order(store, it.intent_id)
    assert row["status"] == "filled" and row["pnl"] == -25000.0
    assert rec.got == []            # 가상 매도는 접수·체결 알림 없이 엔진의 청산 완료 알림이 결과를 싣는다 (실현손익은 DB·성적표)


def test_live_account_uses_own_holdings_switch_and_scale():
    """계정 live: 엔진이 넘기는 가상 보유가 아니라 자기 계좌 보유를 보고, 계정 스위치·금액 배율을 따르며, 알림은 그 계정으로 간다."""
    ex, store, rec = make_exec(live=True, scale=0.5)
    it = ex.on_signal(Signal("ENTRY", "🔵 매수하세요", "AAA", "b", "AAA"), snap(100.0), {"AAA": {"qty": 9, "avg": 1}})
    assert it.status == "sent" and it.quantity == 2500 and it.account_id == ex.account_id     # 50만 × 0.5 / 100 · 가상 보유 무시
    assert rec.got[-1].account_id == ex.account_id and not rec.got[-1].body.startswith("[DRY]")
    ex.reconcile()
    assert DBM.open_orders(store) == [] and DBM.dry_positions(store) == {}          # live 체결은 가상 장부와 섞이지 않는다
    sold = ex.on_signal(Signal("SELL", "🔴 매도하세요", "AAA", "b", "AAA"), snap(99.0), {})
    assert sold.status == "rejected" and sold.reason == "quantity-below-1"            # 내 계좌엔 아직 없다 (가짜 브로커 보유 비어 있음 → 0주)
    ex.broker.held = {"AAA": {"qty": 7.0, "avg": 100.0}}
    sold = ex.on_signal(Signal("SELL", "🔴 매도하세요", "AAA", "b", "AAA"), snap(99.0, "b3"), {})
    assert sold.status == "sent" and sold.quantity == 7
    ex.reconcile()                                                                    # 계정 live 매도 체결은 그 계정에 실현손익과 함께 알린다
    assert rec.got[-1].kind == "ORDER_FILLED" and rec.got[-1].body == "SELL 7주 @ 99\n실현손익 -7 KRW"
    ACC.update_live(store, ex.account_id, toss_live=False)                           # 백오피스에서 끄면 곧바로 막힌다 — 계좌 조회도 없다
    ex.broker.held = None
    assert ex.on_signal(Signal("ENTRY", "🔵 매수하세요", "AAA", "b", "AAA"), snap(100.0, "b9"), {}) is None
    ACC.update_live(store, ex.account_id, toss_live=True)
    ex.broker.held = None                                                             # 계좌 조회 실패 → 주문 보류
    assert ex.on_signal(Signal("ENTRY", "🔵 매수하세요", "AAA", "b", "AAA"), snap(100.0, "b10"), {}) is None


def test_executor_kill_switch_and_unrelated_kinds():
    ex, store, rec = make_exec(live=True, live_on=False)
    assert ex.on_signal(Signal("ENTRY", "🔵 매수하세요", "AAA", "b", "AAA"), snap(), {}) is None      # 스위치 OFF — 의도도 조회도 없다
    ACC.update_live(store, ex.account_id, toss_live=True)
    plain = {**CFG, "auto_trade": False}
    assert ex.on_signal(Signal("ENTRY", "🔵 매수하세요", "AAA", "b", "AAA"), snap(cfg=plain), {}) is None  # live 자동매매 종목이 아니다
    assert rec.got == [] and DBM.recent_orders(store) == []

    class Down(AccountBroker):
        def holdings(self):
            raise SystemExit("403 — 허용 IP 미등록")                                  # 토스 인증 실패가 엔진으로 새지 않는다
    ex.broker = Down()
    assert ex.on_signal(Signal("ENTRY", "🔵 매수하세요", "AAA", "b", "AAA"), snap(), {}) is None
    assert ex.on_signal(Signal("EXIT_HALF", "🟡 절반", "AAA", "b", "AAA"), snap(), {"AAA": {"qty": 1, "avg": 1}}) is None
    assert ex.on_signal(Signal("ADDON", "🔵 추가", "AAA", "b", "AAA"), snap(), {}) is None


class FailingBroker(AccountBroker):
    def __init__(self, code="insufficient-buying-power"):
        super().__init__()
        self.code = code

    def place(self, intent):
        raise BrokerError(self.code, "실패")


def test_executor_auto_disables_after_failures():
    ex, store, rec = make_exec(broker=FailingBroker(), live=True)
    for i in range(3):
        it = ex.on_signal(Signal("ENTRY", "🔵 매수하세요", "AAA", "b", "AAA"), snap(100.0, f"b{i}"), {})
        assert it.status == "failed"
    assert ACC.get(store, ex.account_id)["toss_live"] is False                        # 그 계정 스위치만 꺼진다
    assert [s.kind for s in rec.got][-2:] == ["ORDER_FAILED", "AUTOTRADE_DISABLED"] and rec.got[-1].account_id == ex.account_id
    # 권한 오류는 한 번에 차단
    ex2, store2, rec2 = make_exec(broker=FailingBroker("prerequisite-required"), live=True)
    ex2.on_signal(Signal("ENTRY", "🔵 매수하세요", "AAA", "b", "AAA"), snap(), {})
    assert ACC.get(store2, ex2.account_id)["toss_live"] is False


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


def test_live_daily_report_counts_only_that_account(monkeypatch):
    ex, store, rec = make_exec(live=True)
    other, _, _ = make_exec(store=store, live=True)
    base = {"market": "KR", "order_type": "MARKET", "price": 100.0, "quantity": 10, "amount": 1000, "bar_key": None,
            "reason": None, "order_id": "o", "filled_qty": 10, "status": "filled", "updated_at": None, "mode": "live",
            "side": "SELL", "kind": "SELL", "ref_avg": 100.0, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    DBM.insert_order(store, {**base, "intent_id": "a", "symbol": "AAA", "avg_price": 103.0, "pnl": 30.0, "account_id": ex.account_id})
    DBM.insert_order(store, {**base, "intent_id": "b", "symbol": "BBB", "avg_price": 99.0, "pnl": -10.0, "account_id": other.account_id})
    report = ex.daily_report("KR")
    assert report.startswith("청산 1건: 1익절 0손절·본전 (승률 100%)") and "실현손익 +30 KRW" in report and "BBB" not in report
    assert X.Executor(store, DryRunBroker(), "dry", Dispatcher([])).daily_report("KR") == ""


def test_live_executors_follow_accounts_keys_and_switches(monkeypatch):
    monkeypatch.setattr(config, "MASTER_KEY", crypto.generate_key())
    store = DBM.DB.sqlite().init_schema()
    a = ACC.add_account(store, "a@example.com")
    b = ACC.add_account(store, "b@example.com")
    for acc in (a, b):
        ACC.save_keys(store, acc, "toss", {"client_id": f"id{acc}", "client_secret": "s"})
        ACC.save_keys(store, acc, "telegram", {"bot_token": "1:T", "chat_id": str(acc)})
    built, fail = [], set()
    clock = [0.0]

    def build(st, account):
        built.append(account["email"])
        if account["id"] in fail:
            return None
        return X.Executor(st, AccountBroker(), "live", Dispatcher([]), account=account)

    live = LiveExecutors(store, build=build, clock=lambda: clock[0])
    live.refresh()
    assert live.executors == [] and built == []                                     # 스위치가 꺼져 있다
    ACC.update_live(store, a, toss_live=True)
    ACC.update_live(store, b, toss_live=True)
    fail.add(b)
    live.refresh()
    assert [ex.account_id for ex in live.executors] == [a] and built == ["a@example.com", "b@example.com"]
    live.refresh()
    assert len(built) == 2                                                          # 바뀐 게 없고 재시도 시각 전
    fail.clear()
    clock[0] = 601.0
    live.refresh()                                                                  # 실패 계정은 10분 뒤 다시 시도
    assert sorted(ex.account_id for ex in live.executors) == [a, b] and built.count("a@example.com") == 1
    ACC.update_live(store, a, amount_scale=2.0)                                     # 키가 같으면 실행기는 두고 설정만
    live.refresh()
    assert built.count("a@example.com") == 1 and next(ex for ex in live.executors if ex.account_id == a).account["amount_scale"] == 2.0
    ACC.save_keys(store, a, "toss", {"client_id": "new", "client_secret": "s"})     # 키가 바뀌면 다시 만든다
    live.refresh()
    assert built.count("a@example.com") == 2
    ACC.update_live(store, b, toss_live=False)                                      # 스위치 OFF → 치운다
    live.refresh()
    assert [ex.account_id for ex in live.executors] == [a]


def test_engine_hooks_pass_signals_and_fill_prices(monkeypatch, tmp_path):
    store = DBM.DB.sqlite().init_schema()
    DBM.seed_watchlist(store, {"AAA": {**CFG, "name": "테스트"}})
    rec = Recorder("telegram")
    ex = X.Executor(store, DryRunBroker(), "dry", Dispatcher([rec]))
    monkeypatch.setattr(E, "now_local", sc.fixed_now_local)
    monkeypatch.setattr(MH, "now_local", sc.fixed_now_local)
    monkeypatch.setattr(X, "now_local", sc.fixed_now_local)
    monkeypatch.setattr(E, "DATA_DIR", tmp_path)
    cap = sc.CaptureNotifier()
    eng = E.SignalEngine(sc.FakeClient(sc.scenario_candles()), cap, DBM.load_watchlist(store), store, ex)
    assert ex.hours is eng.hours
    eng.evaluate("AAA", {"AAA": 100.8}, {})                       # ENTRY → 가상 매수 (접수 알림 없음)
    assert rec.got == []
    eng._reconcile_orders()
    assert [s.kind for s in rec.got] == ["ORDER_FILLED"]
    sc.run_steps(eng, [2, 3])                                      # 보유 → 매도 신호 → 가상 매도
    assert any(o["side"] == "SELL" for o in DBM.recent_orders(store)) and len(rec.got) == 1
    eng._reconcile_orders()                                        # 체결가가 청산 메시지에 반영된다
    assert eng.last_seen["AAA"]["actual"] is True and eng.last_seen["AAA"]["price"] == 100.2
    assert [s.kind for s in rec.got] == ["ORDER_FILLED"]           # 가상 매도 체결은 청산 완료 알림이 대신한다
    eng.evaluate("AAA", {"AAA": 100.2}, {})
    assert "가상 장부 체결가 기준" in cap.sent[-1][2]


def test_engine_fans_signals_out_to_live_accounts(monkeypatch, tmp_path):
    """확정 신호는 가상 장부와 계정별 live 실행기에 모두 간다. live 는 자기 계좌 보유로 판단하고, 한 계정 오류가 다른 쪽을 막지 않는다."""
    store = DBM.DB.sqlite().init_schema()
    DBM.seed_watchlist(store, {"AAA": {**CFG, "name": "테스트"}})
    for mod in (E, MH, X):
        monkeypatch.setattr(mod, "now_local", sc.fixed_now_local)
    monkeypatch.setattr(E, "DATA_DIR", tmp_path)
    virtual = X.Executor(store, DryRunBroker(), "dry", Dispatcher([Recorder("telegram")]))
    good, _, good_rec = make_exec(store=store, live=True)

    class Broken:
        account_id = 999

        def on_signal(self, *a):
            raise RuntimeError("boom")

        def reconcile(self):
            raise RuntimeError("boom")

    class Desk:
        hours = None
        executors = [Broken(), good]

        def refresh(self):
            pass

    desk = Desk()
    eng = E.SignalEngine(sc.FakeClient(sc.scenario_candles()), sc.CaptureNotifier(), DBM.load_watchlist(store), store, virtual, desk)
    assert desk.hours is eng.hours
    eng.evaluate("AAA", {"AAA": 100.8}, {})
    orders = DBM.recent_orders(store)
    assert sorted((o["account_id"] or 0, o["status"]) for o in orders) == [(0, "sent"), (good.account_id, "sent")]
    assert [s.kind for s in good_rec.got] == ["ORDER_SENT"] and good_rec.got[0].account_id == good.account_id
    assert "\n주문번호 " in good_rec.got[0].body                     # 계정 채널의 live 접수 알림에는 주문번호가 남는다
    eng._reconcile_orders()                                        # 한 계정의 추적 오류도 나머지를 막지 않는다
    assert all(o["status"] == "filled" for o in DBM.recent_orders(store))
    mine = next(o for o in orders if o["account_id"] is None)
    assert eng._book_holdings() == {"AAA": {"qty": mine["quantity"], "avg": mine["price"], "market": "KR"}}    # 엔진 보유는 가상 장부뿐


def test_engine_repeat_entry_does_not_reorder(monkeypatch, tmp_path):
    """확정 신호는 한 번만 실행기에 간다(반복이 없다). 대기 신호는 승격될 때 원래 대기 신호봉으로 한 번 간다."""
    store = DBM.DB.sqlite().init_schema()
    DBM.seed_watchlist(store, {"AAA": {**CFG, "name": "테스트"}})
    ex = X.Executor(store, SlowBroker(), "dry", Dispatcher([Recorder("telegram")]))   # 체결되지 않는 지정가
    for mod in (E, MH, X):
        monkeypatch.setattr(mod, "now_local", sc.fixed_now_local)
    monkeypatch.setattr(E, "DATA_DIR", tmp_path)
    engine_rec = Recorder("engine")
    client = sc.FakeClient(sc.scenario_candles())
    eng = E.SignalEngine(client, Dispatcher([engine_rec]), DBM.load_watchlist(store), store, ex)   # 진짜 쿨다운
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
    weak = sc.scenario_candles()
    weak[-1] = bar(datetime.fromisoformat(weak[-1]["timestamp"]), 100.8, 2500, high=100.85, low=100.3)
    client2, rec2 = sc.FakeClient(weak), Recorder("engine")
    eng2 = E.SignalEngine(client2, Dispatcher([rec2]), DBM.load_watchlist(store2), store2,
                          X.Executor(store2, SlowBroker(), "dry", Dispatcher([Recorder("telegram")])))
    eng2.evaluate("AAA", {"AAA": 100.8}, {})
    assert rec2.got[-1].kind == "ENTRY_WATCH" and DBM.recent_orders(store2) == []
    client2.candles = weak + [bar(datetime.fromisoformat(weak[-1]["timestamp"]) + timedelta(minutes=1), 100.9, 4000,
                                  high=100.95, low=100.8)]
    eng2.evaluate("AAA", {"AAA": 100.9}, {})
    assert rec2.got[-1].kind == "ENTRY" and [o["bar_key"][11:16] for o in DBM.recent_orders(store2)] == ["10:01"]


def test_virtual_fills_become_holdings_and_round_trip_to_report(monkeypatch, tmp_path):
    """가상 장부는 진짜 모의매매다: 가상 매수 체결이 엔진 보유로 잡혀 매도 신호에 전량 가상 매도가 나가고, 청산 완료·거래 기록·성적표까지 이어진다.
    실계좌 보유는 어디에도 섞이지 않는다 (엔진은 계좌를 읽지 않는다)."""
    store = DBM.DB.sqlite().init_schema()
    DBM.seed_watchlist(store, {"AAA": {**CFG, "name": "테스트"}})
    rec = Recorder("telegram")
    ex = X.Executor(store, DryRunBroker(), "dry", Dispatcher([rec]))
    for mod in (E, MH, X):
        monkeypatch.setattr(mod, "now_local", sc.fixed_now_local)
    monkeypatch.setattr(E, "DATA_DIR", tmp_path)
    cap = sc.CaptureNotifier()

    class NoAccountClient(sc.FakeClient):
        def get_holdings(self):
            raise AssertionError("엔진이 실계좌를 읽었다")

    eng = E.SignalEngine(NoAccountClient(sc.scenario_candles()), cap, DBM.load_watchlist(store), store, ex)
    assert eng._book_holdings() == {} and ex.dry_holdings() == {}
    eng.evaluate("AAA", {"AAA": 100.8}, {})                               # 매수하세요 → 가상 지정가 매수 (다음 사이클에 체결)
    assert ex.dry_holdings() == {}
    eng._reconcile_orders()
    order = DBM.recent_orders(store)[0]
    held = eng._book_holdings()
    assert order["status"] == "filled" and held == {"AAA": {"qty": order["quantity"], "avg": order["price"], "market": "KR"}}
    assert held["AAA"]["qty"] >= 1 and held["AAA"]["avg"] > 100.8      # 수량 = 50만 ÷ 지정가(신호가 +0.3%)
    eng.evaluate("AAA", {"AAA": 100.9}, held)                             # 보유로 관리 — 평단은 가상 체결가
    assert eng.state["AAA"] == "보유" and eng.last_seen["AAA"]["avg"] == order["price"]
    eng.client.candles = eng.client.candles + [sc.STEP_BARS[3]]
    eng.evaluate("AAA", {"AAA": 100.2}, eng._book_holdings())             # 매도선 이탈 → 매도하세요 → 가상 전량 시장가 매도
    sell = next(o for o in DBM.recent_orders(store) if o["side"] == "SELL")     # 매수와 같은 초에 생겨 정렬이 섞일 수 있다
    assert sell["quantity"] == held["AAA"]["qty"] and cap.signals[-1].kind == "SELL"
    assert cap.signals[-1].account.startswith("가상 손익 ") and f"(평단 {order['price']:,.0f} → " in cap.signals[-1].account
    eng._reconcile_orders()
    assert DBM.get_order(store, sell["intent_id"])["status"] == "filled" and ex.dry_holdings() == {}
    assert eng.last_seen["AAA"]["actual"] is True and eng.last_seen["AAA"]["price"] == 100.2
    eng.evaluate("AAA", {"AAA": 100.2}, eng._book_holdings())             # 가상 보유가 사라짐 → 관망 + 청산 완료 + 거래 기록
    assert eng.state["AAA"] == "관망" and cap.sent[-1][0] == "✅ 손절 완료" and "가상 장부 체결가 기준" in cap.sent[-1][2]
    assert eng.trades.daily_summary("KR").startswith("0익절 1손절 (승률 0%)")
    # 실행기가 없으면(알림만) 엔진 보유는 비어 있다
    eng.executor = None
    assert eng._book_holdings() == {}
