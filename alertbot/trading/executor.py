"""실행기 — 신호를 주문 의도로 바꾸고, 정책을 거쳐, 브로커에 내고, 미결을 추적한다.

엔진은 알림을 낸 직후 on_signal 을, 사이클 끝에 reconcile 을 부른다. 실행기 안의 어떤 예외도
엔진을 멈추면 안 되므로 엔진 쪽 호출은 try/except 로 감싼다.

자동 범위: 매수 ENTRY / 전량 매도 STOP·SELL·EXIT_FULL·CLOSE_WARN(당일 청산 종목에만 온다). 나머지 신호는 무시한다.
"""

import logging
import math
from datetime import datetime, timedelta, timezone

from .. import db
from ..config import (AUTOTRADE_BUY_BUFFER_PCT, AUTOTRADE_BUY_TTL_MIN, AUTOTRADE_HARD_MAX_AMOUNT_KRW,
                      AUTOTRADE_HARD_MAX_AMOUNT_USD)
from ..models import Signal
from ..timeutil import now_local
from .broker import BrokerError, round_price
from .models import OPEN_STATUSES, OrderIntent
from .policy import RiskPolicy

log = logging.getLogger("scalper")

BUY_KINDS = ("ENTRY",)
SELL_KINDS = ("STOP", "SELL", "EXIT_FULL", "CLOSE_WARN")
MAX_CONSECUTIVE_FAILURES = 3


