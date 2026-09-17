"""Binance 무기한 선물 자동매매 — 공용 가상 장부(dry) / 계정별 live(실제 주문). 2026-09-15 레버리지 분석 「청산선 밖의 배율」의 설계를 따른다.

run_binance.py 가 공용 가상 트레이더를 늘 만들고, ALERT_BINANCE_TRADE_MODE=live 면 Binance live 스위치를 켠 계정마다 live 트레이더를 더해
TraderGroup 으로 워커들에 넘긴다. 워커는 진입 후보(action) 알림을 보낸 직후 trader.on_entry(...) 를 부르고,
메인 루프는 사이클마다 trader.poll() 로 열린 포지션의 손절·보유 한도·펀딩을 본다.

공통 규칙
  크기   명목가 = 전략 배분 자본 × 전략별 유효 배율(config.BINANCE_TRADE_LEVERAGE). 펀딩 게이트(롱 > 3bp, 숏 < -3bp)면 절반.
  손절   알림의 손절 참고선, 마크 가격 기준. 종료는 보유 한도(5시간 / 7일 / 20일 / 급등 소진 숏 48시간).
  목표가 진입 후보가 take_profit 을 주면(급등 소진 숏 SCAN_FADE) 마크가 닿을 때 종료. 나머지 전략은 목표 지정가가 없다.
  한도   전략·심볼당 열린 포지션 하나(SCAN_FADE 는 전략 전체 동시 8개까지) · 합산 명목 ≤ 자본 합 × 3 ·
         오늘(KST) 실현손실이 자본 합의 6% 를 넘으면 신규 진입 중단.
  live 전략 config.BINANCE_LIVE_STRATEGIES 만 계정 live 로 주문한다. SCAN_FADE 는 공용 가상 장부 전용이다.
dry    공개 시세만 쓴다(키 불필요). 체결 = 마지막 체결가 ± 슬리피지, 마크가 손절에 닿으면 종료.
live   binance_broker.BinanceFutures 로 시장가 진입 → 즉시 STOP_MARKET 알고 주문(마크 트리거, closePosition) 손절.
       계정의 Binance live 스위치가 켜져 있어야 진입한다. 진입 주문이 3회 연속 실패하면 그 계정 스위치를 끈다.
       매 사이클 손절 주문과 포지션을 조회해 손절 체결·거래소에서의 수동 종료를 반영하고, 보유 한도가 되면 손절을 취소하고
       시장가로 닫는다. 손절 주문이 안 걸린 포지션은 사이클마다 다시 건다. 손익·수수료·펀딩은 dry 와 같은 식의 추정값이다.
상태는 MySQL alert_binance_positions 에만 있어 재시작해도 이어진다(account_id NULL = 가상 장부). 수수료는 테이커 0.05% 편도로 잡는다.
채널   가상 장부의 포지션 사건은 공용 채널로, 계정 live 의 포지션 사건은 그 계정의 텔레그램으로만 간다 (Signal.account_id).
       자본도 장부마다 다르다 — 가상은 .env ALERT_BINANCE_TRADE_CAPITAL, 계정은 백오피스의 계정별 자본.
"""

import logging
import time
from datetime import datetime, timedelta, timezone

import requests

from . import accounts, db
from .binance_broker import BinanceFutures, BrokerError
from .binance_crash import KST, fmt_price
from .config import (BINANCE_FAPI, BINANCE_LIVE_STRATEGIES, BINANCE_TRADE_CAPITAL, BINANCE_TRADE_DAILY_LOSS_PCT, BINANCE_TRADE_FEE,
                     BINANCE_TRADE_LEVERAGE, BINANCE_TRADE_MAX_OPEN, BINANCE_TRADE_MAX_TOTAL_LEV, BINANCE_TRADE_SLIP,
                     SURGE_FUNDING_WARN)
from .models import Signal
from .notify import account_channels
from .notify.dispatcher import Dispatcher

log = logging.getLogger("binance")

MAX_FAILURES = 3
REASON = {"stop": "손절", "tp": "목표가 도달", "time": "보유 한도", "manual": "거래소에서 직접 종료"}
STRATEGY_NAMES = {"CRASH_BUY": "급락 매수", "SURGE_ENTRY": "4시간 추종", "SURGE_ENTRY_1D": "일봉 추종", "CRASH_SHORT_1D": "일봉 추종",
                  "SCAN_FADE": "급등 소진"}


