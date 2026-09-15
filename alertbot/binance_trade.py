"""Binance 무기한 선물 자동매매 — dry 모드(가상 체결). 2026-09-15 레버리지 분석 「청산선 밖의 배율」의 설계를 따른다.

run_binance.py 가 ALERT_BINANCE_TRADE_MODE=dry 일 때 DryTrader 하나를 만들어 워커들에 넘긴다. 워커는 진입 후보(action)
알림을 보낸 직후 trader.on_entry(...) 를 부르고, 메인 루프는 사이클마다 trader.poll() 로 열린 포지션의 손절·보유 한도·펀딩을 본다.
live 는 아직 없다. dry 는 공개 시세(마지막 체결가·마크 가격·펀딩)만 쓰므로 API 키가 필요 없다.

규칙
  크기   명목가 = 전략 배분 자본 × 전략별 유효 배율(config.BINANCE_TRADE_LEVERAGE). 펀딩 게이트(롱 > 3bp, 숏 < -3bp)면 절반.
  체결   진입·종료 모두 마지막 체결가 ± 슬리피지 가정, 수수료 테이커 0.05% 편도.
  손절   알림의 손절 참고선. 마크 가격이 닿으면 종료 — 실제 STOP_MARKET(MARK_PRICE) 주문과 같은 조건.
  종료   보유 한도(5시간 / 7일 / 20일)에 닿으면 마지막 체결가로.
  펀딩   정산 시각을 지날 때마다 정산된 펀딩비 × 명목가 (롱은 양의 펀딩을 지급).
  한도   전략당 열린 포지션 하나 · 합산 명목 ≤ 자본 합 × 3 · 오늘(KST) 실현손실이 자본 합의 6% 를 넘으면 신규 진입 중단.
상태는 MySQL alert_binance_positions 에만 있어 재시작해도 이어진다.
"""

import logging
from datetime import datetime, timedelta, timezone

import requests

from . import db
from .binance_crash import KST, fmt_price
from .config import (BINANCE_FAPI, BINANCE_TRADE_CAPITAL, BINANCE_TRADE_DAILY_LOSS_PCT, BINANCE_TRADE_FEE,
                     BINANCE_TRADE_LEVERAGE, BINANCE_TRADE_MAX_TOTAL_LEV, BINANCE_TRADE_SLIP, SURGE_FUNDING_WARN)
from .models import Signal

log = logging.getLogger("binance")


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


