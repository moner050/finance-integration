"""Binance 무기한 선물 자동매매 — dry(가상 체결) / live(실제 주문). 2026-09-15 레버리지 분석 「청산선 밖의 배율」의 설계를 따른다.

run_binance.py 가 ALERT_BINANCE_TRADE_MODE 가 off 가 아니면 Trader 하나를 만들어 워커들에 넘긴다. 워커는 진입 후보(action)
알림을 보낸 직후 trader.on_entry(...) 를 부르고, 메인 루프는 사이클마다 trader.poll() 로 열린 포지션의 손절·보유 한도·펀딩을 본다.

공통 규칙
  크기   명목가 = 전략 배분 자본 × 전략별 유효 배율(config.BINANCE_TRADE_LEVERAGE). 펀딩 게이트(롱 > 3bp, 숏 < -3bp)면 절반.
  손절   알림의 손절 참고선, 마크 가격 기준. 종료는 보유 한도(5시간 / 7일 / 20일). 목표 지정가는 없다.
  한도   전략당 열린 포지션 하나 · 합산 명목 ≤ 자본 합 × 3 · 오늘(KST) 실현손실이 자본 합의 6% 를 넘으면 신규 진입 중단.
dry    공개 시세만 쓴다(키 불필요). 체결 = 마지막 체결가 ± 슬리피지, 마크가 손절에 닿으면 종료.
live   binance_broker.BinanceFutures 로 시장가 진입 → 즉시 STOP_MARKET 알고 주문(마크 트리거, closePosition) 손절.
       DB 킬 스위치(binance_trade_enabled)가 1 이어야 진입한다. 진입 주문이 3회 연속 실패하면 킬 스위치를 끈다.
       매 사이클 손절 주문과 포지션을 조회해 손절 체결·거래소에서의 수동 종료를 반영하고, 보유 한도가 되면 손절을 취소하고
       시장가로 닫는다. 손절 주문이 안 걸린 포지션은 사이클마다 다시 건다. 손익·수수료·펀딩은 dry 와 같은 식의 추정값이다.
상태는 MySQL alert_binance_positions 에만 있어 재시작해도 이어진다. 수수료는 테이커 0.05% 편도로 잡는다.
"""

import logging
from datetime import datetime, timedelta, timezone

import requests

from . import db
from .binance_broker import BrokerError
from .binance_crash import KST, fmt_price
from .config import (BINANCE_FAPI, BINANCE_TRADE_CAPITAL, BINANCE_TRADE_DAILY_LOSS_PCT, BINANCE_TRADE_FEE,
                     BINANCE_TRADE_LEVERAGE, BINANCE_TRADE_MAX_TOTAL_LEV, BINANCE_TRADE_SLIP, SURGE_FUNDING_WARN)
from .models import Signal

log = logging.getLogger("binance")

MAX_FAILURES = 3
REASON = {"stop": "손절 (마크 도달)", "time": "보유 한도", "manual": "거래소에서 직접 종료됨"}


def fetch_price(symbol: str, session=None) -> float:
    r = (session or requests).get(f"{BINANCE_FAPI}/fapi/v1/ticker/price", params={"symbol": symbol}, timeout=10)
    r.raise_for_status()
    return float(r.json()["price"])


def fetch_premium(symbol: str, session=None) -> dict:
    """마크 가격과 다음 펀딩 정산 시각(ms)."""
    r = (session or requests).get(f"{BINANCE_FAPI}/fapi/v1/premiumIndex", params={"symbol": symbol}, timeout=10)
    r.raise_for_status()
    j = r.json()
    return {"mark": float(j["markPrice"]), "next_funding": int(j["nextFundingTime"])}


def fetch_settled_funding(symbol: str, session=None) -> tuple:
    """마지막으로 정산된 펀딩 (정산 시각 ms, 비율)."""
    r = (session or requests).get(f"{BINANCE_FAPI}/fapi/v1/fundingRate", params={"symbol": symbol, "limit": 1}, timeout=10)
    r.raise_for_status()
    j = r.json()[-1]
    return int(j["fundingTime"]), float(j["fundingRate"])