def strategy_text(strategy: str, side: str) -> str:
    """알림용 전략 이름 — 'SCAN_FADE short' → '급등 소진 숏'."""
    return f"{STRATEGY_NAMES.get(strategy, strategy)} {'롱' if side == 'long' else '숏'}"


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
    """장부 하나의 트레이더. 포지션은 DB 에만 있고 매 사이클 DB 에서 다시 읽는다.
    account 가 None 이면 공용 가상 장부(dry), 있으면 그 계정의 live(broker 필요)다. 자기 장부(mode·account_id)의 행만 본다."""

    def __init__(self, store, notifier, mode: str = "dry", broker=None, fetch_price=fetch_price,
                 fetch_premium=fetch_premium, fetch_settled_funding=fetch_settled_funding, account: dict = None):
        if (mode == "live") != (account is not None):
            raise ValueError("live 트레이더는 계정이 있어야 하고, 가상(dry) 트레이더는 계정이 없어야 한다")
        self.store, self.notify, self.mode, self.broker = store, notifier, mode, broker
        self.account = account          # accounts.live_accounts 항목. None = 공용 가상 장부
        self.account_id = account["id"] if account else None
        self.fetch_price, self.fetch_premium, self.fetch_settled = fetch_price, fetch_premium, fetch_settled_funding
        self.capital = float(account["binance_capital"]) if account else BINANCE_TRADE_CAPITAL
        # 자본 합 = 전략 수 × 배분 자본. live 는 실제로 주문하는 전략만 센다 (가상 전용 전략이 계정 한도를 키우지 않게)
        self.capital_total = self.capital * len(BINANCE_LIVE_STRATEGIES if account else BINANCE_TRADE_LEVERAGE)
        self.failures = 0
        self.tag = "[DRY] " if mode == "dry" else "[LIVE] "
        log.info("Binance 자동매매 %s%s: 전략별 자본 %.0f USDT · 유효 배율 %s · 합산 명목 한도 %.0f · 일손실 한도 %.0f",
                 mode, f" ({account['email']})" if account else "", self.capital, BINANCE_TRADE_LEVERAGE,
                 self.capital_total * BINANCE_TRADE_MAX_TOTAL_LEV, self.capital_total * BINANCE_TRADE_DAILY_LOSS_PCT / 100)

    # -- 진입 -------------------------------------------------------------------
    def on_entry(self, strategy: str, symbol: str, side: str, result: dict, hold_hours: float, now: datetime = None,
                 notify_skip: bool = True):
        """진입 후보 알림 하나 → 포지션 행. 막히거나 실패하면 알림을 보내고 None.
        result: stop(필수)·take_profit(선택)·open_time·funding. notify_skip=False 면 보류를 로그에만 남긴다 — 급변 감시처럼 같은 코인이
        보유 중에 다시 감지되거나 동시 보유 상한이 차는 일이 잦은 전략이 공용 채널을 '진입 보류' 로 채우지 않게."""
        if self.mode == "live" and strategy not in BINANCE_LIVE_STRATEGIES:
            return None                                         # 가상 장부 전용 전략 — 계정 live 는 주문하지 않는다
        now = now or datetime.now(timezone.utc)
        lev = BINANCE_TRADE_LEVERAGE[strategy]
        notional, note = self.capital * lev, ""
        fund = result.get("funding")
        if fund is not None and (fund > SURGE_FUNDING_WARN if side == "long" else fund < -SURGE_FUNDING_WARN):
            notional, note = notional / 2, " · 펀딩으로 크기 절반"
        why = self._blocked(strategy, symbol, notional, now)
        if why is None and self.mode == "live" and not self._live_on():
            why = "live 스위치 OFF (백오피스 자동매매 화면의 내 live 매매에서 켠다)"
        if why is None:
            try:
                last, prem = self.fetch_price(symbol), self.fetch_premium(symbol)
            except Exception as e:
                why = f"시세 조회 실패 {e}"
        if why:
            if notify_skip:
                self._emit("BN_SKIP", "⏸ 진입 보류", symbol, f"{strategy_text(strategy, side)}: {why}")
            else:
                log.info("%s%s %s %s 진입 보류: %s", self.tag, strategy, symbol, side.upper(), why)
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
        tp = float(result["take_profit"]) if result.get("take_profit") is not None else None
        row = dict(mode=self.mode, account_id=self.account_id, strategy=strategy, symbol=symbol, side=side, qty=qty, entry_price=price,
                   notional=round(qty * price, 4), leverage=lev, stop=stop, take_profit=tp, deadline=deadline.isoformat(timespec="seconds"),
                   signal_bar=result.get("open_time"), next_funding=prem["next_funding"], funding=0.0, status="open",
                   opened_at=now.isoformat(timespec="seconds"), **ids)
        row["id"] = db.insert_binance_position(self.store, row)
        self._emit("BN_ENTRY", "📥 진입", symbol,
                   f"{strategy_text(strategy, side)} {fmt_price(price)} · 명목 {row['notional']:,.0f} USDT{note}\n"
                   f"손절 {fmt_price(stop)}{'' if ids['stop_order_id'] or self.mode == 'dry' else ' (주문 실패 — 재시도 중)'}"
                   + (f" · 목표가 {fmt_price(tp)}" if tp is not None else "")
                   + f" · {deadline.astimezone(KST):%m-%d %H:%M} 까지")
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
            accounts.update_live(self.store, self.account_id, binance_live=False)
            self._emit("BN_FAIL", "⛔ 자동매매 차단", "시스템", f"진입 주문 {self.failures}회 연속 실패 — Binance live 스위치를 껐다. 원인을 확인하고 백오피스에서 다시 켠다")

    def _live_on(self) -> bool:
        """계정 Binance live 스위치 — 진입마다 DB 에서 다시 읽는다 (백오피스에서 끄면 곧바로 막힌다)."""
        acc = accounts.get(self.store, self.account_id)
        return bool(acc and acc["active"] and acc["binance_live"])

    def open_rows(self) -> list:
        """이 장부(모드·계정)의 열린 포지션."""
        return [p for p in db.binance_positions(self.store, status="open")
                if p["mode"] == self.mode and p.get("account_id") == self.account_id]

    def _blocked(self, strategy, symbol, notional, now):
        opened = self.open_rows()
        if any(p["strategy"] == strategy and p["symbol"] == symbol for p in opened):
            return "같은 전략의 포지션이 열려 있다"
        max_open = BINANCE_TRADE_MAX_OPEN.get(strategy)
        if max_open is not None and sum(1 for p in opened if p["strategy"] == strategy) >= max_open:
            return f"이 전략의 동시 보유 {max_open}개가 다 찼다"
        total, cap = sum(p["notional"] for p in opened) + notional, self.capital_total * BINANCE_TRADE_MAX_TOTAL_LEV
        if total > cap:
            return f"합산 명목 {total:,.0f} > 한도 {cap:,.0f} USDT"
        loss = db.binance_pnl_since(self.store, self._day_start(now), self.mode, self.account_id)
        limit = self.capital_total * BINANCE_TRADE_DAILY_LOSS_PCT / 100
        if loss <= -limit:
            return f"오늘 실현손익 {loss:+,.0f} USDT 가 한도 -{limit:,.0f} 아래"
        return None

    def open_lines(self) -> list:
        """시황 요약용 — 이 모드의 열린 포지션마다 한 줄 (마크 가격 대비 손익, 보유 한도)."""
        out = []
        for p in self.open_rows():
            sgn = 1 if p["side"] == "long" else -1
            try:                                                        # 주식 시황의 '가상 보유' 줄처럼 손익 부호로 🟢/🔴
                pnl = sgn * (self.fetch_premium(p["symbol"])["mark"] / p["entry_price"] - 1) * 100
                mark, move = ("🟢" if pnl > 0 else "🔴"), f"{pnl:+.2f}%"
            except Exception as e:                                      # 시세 실패면 손익 없이 표기
                mark, move = "⚪", f"마크 조회 실패 ({e})"
            deadline = datetime.fromisoformat(p["deadline"]).astimezone(KST)
            out.append(f"{mark} {p['symbol']} {strategy_text(p['strategy'], p['side'])}  {move} · 손절 {fmt_price(p['stop'])}"
                       + (f" · 목표 {fmt_price(p['take_profit'])}" if p.get("take_profit") is not None else "")
                       + f" · {deadline:%m-%d %H:%M} 까지")
        return out

    # -- 감시 -------------------------------------------------------------------
    def poll(self, now: datetime = None) -> list:
        """열린 포지션마다 펀딩 정산 → (dry) 마크 손절·보유 한도 / (live) 손절 체결·수동 종료·보유 한도. 종료 행 목록."""
        now = now or datetime.now(timezone.utc)
        closed = []
        for p in self.open_rows():                                              # 다른 장부(모드·계정)의 행은 건드리지 않는다
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
        long, mark, tp = p["side"] == "long", prem["mark"], p.get("take_profit")
        hit = mark <= p["stop"] if long else mark >= p["stop"]
        reached = tp is not None and (mark >= tp if long else mark <= tp)
        if not hit and not reached and now < datetime.fromisoformat(p["deadline"]):
            return None
        sgn = 1 if long else -1
        price = self.fetch_price(p["symbol"]) * (1 - sgn * BINANCE_TRADE_SLIP.get(p["symbol"], 0.0005))
        return self._record_close(p, price, "stop" if hit else "tp" if reached else "time", now)

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
        self._emit("BN_EXIT", "📤 종료", p["symbol"],                       # 손익은 수수료·펀딩을 뺀 값
                   f"{strategy_text(p['strategy'], p['side'])} {fmt_price(price)} · {REASON[reason]}\n"
                   f"손익{' 추정' if self.mode == 'live' else ''} {pnl:+,.2f} USDT ({pnl / p['notional'] * 100:+.2f}%)")
        return p

    def daily_report(self, day: str) -> str:
        """live 계정의 하루(KST, YYYY-MM-DD) 종료 포지션 성적 — 자정에 그 계정 텔레그램으로. 없으면 빈 문자열."""
        if self.mode != "live":
            return ""
        rows = [p for p in db.binance_positions(self.store, status="closed", limit=1000, account_ids=[self.account_id])
                if p["mode"] == "live" and p.get("closed_at")
                and datetime.fromisoformat(p["closed_at"]).astimezone(KST).strftime("%Y-%m-%d") == day]
        if not rows:
            return ""
        wins = sum(1 for p in rows if (p["pnl"] or 0) > 0)
        total = sum(p["pnl"] or 0 for p in rows)
        lines = [f"종료 {len(rows)}건: {wins}익절 {len(rows) - wins}손절·본전 · 손익 추정 {total:+,.2f} USDT"]
        lines += [f"· {p['symbol']} {strategy_text(p['strategy'], p['side'])} {fmt_price(p['entry_price'])} → "
                  f"{fmt_price(p['exit_price'])} {p['pnl']:+,.2f} ({REASON.get(p['exit_reason'], p['exit_reason'])})" for p in reversed(rows)]
        return "\n".join(lines + ["", "※ 이 계정의 실제 주문 기준 · 수수료·펀딩 포함 추정"])

    def _emit(self, kind, title, symbol, body):
        self.notify.send(Signal(kind, title, symbol, self.tag + body, symbol if symbol != "시스템" else None,
                                account_id=self.account_id))

    @staticmethod
    def _day_start(now: datetime) -> str:
        """오늘 00:00 KST 를 UTC ISO 로 — 일손실 집계 기준."""
        kst = now.astimezone(KST).replace(hour=0, minute=0, second=0, microsecond=0)
        return kst.astimezone(timezone.utc).isoformat(timespec="seconds")


