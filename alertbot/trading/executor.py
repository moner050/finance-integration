"""실행기 — 신호를 주문 의도로 바꾸고, 정책을 거쳐, 브로커에 내고, 미결을 추적한다.

실행기 하나 = 장부 하나다.
  공용 가상 장부 (account=None, mode dry): DryRunBroker 로 가상 체결. 확정 신호를 전부 사고팔며(한도 없음) 실계좌는 보지 않는다 —
      엔진이 넘겨 주는 보유도 이 장부의 가상 보유뿐이다. 알림은 공용 채널.
  계정 live (account=계정, mode live): 그 계정 키의 TossOrderClient 로 실제 주문. 보유는 그 계정 계좌에서 직접 읽는다.
      계정 live 스위치·종목 auto_trade·한도(계정마다 따로 집계)를 거치고, 알림은 그 계정의 텔레그램으로만.
엔진은 알림을 낸 직후 on_signal 을, 사이클 끝에 reconcile 을 부른다. 실행기 안의 어떤 예외도
엔진을 멈추면 안 되므로 엔진 쪽 호출은 try/except 로 감싼다.

자동 범위: 매수 ENTRY / 전량 매도 STOP·SELL·EXIT_FULL·CLOSE_WARN(당일 청산 종목에만 온다). 나머지 신호는 무시한다.
"""

import logging
import math
from datetime import datetime, timedelta, timezone

from .. import accounts, db
from ..config import (AUTOTRADE_BUY_BUFFER_PCT, AUTOTRADE_BUY_TTL_MIN, AUTOTRADE_HARD_MAX_AMOUNT_KRW,
                      AUTOTRADE_HARD_MAX_AMOUNT_USD, VIRTUAL_AMOUNT)
from ..models import Signal, price_text
from ..timeutil import now_local
from .broker import BrokerError, OrderState, round_price
from .models import OPEN_STATUSES, OrderIntent
from .policy import RiskPolicy

log = logging.getLogger("scalper")

BUY_KINDS = ("ENTRY",)
SELL_KINDS = ("STOP", "SELL", "EXIT_FULL", "CLOSE_WARN")
MAX_CONSECUTIVE_FAILURES = 3


