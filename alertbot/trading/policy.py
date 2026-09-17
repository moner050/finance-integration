"""리스크 정책 — 주문 의도가 나가도 되는지 판단한다. 판단 근거는 전부 문자열 사유로 남긴다.

순서가 중요하다: 게이트(계정 live 스위치·종목 자동 여부·정규장) → 포지션 중복 → 금액·횟수·손실 한도 → 잔고 → 가격 정합성.
손절(STOP)은 횟수 한도를 면제하고, 매도 전체는 손실 한도를 면제한다 — 위험 청산을 막으면 안 된다.
공용 가상 장부(ctx virtual)는 확정 신호를 전부 기록하는 게 목적이라 스위치·종목 체크·금액·보유 수·횟수·손실·잔고 한도를 적용하지 않는다.
정규장·수량·미결 중복·보유 여부 같은 구조 검사만 한다.
"""

from .models import OPEN_STATUSES

PRICE_SANITY_PCT = 3.0      # 지정가가 현재가에서 이 % 넘게 벗어나면 신호가 낡은 것이다


class RiskPolicy:
    def __init__(self, hard_max: dict):
        self.hard_max = hard_max        # {"KRW": 2_000_000, "USD": 2_000} — .env 하드캡

    def check(self, intent, ctx: dict):
        """(허용 여부, 사유). ctx 키:
        virtual(공용 가상 장부 여부), live_on(계정 live 스위치), settings, cfg(워치리스트 항목), regular(정규장 여부), holdings,
        open_intents, orders_today, realized_pnl_today({"KRW": x, "USD": y}), buying_power(callable(currency) -> float), last_price
        """
        settings, cfg, virtual = ctx["settings"], ctx["cfg"], bool(ctx.get("virtual"))
        if not virtual:
            if not ctx.get("live_on"):
                return False, "disabled"
            if not cfg.get("auto_trade"):
                return False, "symbol-not-auto"
        if not ctx.get("regular"):
            return False, "outside-regular-hours"
        if intent.quantity < 1:
            return False, "quantity-below-1"
        open_same = [o for o in ctx.get("open_intents", []) if o["symbol"] == intent.symbol
                     and o["status"] in OPEN_STATUSES and o["side"] == intent.side]
        if open_same:
            return False, "open-buy-exists" if intent.side == "BUY" else "open-sell-exists"

        held = ctx.get("holdings", {}).get(intent.symbol, {})
        held_qty = float(held.get("qty", 0) or 0)
        ccy = intent.currency
        if intent.side == "SELL":
            if held_qty <= 0:
                return False, "nothing-to-sell"
            if not virtual and intent.kind != "STOP" and self._orders_today(ctx) >= int(settings["max_orders_per_day"]):
                return False, "max-orders-per-day"
            return True, "ok"

        # ---- BUY ----
        if held_qty > 0:
            return False, "already-holding"
        if virtual:
            return True, "ok"
        cap = min(float(settings[f"max_order_amount_{ccy.lower()}"]), float(self.hard_max[ccy]))
        if intent.amount > cap:
            return False, f"amount-over-limit({cap:g})"
        positions = {s for s, h in ctx.get("holdings", {}).items() if float(h.get("qty", 0) or 0) > 0}
        positions |= {o["symbol"] for o in ctx.get("open_intents", []) if o["side"] == "BUY" and o["status"] in OPEN_STATUSES}
        if len(positions) >= int(settings["max_positions"]):
            return False, "max-positions"
        if self._orders_today(ctx) >= int(settings["max_orders_per_day"]):
            return False, "max-orders-per-day"
        loss = float(ctx.get("realized_pnl_today", {}).get(ccy, 0) or 0)
        if loss <= -float(settings[f"daily_loss_limit_{ccy.lower()}"]):
            return False, f"daily-loss-limit({loss:g})"
        last = ctx.get("last_price")
        if last and abs(intent.price - last) / last * 100 > PRICE_SANITY_PCT:
            return False, "price-sanity"
        bp = ctx.get("buying_power")
        if bp is not None:
            try:
                available = bp(ccy) if callable(bp) else float(bp)
            except Exception as e:           # 조회 실패면 모르는 채로 사지 않는다
                return False, f"buying-power-unknown({e})"
            if available < intent.amount:
                return False, f"insufficient-buying-power({available:g})"
        return True, "ok"

    @staticmethod
    def _orders_today(ctx) -> int:
        return sum(1 for o in ctx.get("orders_today", []) if o["status"] != "rejected")