class TraderGroup:
    """공용 가상 트레이더와 계정별 live 트레이더를 함께 돌린다 — 워커·메인 루프는 하나만 안다. 한쪽 오류가 다른 쪽을 막지 않는다.
    traders 목록은 run_binance 가 계정 변경 때 갈아 끼운다 (워커는 같은 그룹 객체를 계속 쓴다)."""

    def __init__(self, traders: list):
        self.traders = list(traders)

    def on_entry(self, *args, **kw) -> list:
        out = []
        for t in self.traders:
            try:
                out.append(t.on_entry(*args, **kw))
            except Exception as e:
                log.warning("%s 트레이더 진입 처리 실패: %s", t.mode, e)
        return out

    def poll(self, now: datetime = None) -> list:
        closed = []
        for t in self.traders:
            try:
                closed += t.poll(now)
            except Exception as e:
                log.warning("%s 트레이더 감시 오류: %s", t.mode, e)
        return closed


LIVE_RETRY_SEC = 600


def build_live_trader(store, account: dict, symbols: list, leverage: int):
    """계정 하나의 live 트레이더 — 그 계정 키로 서버 시각·심볼 필터·헤지 모드·격리·배율을 맞춘다. 실패하면 그 계정에 알리고 None."""
    notifier = Dispatcher(account_channels(account), record=lambda s, r: db.log_signal(store, s, r))
    broker = BinanceFutures(account["binance"]["api_key"], account["binance"]["api_secret"])
    try:
        broker.sync_time()
        broker.load_filters(symbols)
        broker.setup(symbols, leverage)
        balance = broker.balance()
    except Exception as e:                                  # BrokerError·통신 오류 — 메시지에 키는 없다 (헤더로만 보낸다)
        log.warning("계정 %s Binance live 준비 실패: %s", account["email"], e)
        notifier.send(Signal("BN_FAIL", "⛔ live 준비 실패", "시스템",
                             f"[LIVE] Binance 준비 실패 — {e}\n키 권한(선물 거래)·IP 제한·열린 포지션(헤지 모드 변경 거부)을 확인한다. "
                             f"{LIVE_RETRY_SEC // 60}분 뒤 다시 시도한다", account_id=account["id"]), force=True)
        return None
    notifier.send(Signal("SYSTEM", "⚪ 시스템", "Binance live 준비",
                         f"[LIVE] {', '.join(symbols)} 격리 {leverage}배 헤지 모드 · 가용 {balance:,.0f} USDT · "
                         f"전략별 자본 {account['binance_capital']:,.0f} USDT", account_id=account["id"]))
    return Trader(store, notifier, "live", broker, account=account)