class Executor:
    def __init__(self, store, broker, mode: str, notify, hours=None, account: dict = None):
        if (mode == "live") != (account is not None):
            raise ValueError("live 실행기는 계정이 있어야 하고, 가상(dry) 실행기는 계정이 없어야 한다")
        self.store = store
        self.broker = broker
        self.mode = mode                    # dry(공용 가상 장부) | live(계정)
        self.notify = notify                # Dispatcher — 가상은 공용 채널, live 는 그 계정 채널
        self.hours = hours                  # MarketHours (엔진이 넣어 준다)
        self.account = account              # accounts.live_accounts 항목 (id·email·amount_scale). None = 가상 장부
        self.account_id = account["id"] if account else None
        self.virtual = account is None
        self.policy = RiskPolicy({"KRW": AUTOTRADE_HARD_MAX_AMOUNT_KRW, "USD": AUTOTRADE_HARD_MAX_AMOUNT_USD})
        self.failures = 0
        self.tag = "[DRY] " if self.virtual else ""
        log.info("자동매매 실행기 준비: %s, 브로커 %s", "공용 가상 장부" if self.virtual else f"계정 {account['email']} live",
                 getattr(broker, "name", type(broker).__name__))

    def dry_holdings(self) -> dict:
        """공용 가상 장부의 모의 보유 (가상 체결 누적). 엔진이 보는 유일한 보유다. live 실행기면 빈 dict."""
        return db.dry_positions(self.store) if self.virtual else {}

    # -- 신호 → 의도 ------------------------------------------------------------
    def on_signal(self, signal: Signal, snap: dict, holdings: dict = None):
        """holdings 는 엔진이 넘기는 가상 보유다 (가상 실행기만 쓴다). live 실행기는 자기 계좌 보유를 직접 읽는다."""
        if signal.kind not in BUY_KINDS + SELL_KINDS or not snap:
            return None
        if not self.virtual:
            if not snap["cfg"].get("auto_trade") or not self._live_on():
                return None                         # 이 계정이 주문하지 않을 신호 — 계좌 조회(토스 API)도 하지 않는다
            try:
                holdings = self.broker.holdings()
            except (Exception, SystemExit) as e:    # 토스는 인증 실패(허용 IP·키)를 SystemExit 로 올린다
                log.warning("계정 %s 보유 조회 오류: %s", self.account["email"], e)
                holdings = None
            if holdings is None:
                log.warning("계정 %s 보유 조회 실패 — %s %s 주문 보류", self.account["email"], signal.kind, signal.symbol)
                return None
        holdings = holdings or {}
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
            log.info("%s자동매매 보류 %s %s %s: %s", self._who(), symbol, intent.side, intent.kind, reason)
            return intent
        if intent.side == "SELL":
            self._cancel_open_buys(symbol)        # 반대 방향 미결이 있으면 매도가 거절된다
        db.insert_order(self.store, intent.to_row())
        return self._place(intent)

    def _buy_intent(self, symbol, market, cfg, snap):
        ref = float(snap["price"])
        limit = round_price(ref * (1 + AUTOTRADE_BUY_BUFFER_PCT / 100), market, "BUY")
        amount = float(cfg.get("auto_amount") or 0)
        if self.virtual:
            amount = amount or VIRTUAL_AMOUNT["KRW" if market == "KR" else "USD"]
        else:
            amount *= float(self.account.get("amount_scale") or 1)      # 계정별 금액 배율
        qty = math.floor(amount / limit) if limit > 0 else 0
        if self.virtual and limit > 0:
            qty = max(qty, 1)                     # 한 주가 금액보다 비싸도 가상 기록은 남긴다
        # 반복 알림은 원래 신호봉(signal_bar)을 넘긴다 — 같은 진입 기회에 두 번 사지 않는다 (_duplicate_buy)
        return OrderIntent.create(self.mode, symbol, market, "BUY", "ENTRY", "LIMIT", limit, qty,
                                  bar_key=snap.get("signal_bar") or snap.get("bar_key"), account_id=self.account_id)

    def _sell_intent(self, symbol, market, kind, snap, held):
        qty = float(held.get("qty", 0) or 0)
        if not self.virtual and qty > 0:
            try:
                qty = min(qty, self.broker.sellable_quantity(symbol))
            except BrokerError as e:
                log.warning("%s 매도가능수량 조회 실패 — 보유 수량으로 진행: %s", symbol, e)
        if market == "KR":
            qty = math.floor(qty)
        return OrderIntent.create(self.mode, symbol, market, "SELL", kind, "MARKET", float(snap["price"]), qty,
                                  bar_key=snap.get("bar_key"), ref_avg=float(held.get("avg") or 0) or None,
                                  account_id=self.account_id)

    def _duplicate_buy(self, intent) -> bool:
        """같은 신호봉으로 이미 의도를 만들었으면(거절 포함) 다시 만들지 않는다. 진입대기 반복 알림 대응."""
        rows = db.orders_since(self.store, self._day_start(), self.mode, self.account_id)
        return any(o["symbol"] == intent.symbol and o["side"] == "BUY" and o["bar_key"] == intent.bar_key for o in rows)

    def _context(self, settings, cfg, market, holdings, intent) -> dict:
        today = db.orders_since(self.store, self._day_start(), self.mode, self.account_id)
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
        return {"virtual": self.virtual, "live_on": self._live_on(), "settings": settings, "cfg": cfg, "regular": regular,
                "holdings": holdings, "open_intents": db.open_orders(self.store, account_id=self.account_id),
                "orders_today": today, "realized_pnl_today": pnl,
                "buying_power": None if self.virtual else self.broker.buying_power,
                "last_price": intent.price if intent.side == "SELL" else None}

    def _live_on(self) -> bool:
        """계정 live 스위치 — 매 신호 DB 에서 다시 읽는다. 백오피스에서 끄면 워커가 실행기를 치우기 전에도 곧바로 막힌다."""
        if self.virtual:
            return False
        acc = accounts.get(self.store, self.account_id)
        return bool(acc and acc["active"] and acc["toss_live"])

    # -- 브로커 ---------------------------------------------------------------
    def _place(self, intent):
        try:
            order_id = self.broker.place(intent)
        except BrokerError as e:
            self.failures += 1
            intent.status, intent.reason = "failed", str(e)[:250]
            db.update_order(self.store, intent.intent_id, status="failed", reason=intent.reason, price=intent.price)
            self._emit("ORDER_FAILED", "🚫 주문 실패", intent,
                       f"{intent.side} {intent.quantity:g}주 @ {price_text(intent.price, intent.market)}\n{e}")
            if self.failures >= MAX_CONSECUTIVE_FAILURES or e.code in ("prerequisite-required", "no-account"):
                self._disable(f"주문 실패 {self.failures}회 연속 ({e.code})")
            return intent
        self.failures = 0
        intent.status, intent.order_id = "sent", order_id
        db.update_order(self.store, intent.intent_id, status="sent", order_id=order_id, price=intent.price)
        if not self.virtual:
            # 가상 장부(공용 채널)는 접수 즉시 체결돼 곧 나갈 체결 알림과 같은 내용이라 보내지 않는다 — 주문번호도 공용 채널에 싣지 않는다
            self._emit("ORDER_SENT", "📤 주문 접수", intent,
                       f"{intent.side} {intent.order_type} {intent.quantity:g}주 @ {price_text(intent.price, intent.market)} "
                       f"(약 {intent.amount:,.0f} {intent.currency})\n주문번호 {order_id}")
        return intent

    def _cancel_open_buys(self, symbol):
        for o in db.open_orders(self.store, symbol, account_id=self.account_id):
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
        """이 장부의 열린 의도를 브로커 상태와 맞춘다. 체결된 매도를 [(symbol, qty, avg_price)] 로 돌려준다."""
        fills = []
        for o in db.open_orders(self.store, account_id=self.account_id):
            intent = OrderIntent.from_row(o)
            try:
                st = self.broker.get(intent.order_id)
            except KeyError:
                if not self.virtual:
                    log.warning("주문 상태 조회 실패 %s", intent.order_id)
                    continue
                st = OrderState(intent.order_id, "filled", intent.quantity, intent.price, "FILLED")
            except BrokerError as e:
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
                # 가상 매도 체결은 다음 사이클 엔진의 청산 완료 알림(수량·평단·청산가·손익)이 같은 내용을 싣는다
                if not (self.virtual and intent.side == "SELL"):
                    self._emit("ORDER_FILLED", "✅ 체결", intent,
                               f"{intent.side} {fields['filled_qty']:g}주 @ {price_text(st.avg_price or intent.price, intent.market)}{pnl_txt}")
                if intent.side == "SELL":
                    fills.append((intent.symbol, fields["filled_qty"], st.avg_price or intent.price))
            else:
                self._emit("ORDER_FAILED", "🚫 주문 종료", intent, f"{intent.side} 상태 {st.raw_status}")
        return fills

    def daily_report(self, market: str) -> str:
        """live 계정의 오늘(거래소 현지 날짜) 체결 매도 성적 — 장 마감에 그 계정 텔레그램으로 간다. 없으면 빈 문자열.
        가상 장부의 성적표는 엔진의 trade_log(청산 완료 기록)가 만든다."""
        if self.virtual:
            return ""
        start = now_local(market).replace(hour=0, minute=0, second=0, microsecond=0)
        rows = [o for o in db.orders_since(self.store, start.astimezone(timezone.utc).isoformat(timespec="seconds"),
                                           self.mode, self.account_id)
                if o["market"] == market and o["side"] == "SELL" and o["status"] in ("filled", "partial") and o.get("pnl") is not None]
        if not rows:
            return ""
        ccy = "KRW" if market == "KR" else "USD"
        wins = sum(1 for o in rows if o["pnl"] > 0)
        total = sum(o["pnl"] for o in rows)
        fmt = (lambda v: f"{v:+,.0f}") if ccy == "KRW" else (lambda v: f"{v:+,.2f}")
        lines = [f"청산 {len(rows)}건: {wins}익절 {len(rows) - wins}손절·본전 (승률 {round(wins / len(rows) * 100)}%)",
                 f"실현손익 {fmt(total)} {ccy}"]
        lines += [f"· {o['symbol']} {o['filled_qty']:g}주 {price_text(o['ref_avg'], market)} → {price_text(o['avg_price'], market)} "
                  f"{fmt(o['pnl'])} ({o['kind']})" for o in rows]
        return "\n".join(lines + ["", "※ 이 계정의 실제 체결 기준"])

    # -- 공통 -----------------------------------------------------------------
    def _disable(self, why: str):
        """live 계정만 — 그 계정의 토스 live 스위치를 끈다. 가상 장부(DryRunBroker)는 주문이 실패하지 않는다."""
        if self.virtual:
            log.warning("가상 장부 주문 실패: %s", why)
            return
        accounts.update_live(self.store, self.account_id, toss_live=False)
        log.warning("계정 %s 자동매매 자동 차단: %s", self.account["email"], why)
        self.notify.send(Signal("AUTOTRADE_DISABLED", "⛔ 자동매매 차단", "시스템",
                                f"{why}\n백오피스 자동매매 화면에서 원인을 확인하고 내 live 스위치를 다시 켠다",
                                account_id=self.account_id))

    def _emit(self, kind, title, intent, body):
        self.notify.send(Signal(kind, title, intent.symbol, f"{self.tag}{body}", intent.symbol, account_id=self.account_id))

    def _who(self) -> str:
        return self.tag if self.virtual else f"[{self.account['email']}] "

    @staticmethod
    def _day_start() -> str:
        """오늘 00:00 KST 를 UTC ISO 로. 하루 집계 기준."""
        kst = now_local("KR").replace(hour=0, minute=0, second=0, microsecond=0)
        return kst.astimezone(timezone.utc).isoformat(timespec="seconds")