class DryTrader:
    """가상 체결 자동매매. 포지션은 DB 에만 있고, 매 사이클 DB 에서 다시 읽는다."""

    mode = "dry"

    def __init__(self, store, notifier, fetch_price=fetch_price, fetch_premium=fetch_premium,
                 fetch_settled_funding=fetch_settled_funding):
        self.store, self.notify = store, notifier
        self.fetch_price, self.fetch_premium, self.fetch_settled = fetch_price, fetch_premium, fetch_settled_funding
        self.capital_total = BINANCE_TRADE_CAPITAL * len(BINANCE_TRADE_LEVERAGE)
        log.info("Binance 자동매매 dry: 전략별 자본 %.0f USDT · 유효 배율 %s · 합산 명목 한도 %.0f · 일손실 한도 %.0f",
                 BINANCE_TRADE_CAPITAL, BINANCE_TRADE_LEVERAGE, self.capital_total * BINANCE_TRADE_MAX_TOTAL_LEV,
                 self.capital_total * BINANCE_TRADE_DAILY_LOSS_PCT / 100)

    # -- 진입 -------------------------------------------------------------------
    def on_entry(self, strategy: str, symbol: str, side: str, result: dict, hold_hours: float, now: datetime = None):
        """진입 후보 알림 하나 → 가상 포지션 행. 막히면 BN_SKIP 알림을 보내고 None."""
        now = now or datetime.now(timezone.utc)
        lev = BINANCE_TRADE_LEVERAGE[strategy]
        notional, note = BINANCE_TRADE_CAPITAL * lev, ""
        fund = result.get("funding")
        if fund is not None and (fund > SURGE_FUNDING_WARN if side == "long" else fund < -SURGE_FUNDING_WARN):
            notional, note = notional / 2, " · 펀딩 게이트로 크기 절반"
        why = self._blocked(strategy, symbol, notional, now)
        if why is None:
            try:
                last, prem = self.fetch_price(symbol), self.fetch_premium(symbol)
            except Exception as e:
                why = f"시세 조회 실패 {e}"
        if why:
            self._emit("BN_SKIP", "⏸ 진입 보류 (가상)", symbol, f"{strategy} {symbol} {side.upper()}: {why}")
            return None
        sgn = 1 if side == "long" else -1
        price = last * (1 + sgn * BINANCE_TRADE_SLIP.get(symbol, 0.0005))
        qty = round(notional / price, 6)
        deadline = now + timedelta(hours=hold_hours)
        row = dict(mode=self.mode, strategy=strategy, symbol=symbol, side=side, qty=qty, entry_price=price,
                   notional=round(qty * price, 4), leverage=lev, stop=float(result["stop"]),
                   deadline=deadline.isoformat(timespec="seconds"), signal_bar=result.get("open_time"),
                   next_funding=prem["next_funding"], funding=0.0, status="open",
                   opened_at=now.isoformat(timespec="seconds"))
        row["id"] = db.insert_binance_position(self.store, row)
        self._emit("BN_ENTRY", "📥 진입 (가상)", symbol,
                   f"{strategy} {side.upper()} {qty:g} @ {fmt_price(price)} · 명목 {row['notional']:,.0f} USDT "
                   f"(자본 {BINANCE_TRADE_CAPITAL:,.0f} × {lev:g}배){note}\n"
                   f"손절 {fmt_price(row['stop'])} (마크 기준) · 보유 한도 {deadline.astimezone(KST):%m-%d %H:%M} KST 까지")
        return row

    def _blocked(self, strategy, symbol, notional, now):
        opened = db.binance_positions(self.store, status="open")
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

    # -- 감시 -------------------------------------------------------------------
    def poll(self, now: datetime = None) -> list:
        """열린 포지션마다 펀딩 정산 → 마크 손절 → 보유 한도. 종료한 포지션 행 목록을 돌려준다."""
        now = now or datetime.now(timezone.utc)
        closed = []
        for p in db.binance_positions(self.store, status="open"):
            try:
                prem = self.fetch_premium(p["symbol"])
                if prem["next_funding"] > (p["next_funding"] or 0):          # 정산 시각을 지났다
                    p = self._settle_funding(p, prem["next_funding"])
            except Exception as e:                                              # 다음 사이클에 다시 본다
                log.warning("%s 마크·펀딩 조회 실패: %s", p["symbol"], e)
                continue
            hit = prem["mark"] <= p["stop"] if p["side"] == "long" else prem["mark"] >= p["stop"]
            if hit or now >= datetime.fromisoformat(p["deadline"]):
                row = self._close(p, "stop" if hit else "time", now)
                if row:
                    closed.append(row)
        return closed

    def _settle_funding(self, p, next_funding):
        _, rate = self.fetch_settled(p["symbol"])
        cost = (rate if p["side"] == "long" else -rate) * p["notional"]        # 롱은 양의 펀딩을 지급
        p["funding"] = round(p["funding"] + cost, 6)
        db.update_binance_position(self.store, p["id"], funding=p["funding"], next_funding=next_funding)
        return p

    def _close(self, p, reason, now):
        try:
            last = self.fetch_price(p["symbol"])
        except Exception as e:
            log.warning("%s 종료 시세 조회 실패: %s", p["symbol"], e)
            return None
        sgn = 1 if p["side"] == "long" else -1
        price = last * (1 - sgn * BINANCE_TRADE_SLIP.get(p["symbol"], 0.0005))
        gross = sgn * (price - p["entry_price"]) * p["qty"]
        fees = (p["entry_price"] + price) * p["qty"] * BINANCE_TRADE_FEE
        pnl = round(gross - fees - p["funding"], 4)
        db.update_binance_position(self.store, p["id"], status="closed", exit_price=price, exit_reason=reason, pnl=pnl,
                                   closed_at=now.isoformat(timespec="seconds"))
        p.update(status="closed", exit_price=price, exit_reason=reason, pnl=pnl)
        self._emit("BN_EXIT", "📤 종료 (가상)", p["symbol"],
                   f"{p['strategy']} {p['side'].upper()} {p['qty']:g} @ {fmt_price(price)} · "
                   f"{'손절 (마크 도달)' if reason == 'stop' else '보유 한도'}\n"
                   f"손익 {pnl:+,.2f} USDT (명목 대비 {pnl / p['notional'] * 100:+.2f}% · 자본 대비 "
                   f"{pnl / BINANCE_TRADE_CAPITAL * 100:+.2f}%) · 수수료 {fees:.2f} · 펀딩 {p['funding']:+.2f}")
        return p

    def _emit(self, kind, title, symbol, body):
        self.notify.send(Signal(kind, title, symbol, "[DRY] " + body, symbol))

    @staticmethod
    def _day_start(now: datetime) -> str:
        """오늘 00:00 KST 를 UTC ISO 로 — 일손실 집계 기준."""
        kst = now.astimezone(KST).replace(hour=0, minute=0, second=0, microsecond=0)
        return kst.astimezone(timezone.utc).isoformat(timespec="seconds")