class AccountTraders:
    """계정별 Binance live 트레이더 — 백오피스에서 바꾼 계정·키·스위치를 워커 재시작 없이 반영한다.

    Binance live 스위치가 켜진 활성 계정(키·텔레그램·자본 필요)에 더해, 스위치를 껐거나 계정이 중지돼도 **열린 live 포지션이 남은 계정**은
    트레이더를 유지한다 — 새 진입은 스위치가 막고(_live_on), 손절 재설정·보유 한도 청산은 끝까지 돈다.
    """

    def __init__(self, store, symbols: list, leverage: int, build=build_live_trader, clock=time.monotonic):
        self.store, self.symbols, self.leverage, self.build = store, list(symbols), leverage, build
        self.clock = clock
        self.version = None
        self.by_account = {}            # account_id -> (키 서명, Trader)
        self.failed = {}                # account_id -> 실패 시각

    @property
    def traders(self) -> list:
        return [t for _, t in self.by_account.values()]

    def refresh(self) -> list:
        now = self.clock()
        try:
            holding = {p["account_id"] for p in db.binance_positions(self.store, status="open", limit=1000)
                       if p["mode"] == "live" and p.get("account_id") is not None}
            version = f"{accounts.accounts_version(self.store)}|{sorted(holding)}"
            retry = any(now - t >= LIVE_RETRY_SEC for t in self.failed.values())
            if version == self.version and not retry:
                return self.traders
            items = [a for a in accounts.live_accounts(self.store, include_ids=holding)
                     if (a["binance_live"] and a["active"]) or a["id"] in holding]
        except Exception as e:
            log.warning("Binance live 계정 조회 실패 — 기존 트레이더 유지: %s", e)
            return self.traders
        changed, self.version = version != self.version, version
        keep = set()
        for acc in items:
            usable = acc["binance"] and (acc["id"] in holding or (acc["telegram"] and acc["binance_capital"] > 0))
            if acc["error"] or not usable:
                log.warning("계정 %s Binance live 건너뜀: %s", acc["email"], acc["error"] or "키·텔레그램·자본 확인")
                continue
            sign = (acc["binance"]["api_key"], acc["binance"]["api_secret"], (acc["telegram"] or {}).get("bot_token"),
                    (acc["telegram"] or {}).get("chat_id"), acc["binance_capital"])
            cur = self.by_account.get(acc["id"])
            if cur and cur[0] == sign:
                cur[1].account = acc
                keep.add(acc["id"])
                continue
            if not changed and now - self.failed.get(acc["id"], now - LIVE_RETRY_SEC) < LIVE_RETRY_SEC:
                continue
            trader = self.build(self.store, acc, self.symbols, self.leverage)
            if trader is None:
                self.failed[acc["id"]] = now
                continue
            self.failed.pop(acc["id"], None)
            self.by_account[acc["id"]] = (sign, trader)
            keep.add(acc["id"])
        for account_id in set(self.by_account) - keep:
            log.info("계정 %s Binance live 트레이더 제거 (스위치 OFF·키 변경·계정 중지, 열린 포지션 없음)", account_id)
            del self.by_account[account_id]
        return self.traders