class Trader:
    """dry/live 공용. 포지션은 DB 에만 있고 매 사이클 DB 에서 다시 읽는다. live 는 broker 가 필요하다."""

    def __init__(self, store, notifier, mode: str = "dry", broker=None, fetch_price=fetch_price,
                 fetch_premium=fetch_premium, fetch_settled_funding=fetch_settled_funding):
        self.store, self.notify, self.mode, self.broker = store, notifier, mode, broker
        self.fetch_price, self.fetch_premium, self.fetch_settled = fetch_price, fetch_premium, fetch_settled_funding
        self.capital_total = BINANCE_TRADE_CAPITAL * len(BINANCE_TRADE_LEVERAGE)
        self.failures = 0
        self.tag = "[DRY] " if mode == "dry" else "[LIVE] "
        log.info("Binance 자동매매 %s: 전략별 자본 %.0f USDT · 유효 배율 %s · 합산 명목 한도 %.0f · 일손실 한도 %.0f",
                 mode, BINANCE_TRADE_CAPITAL, BINANCE_TRADE_LEVERAGE, self.capital_total * BINANCE_TRADE_MAX_TOTAL_LEV,
                 self.capital_total * BINANCE_TRADE_DAILY_LOSS_PCT / 100)

    # -- 진입 -------------------------------------------------------------------
    def on_entry(self, strategy: str, symbol: str, side: str, result: dict, hold_hours: float, now: datetime = None):
        """진입 후보 알림 하나 → 포지션 행. 막히거나 실패하면 알림을 보내고 None."""
        now = now or datetime.now(timezone.utc)
        lev = BINANCE_TRADE_LEVERAGE[strategy]
        notional, note = BINANCE_TRADE_CAPITAL * lev, ""
        fund = result.get("funding")
        if fund is not None and (fund > SURGE_FUNDING_WARN if side == "long" else fund < -SURGE_FUNDING_WARN):
            notional, note = notional / 2, " · 펀딩 게이트로 크기 절반"
        why = self._blocked(strategy, symbol, notional, now)
        if why is None and self.mode == "live" and db.get_settings(self.store)["binance_trade_enabled"] != "1":
            why = "킬 스위치 OFF (백오피스 자동매매 화면에서 켠다)"
        if why is None:
            try:
                last, prem = self.fetch_price(symbol), self.fetch_premium(symbol)
            except Exception as e:
                why = f"시세 조회 실패 {e}"
        if why:
            self._emit("BN_SKIP", "⏸ 진입 보류", symbol, f"{strategy} {symbol} {side.upper()}: {why}")
            return None
        sgn = 1 if side == "long" else -1
        ids = {"entry_order_id": None, "stop_order_id": None}
        if self.mode == "dry":
            price = last * (1 + sgn * BINANCE_TRADE_SLIP.get(symbol, 0.0005))
            qty, stop = round(notional / price, 6), float(result["stop"])
        else:
            try:
                price, qty, stop, ids = self._live_open(symbol, side, notional, last, float(result["stop"]))
            except BrokerError as e:
                self._entry_failed(f"{strategy} {symbol} {side.upper()} 진입 주문 실패: {e}", symbol)
                return None
        deadline = now + timedelta(hours=hold_hours)
        row = dict(mode=self.mode, strategy=strategy, symbol=symbol, side=side, qty=qty, entry_price=price,
                   notional=round(qty * price, 4), leverage=lev, stop=stop, deadline=deadline.isoformat(timespec="seconds"),
                   signal_bar=result.get("open_time"), next_funding=prem["next_funding"], funding=0.0, status="open",
                   opened_at=now.isoformat(timespec="seconds"), **ids)
        row["id"] = db.insert_binance_position(self.store, row)
        self._emit("BN_ENTRY", "📥 진입", symbol,
                   f"{strategy} {side.upper()} {qty:g} @ {fmt_price(price)} · 명목 {row['notional']:,.0f} USDT "
                   f"(자본 {BINANCE_TRADE_CAPITAL:,.0f} × {lev:g}배){note}\n"
                   f"손절 {fmt_price(stop)} (마크 기준{'' if ids['stop_order_id'] or self.mode == 'dry' else ' — 주문 실패, 재시도 중'}) · "
                   f"보유 한도 {deadline.astimezone(KST):%m-%d %H:%M} KST 까지")
        return row

    def _live_open(self, symbol, side, notional, last, stop):
        """시장가 진입 → 손절 알고 주문. 손절 주문만 실패하면 포지션은 두고 알린다 (poll 이 사이클마다 다시 건다)."""
        b = self.broker
        qty = b.round_qty(symbol, notional / last)
        if qty <= 0 or qty * last < b.min_notional(symbol):
            raise BrokerError("min-notional", f"수량 {qty} (명목 {qty * last:,.0f} USDT)가 최소 주문 미만")
        oid, price, filled = b.market_open(symbol, side, qty)
        self.failures = 0
        stop_px = b.round_price(symbol, stop)
        ids = {"entry_order_id": oid, "stop_order_id": None}
        try:
            ids["stop_order_id"] = b.place_stop(symbol, side, stop_px)
        except BrokerError as e:
            self._emit("BN_FAIL", "⛔ 손절 주문 실패", symbol,
                       f"{symbol} {side.upper()} 진입은 체결됐지만 손절 주문이 거부됐다 — 사이클마다 다시 건다\n{e}")
        return price, filled, stop_px, ids

    def _entry_failed(self, why, symbol):
        self.failures += 1
        self._emit("BN_FAIL", "⛔ 주문 실패", symbol, why)
        if self.failures >= MAX_FAILURES:
            db.set_setting(self.store, "binance_trade_enabled", 0)
            self._emit("BN_FAIL", "⛔ 자동매매 차단", "시스템", f"진입 주문 {self.failures}회 연속 실패 — 킬 스위치를 껐다. 원인을 확인하고 백오피스에서 다시 켠다")

    def _blocked(self, strategy, symbol, notional, now):
        opened = [p for p in db.binance_positions(self.store, status="open") if p["mode"] == self.mode]
        if any(p["strategy"] == strategy and p["symbol"] == symbol for p in opened):
            return "같은 전략의 포지션이 열려 있다"
        total, cap = sum(p["notional"] for p in opened) + notional, self.capital_total * BINANCE_TRADE_MAX_TOTAL_LEV
        if total > cap:
            return f"합산 명목 {total:,.0f} > 한도 {cap:,.0f} USDT"
        loss = db.binance_pnl_since(self.store, self._day_start(now), self.mode)
        limit = self.capital_total * BINANCE_TRADE_DAILY_LOSS_PCT / 100
        if loss <= -limit:
            return f"오늘 실현손익 {loss:+,.0f} USDT 가 한도 -{limit:,.0f} 아래"
        return None

    def open_lines(self) -> list:
        """시황 요약용 — 이 모드의 열린 포지션마다 한 줄 (마크 가격 대비 손익, 보유 한도)."""
        out = []
        for p in db.binance_positions(self.store, status="open"):
            if p["mode"] != self.mode:
                continue
            sgn = 1 if p["side"] == "long" else -1
            try:
                mark = self.fetch_premium(p["symbol"])["mark"]
                pnl = f"마크 {fmt_price(mark)} ({sgn * (mark / p['entry_price'] - 1) * 100:+.2f}%)"
            except Exception as e:                                      # 시세 실패면 손익 없이 표기
                pnl = f"마크 조회 실패 ({e})"
            deadline = datetime.fromisoformat(p["deadline"]).astimezone(KST)
            out.append(f"📥 {self.tag.strip()} {p['symbol']} {p['strategy']} {'롱' if p['side'] == 'long' else '숏'} "
                       f"{p['qty']:g} @ {fmt_price(p['entry_price'])} · {pnl} · 손절 {fmt_price(p['stop'])} · "
                       f"한도 {deadline:%m-%d %H:%M} KST")
        return out

    # -- 감시 -------------------------------------------------------------------
    def poll(self, now: datetime = None) -> list:
        """열린 포지션마다 펀딩 정산 → (dry) 마크 손절·보유 한도 / (live) 손절 체결·수동 종료·보유 한도. 종료 행 목록."""
        now = now or datetime.now(timezone.utc)
        closed = []
        for p in db.binance_positions(self.store, status="open"):
            if p["mode"] != self.mode:                                          # 모드를 바꾼 뒤 남은 다른 모드 행은 건드리지 않는다
                continue
            try:
                prem = self.fetch_premium(p["symbol"])
                if prem["next_funding"] > (p["next_funding"] or 0):            # 정산 시각을 지났다
                    p = self._settle_funding(p, prem["next_funding"])
                row = self._check_live(p, prem, now) if self.mode == "live" else self._check_dry(p, prem, now)
            except (BrokerError, Exception) as e:                               # 다음 사이클에 다시 본다
                log.warning("%s 포지션 감시 실패: %s", p["symbol"], e)
                continue
            if row:
                closed.append(row)
        return closed

    def _check_dry(self, p, prem, now):
        hit = prem["mark"] <= p["stop"] if p["side"] == "long" else prem["mark"] >= p["stop"]
        if not hit and now < datetime.fromisoformat(p["deadline"]):
            return None
        sgn = 1 if p["side"] == "long" else -1
        price = self.fetch_price(p["symbol"]) * (1 - sgn * BINANCE_TRADE_SLIP.get(p["symbol"], 0.0005))
        return self._record_close(p, price, "stop" if hit else "time", now)

    def _check_live(self, p, prem, now):
        b, symbol, side = self.broker, p["symbol"], p["side"]
        if p["stop_order_id"]:
            st = b.stop_status(p["stop_order_id"])
            if st["triggered"]:
                return self._record_close(p, st["price"] or prem["mark"], "stop", now)
            if not st["active"]:                                                # 취소·만료됐다 — 다시 건다
                p["stop_order_id"] = None
        if b.position_qty(symbol, side) <= 0:
            return self._record_close(p, prem["mark"], "manual", now)
        if not p["stop_order_id"]:
            p["stop_order_id"] = b.place_stop(symbol, side, p["stop"])
            db.update_binance_position(self.store, p["id"], stop_order_id=p["stop_order_id"])
            log.info("%s %s 손절 알고 주문 %s 걸었다 (트리거 %s)", symbol, side, p["stop_order_id"], p["stop"])
        if now < datetime.fromisoformat(p["deadline"]):
            return None
        b.cancel_stop(p["stop_order_id"])
        _, price, _ = b.market_close(symbol, side, b.position_qty(symbol, side))
        return self._record_close(p, price, "time", now)

    def _settle_funding(self, p, next_funding):
        _, rate = self.fetch_settled(p["symbol"])
        cost = (rate if p["side"] == "long" else -rate) * p["notional"]        # 롱은 양의 펀딩을 지급
        p["funding"] = round(p["funding"] + cost, 6)
        db.update_binance_position(self.store, p["id"], funding=p["funding"], next_funding=next_funding)
        return p

    def _record_close(self, p, price, reason, now):
        sgn = 1 if p["side"] == "long" else -1
        gross = sgn * (price - p["entry_price"]) * p["qty"]
        fees = (p["entry_price"] + price) * p["qty"] * BINANCE_TRADE_FEE
        pnl = round(gross - fees - p["funding"], 4)
        db.update_binance_position(self.store, p["id"], status="closed", exit_price=price, exit_reason=reason, pnl=pnl,
                                   closed_at=now.isoformat(timespec="seconds"))
        p.update(status="closed", exit_price=price, exit_reason=reason, pnl=pnl)
        self._emit("BN_EXIT", "📤 종료", p["symbol"],
                   f"{p['strategy']} {p['side'].upper()} {p['qty']:g} @ {fmt_price(price)} · {REASON[reason]}\n"
                   f"손익{' 추정' if self.mode == 'live' else ''} {pnl:+,.2f} USDT (명목 대비 {pnl / p['notional'] * 100:+.2f}% · "
                   f"자본 대비 {pnl / BINANCE_TRADE_CAPITAL * 100:+.2f}%) · 수수료 {fees:.2f} · 펀딩 {p['funding']:+.2f}")
        return p

    def _emit(self, kind, title, symbol, body):
        self.notify.send(Signal(kind, title, symbol, self.tag + body, symbol if symbol != "시스템" else None))

    @staticmethod
    def _day_start(now: datetime) -> str:
        """오늘 00:00 KST 를 UTC ISO 로 — 일손실 집계 기준."""
        kst = now.astimezone(KST).replace(hour=0, minute=0, second=0, microsecond=0)
        return kst.astimezone(timezone.utc).isoformat(timespec="seconds")