class Executor:
    def __init__(self, store, broker, mode: str, notify, hours=None):
        self.store = store
        self.broker = broker
        self.mode = mode                    # dry | live
        self.notify = notify                # Dispatcher
        self.hours = hours                  # MarketHours (엔진이 넣어 준다)
        self.policy = RiskPolicy({"KRW": AUTOTRADE_HARD_MAX_AMOUNT_KRW, "USD": AUTOTRADE_HARD_MAX_AMOUNT_USD})
        self.failures = 0
        self.tag = "[DRY] " if mode == "dry" else ""
        log.info("자동매매 실행기 준비: 모드 %s, 브로커 %s (킬 스위치·종목별 auto_trade 가 켜져야 주문이 나간다)",
                 mode, getattr(broker, "name", type(broker).__name__))

    # -- 신호 → 의도 ------------------------------------------------------------
    def on_signal(self, signal: Signal, snap: dict, holdings: dict):
        if signal.kind not in BUY_KINDS + SELL_KINDS or not snap:
            return None
        settings = db.get_settings(self.store)
        cfg = snap["cfg"]
        symbol, market = signal.symbol, snap["market"]
        if signal.kind in BUY_KINDS:
            intent = self._buy_intent(symbol, market, cfg, snap)
            if intent is None:
                return None
            if self._duplicate_buy(intent):
                return None
        else:
            held = holdings.get(symbol) or {}
            intent = self._sell_intent(symbol, market, signal.kind, snap, held)
        ctx = self._context(settings, cfg, market, holdings, intent)
        ok, reason = self.policy.check(intent, ctx)
        if not ok:
            intent.status, intent.reason = "rejected", reason
            db.insert_order(self.store, intent.to_row())
            log.info("%s자동매매 보류 %s %s %s: %s", self.tag, symbol, intent.side, intent.kind, reason)
            return intent
        if intent.side == "SELL":
            self._cancel_open_buys(symbol)        # 반대 방향 미결이 있으면 매도가 거절된다
        db.insert_order(self.store, intent.to_row())
        return self._place(intent)

    def _buy_intent(self, symbol, market, cfg, snap):
        ref = float(snap["price"])
        limit = round_price(ref * (1 + AUTOTRADE_BUY_BUFFER_PCT / 100), market, "BUY")
        amount = float(cfg.get("auto_amount") or 0)
        qty = math.floor(amount / limit) if limit > 0 else 0
        # 반복 알림은 원래 신호봉(signal_bar)을 넘긴다 — 같은 진입 기회에 두 번 사지 않는다 (_duplicate_buy)
        return OrderIntent.create(self.mode, symbol, market, "BUY", "ENTRY", "LIMIT", limit, qty,
                                  bar_key=snap.get("signal_bar") or snap.get("bar_key"))

    def _sell_intent(self, symbol, market, kind, snap, held):
        qty = float(held.get("qty", 0) or 0)
        if self.mode == "live" and qty > 0:
            try:
                qty = min(qty, self.broker.sellable_quantity(symbol))
            except BrokerError as e:
                log.warning("%s 매도가능수량 조회 실패 — 보유 수량으로 진행: %s", symbol, e)
        if market == "KR":
            qty = math.floor(qty)
        return OrderIntent.create(self.mode, symbol, market, "SELL", kind, "MARKET", float(snap["price"]), qty,
                                  bar_key=snap.get("bar_key"), ref_avg=float(held.get("avg") or 0) or None)

    def _duplicate_buy(self, intent) -> bool:
        """같은 신호봉으로 이미 의도를 만들었으면(거절 포함) 다시 만들지 않는다. 진입대기 반복 알림 대응."""
        rows = db.orders_since(self.store, self._day_start(), self.mode)
        return any(o["symbol"] == intent.symbol and o["side"] == "BUY" and o["bar_key"] == intent.bar_key for o in rows)

    def _context(self, settings, cfg, market, holdings, intent) -> dict:
        today = db.orders_since(self.store, self._day_start(), self.mode)
        pnl = {"KRW": 0.0, "USD": 0.0}
        for o in today:
            if o["side"] == "SELL" and o["status"] in ("filled", "partial") and o.get("pnl") is not None:
                pnl["KRW" if o["market"] == "KR" else "USD"] += float(o["pnl"])
        regular = True
        if self.hours is not None:
            info = self.hours.info(market)
            hm = now_local(market)
            hm = hm.hour * 60 + hm.minute
            regular = (not info["closed"]) and info["open"] <= hm < info["close"]
        return {"settings": settings, "cfg": cfg, "regular": regular, "holdings": holdings,
                "open_intents": db.open_orders(self.store), "orders_today": today, "realized_pnl_today": pnl,
                "buying_power": self.broker.buying_power, "last_price": intent.price if intent.side == "SELL" else None}

    # -- 브로커 ---------------------------------------------------------------
    def _place(self, intent):
        try:
            order_id = self.broker.place(intent)
        except BrokerError as e:
            self.failures += 1
            intent.status, intent.reason = "failed", str(e)[:250]
            db.update_order(self.store, intent.intent_id, status="failed", reason=intent.reason, price=intent.price)
            self._emit("ORDER_FAILED", "🚫 주문 실패", intent, f"{intent.side} {intent.quantity:g}주 @ {intent.price}\n{e}")
            if self.failures >= MAX_CONSECUTIVE_FAILURES or e.code in ("prerequisite-required", "no-account"):
                self._disable(f"주문 실패 {self.failures}회 연속 ({e.code})")
            return intent
        self.failures = 0
        intent.status, intent.order_id = "sent", order_id
        db.update_order(self.store, intent.intent_id, status="sent", order_id=order_id, price=intent.price)
        self._emit("ORDER_SENT", "📤 주문 접수", intent,
                   f"{intent.side} {intent.order_type} {intent.quantity:g}주 @ {intent.price} "
                   f"(약 {intent.amount:,.0f} {intent.currency})\n주문번호 {order_id}")
        return intent

    def _cancel_open_buys(self, symbol):
        for o in db.open_orders(self.store, symbol):
            if o["side"] != "BUY":
                continue
            try:
                self.broker.cancel(o["order_id"])
                db.update_order(self.store, o["intent_id"], status="canceled", reason="sell-signal")
                log.info("%s 매도 전 미체결 매수 취소 %s", symbol, o["order_id"])
            except BrokerError as e:
                log.warning("%s 매수 취소 실패: %s", symbol, e)

    # -- 미결 추적 ---------------------------------------------------------------
    def reconcile(self) -> list:
        """열린 의도를 브로커 상태와 맞춘다. 체결된 매도를 [(symbol, qty, avg_price)] 로 돌려준다."""
        fills = []
        for o in db.open_orders(self.store):
            intent = OrderIntent.from_row(o)
            try:
                st = self.broker.get(intent.order_id)
            except (BrokerError, KeyError) as e:
                log.warning("주문 상태 조회 실패 %s: %s", intent.order_id, e)
                continue
            if st.status == "open":
                age = datetime.now(timezone.utc) - datetime.fromisoformat(intent.created_at)
                if intent.side == "BUY" and age > timedelta(minutes=AUTOTRADE_BUY_TTL_MIN):
                    try:
                        self.broker.cancel(intent.order_id)
                        db.update_order(self.store, intent.intent_id, status="canceled", reason="ttl",
                                        filled_qty=st.filled_qty, avg_price=st.avg_price)
                        self._emit("ORDER_CANCELED", "↩ 매수 취소", intent,
                                   f"{AUTOTRADE_BUY_TTL_MIN}분 미체결 (체결 {st.filled_qty:g}주)")
                    except BrokerError as e:
                        log.warning("TTL 취소 실패 %s: %s", intent.order_id, e)
                elif st.status != intent.status:
                    db.update_order(self.store, intent.intent_id, status="open", filled_qty=st.filled_qty)
                continue
            fields = {"status": st.status, "filled_qty": st.filled_qty, "avg_price": st.avg_price}
            if st.status == "filled" and st.filled_qty <= 0:
                fields["filled_qty"] = intent.quantity
            if st.status in ("canceled", "failed") and st.filled_qty > 0:
                fields["status"] = "partial"
            if intent.side == "SELL" and fields["status"] in ("filled", "partial") and intent.ref_avg and st.avg_price:
                fields["pnl"] = round((st.avg_price - intent.ref_avg) * fields["filled_qty"], 4)
            db.update_order(self.store, intent.intent_id, **fields)
            if fields["status"] in ("filled", "partial"):
                pnl_txt = f"\n실현손익 {fields['pnl']:+,.0f} {intent.currency}" if fields.get("pnl") is not None else ""
                self._emit("ORDER_FILLED", "✅ 체결", intent,
                           f"{intent.side} {fields['filled_qty']:g}주 @ {st.avg_price or intent.price}{pnl_txt}")
                if intent.side == "SELL":
                    fills.append((intent.symbol, fields["filled_qty"], st.avg_price or intent.price))
            else:
                self._emit("ORDER_FAILED", "🚫 주문 종료", intent, f"{intent.side} 상태 {st.raw_status}")
        return fills

    # -- 공통 -----------------------------------------------------------------
    def _disable(self, why: str):
        db.set_setting(self.store, "autotrade_enabled", 0)
        log.warning("자동매매 자동 차단: %s", why)
        self.notify.send(Signal("AUTOTRADE_DISABLED", "⛔ 자동매매 차단", "시스템",
                                f"{self.tag}{why}\n백오피스에서 원인을 확인하고 다시 켜야 한다"))

    def _emit(self, kind, title, intent, body):
        self.notify.send(Signal(kind, title, intent.symbol, f"{self.tag}{body}", intent.symbol))

    @staticmethod
    def _day_start() -> str:
        """오늘 00:00 KST 를 UTC ISO 로. 하루 집계 기준."""
        kst = now_local("KR").replace(hour=0, minute=0, second=0, microsecond=0)
        return kst.astimezone(timezone.utc).isoformat(timespec="seconds")
