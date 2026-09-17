"""코인 신호 포지션 장부 — 진입 후보 알림을 따른 사람의 포지션을 알림 쪽에서 끝까지 관리한다.

워커가 🔵/🔴 진입 후보를 보내면 그 신호가·손절선·보유 한도를 여기 적어 두고, 이후 그 심볼의 알림은 진입이 아니라 관리다:
마크 가격이 손절선에 닿으면 🔴 손절하세요, 보유 한도가 되면 🟢 익절/정리하세요 를 보내고 장부에서 지운다. 시황 요약에는
조건 충족 여부 대신 '신호 진행 중 — 신호가 대비 손익' 을 싣는다(워커 status_lines 가 status_line 을 앞세운다).
자동매매(binance_trade.Trader)는 내 계좌(가상·실제)의 포지션이고 이 장부는 독자의 포지션이라 서로 독립이다 — 트레이더가
한도·킬 스위치로 진입을 건너뛰어도 독자에게 나간 신호는 끝까지 관리해야 한다.
상태는 alert_settings(binance_signal_book) 에 JSON 으로 저장해 재시작해도 이어진다.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

import requests

from . import db
from .binance_crash import fmt_price
from .config import BINANCE_FAPI
from .models import Signal

log = logging.getLogger("binance")

SETTING_KEY = "binance_signal_book"


def fetch_mark(symbol: str, session=None) -> float:
    """마크 가격 — 손절 판정은 실제 STOP_MARKET(MARK_PRICE) 주문과 같은 기준이다."""
    r = (session or requests).get(f"{BINANCE_FAPI}/fapi/v1/premiumIndex", params={"symbol": symbol}, timeout=10)
    r.raise_for_status()
    return float(r.json()["markPrice"])


def hold_text(hours: float) -> str:
    return f"{hours / 24:g}일" if hours >= 24 and hours % 24 == 0 else f"{hours:g}시간"


class SignalBook:
    def __init__(self, store, notifier, fetch_mark=fetch_mark, trades=None):
        self.store = store              # db.DB. None 이면 저장 없이 메모리만 (테스트)
        self.notify = notifier
        self.fetch_mark = fetch_mark
        self.trades = trades            # tracking.SignalTradeLog — 닫힌 신호의 모의 성적 (자정 성적표). None 이면 기록 없음
        self.open = {}                  # "kind:symbol" -> 신호 포지션 dict
        self._load()

    @staticmethod
    def _key(kind: str, symbol: str) -> str:
        return f"{kind}:{symbol}"

    def is_open(self, kind: str, symbol: str) -> bool:
        return self._key(kind, symbol) in self.open

    def opened(self, kind: str, symbol: str, side: str, label: str, name: str, price: float, stop: float,
               hold_hours: float, now: datetime = None) -> dict:
        """진입 후보 알림이 나간 직후. 같은 전략의 신호가 이미 열려 있으면(급락 추가매수) 이 봉 기준으로 갱신한다."""
        now = now or datetime.now(timezone.utc)
        row = {"kind": kind, "symbol": symbol, "side": side, "label": label, "name": name,
               "price": float(price), "stop": float(stop), "hold_hours": float(hold_hours),
               "opened_at": now.isoformat(timespec="seconds"),
               "deadline": (now + timedelta(hours=hold_hours)).isoformat(timespec="seconds")}
        self.open[self._key(kind, symbol)] = row
        self._save()
        return row

    @staticmethod
    def _pnl(p: dict, mark: float) -> float:
        sgn = 1 if p["side"] == "long" else -1
        return sgn * (mark / p["price"] - 1) * 100

    def poll(self, now: datetime = None) -> list:
        """열린 신호마다 마크 가격을 보고 손절선·보유 한도를 판정한다. 보낸 청산 Signal 목록."""
        now = now or datetime.now(timezone.utc)
        sent = []
        for key, p in list(self.open.items()):
            try:
                mark = self.fetch_mark(p["symbol"])
            except Exception as e:                  # 시세 실패면 다음 사이클에 다시 본다
                log.warning("%s 마크 가격 조회 실패: %s", p["symbol"], e)
                continue
            p["mark"] = mark
            hit = mark <= p["stop"] if p["side"] == "long" else mark >= p["stop"]
            if not hit and now < datetime.fromisoformat(p["deadline"]):
                continue
            signal = self._exit_signal(p, mark, "stop" if hit else "time", now)
            self.notify.send(signal)
            sent.append(signal)
            if self.trades is not None:
                pnl = round(self._pnl(p, mark), 2)
                self.trades.add(p["symbol"], self._name(p), "BINANCE", p["opened_at"], p["price"], mark, pnl,
                                "손절" if hit else ("익절" if pnl > 0 else "정리"), now)
            del self.open[key]
            self._save()
        return sent

    @staticmethod
    def _name(p: dict) -> str:
        return f"{p['symbol']} {p['name']}" + ("" if p["side"] == "long" else " 숏")

    def open_lines(self) -> list:
        """성적표의 '진행 중' 줄 — 열린 신호마다 신호가 대비 손익 (마크를 아직 못 봤으면 신호가만)."""
        out = []
        for p in self.open.values():
            mark = p.get("mark")
            out.append(f"{self._name(p)} 신호가 {fmt_price(p['price'])}"
                       + (f" → 마크 {fmt_price(mark)} {self._pnl(p, mark):+.2f}%" if mark else ""))
        return out

    def daily_report(self, day: str = None) -> str:
        """하루(KST, 기본 오늘)의 모의 성적표. 기록기가 없으면 빈 문자열."""
        return self.trades.daily_summary("BINANCE", self.open_lines(), day) if self.trades is not None else ""

    def _exit_signal(self, p: dict, mark: float, reason: str, now: datetime) -> Signal:
        long = p["side"] == "long"
        pnl = self._pnl(p, mark)
        held = (now - datetime.fromisoformat(p["opened_at"])).total_seconds() / 3600
        if reason == "stop":
            kind = "SELL"
            title = "🔴 손절하세요" if long else "🔴 숏 손절하세요"
            why = f"마크 {fmt_price(mark)} 이 손절선 {fmt_price(p['stop'])} 에 닿음"
        else:
            kind = "EXIT_FULL"
            title = f"🟢 {'' if long else '숏 '}전량 {'익절' if pnl > 0 else '정리'}하세요"
            why = f"보유 한도 {hold_text(p['hold_hours'])} 도달"
        held_text = f"{held:.1f}시간" if held < 48 else f"{held / 24:.1f}일"
        body = f"신호가 {fmt_price(p['price'])} 대비 {pnl:+.2f}% ({held_text})\n{why}"        # 주식 청산 알림처럼 첫 줄이 신호가 대비 손익
        return Signal(kind, title, p["label"], body, p["symbol"])

    def status_line(self, kind: str, symbol: str, now: datetime = None):
        """시황 요약용 — 열린 신호가 있으면 진행 상황 한 줄, 없으면 None (워커가 조건 줄을 쓴다)."""
        p = self.open.get(self._key(kind, symbol))
        if p is None:
            return None
        head = "🔵 매수 신호 진행 중" if p["side"] == "long" else "🔴 숏 신호 진행 중"
        mark = p.get("mark")
        move = f" 대비 {self._pnl(p, mark):+.2f}%" if mark else ""
        return f"{head} — 신호가 {fmt_price(p['price'])}{move}, 손절 {fmt_price(p['stop'])}"

    # -- 저장 -------------------------------------------------------------------
    def _save(self):
        if self.store is None:
            return
        rows = [{k: v for k, v in p.items() if k != "mark"} for p in self.open.values()]
        try:
            db.set_setting(self.store, SETTING_KEY, json.dumps(rows, ensure_ascii=False))
        except Exception as e:                      # 저장 실패가 알림을 막으면 안 된다
            log.warning("신호 포지션 저장 실패: %s", e)

    def _load(self):
        if self.store is None:
            return
        try:
            rows = json.loads(db.get_settings(self.store).get(SETTING_KEY) or "[]")
        except Exception as e:
            log.warning("신호 포지션 복원 실패: %s", e)
            return
        for p in rows:
            self.open[self._key(p["kind"], p["symbol"])] = p
        if rows:
            log.info("신호 포지션 복원: %s", ", ".join(f"{p['symbol']} {p['name']}" for p in rows))
