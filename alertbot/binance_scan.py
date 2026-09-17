"""Binance 무기한 선물 급변 감시 — 거래대금 상위 코인의 급등·급락 감지 알림과 급등 소진 숏(공용 가상 장부 전용).

2026-09-16 LSK·SYN·BR·BULLA 같은 알트 급등을 ETC·BTC 만 보던 워커가 하나도 알리지 못해 추가했다. run_binance.py 가 다른 워커와 같은 프로세스에서 돌린다.

유니버스  USDT 무기한 코인(exchangeInfo PERPETUAL·TRADING·underlyingType COIN — 주식·금·원유·지수 선물 제외, 스테이블·금 연동 토큰
          SCAN_EXCLUDE 제외) 중 직전 24시간 거래대금 상위 SCAN_TOP_N 개를 SCAN_REFRESH_MIN 분마다 다시 고른다.
          관리자가 백오피스 '종목' 화면에서 코인을 추가·제외한다(alert_settings binance_scan_include / binance_scan_exclude).
감지      SCAN_INTERVAL 완성봉 종가가 직전 SCAN_WINDOW_BARS 봉 저점(고점) 대비 기준ATR × SCAN_ATR_MULT 이상 오르면(내리면) 급등(급락).
          기준 ATR = 직전 SCAN_BASE_ATR_BARS 봉(30일) ATR14% 중앙값, 신호봉 제외. 한 번 알린 코인은 방향과 무관하게 창 길이(4시간) 동안 쉰다.
          한 사이클에서 같은 방향으로 여러 코인이 뜨면 알림 하나로 묶는다.
근거      2026-05-17~09-17 4개월 분석 (보고서 「최근 4개월 코인 신호 재검증」, README 3.5):
          - 1시간봉·4시간 창·기준ATR 30일·종목 단위 쿨다운이 하루 알림 중앙값 10건 기준에서 큰 움직임(24시간 극값 대비 +15%/−12%)을 가장 많이 잡았다.
            1시간 안 포착률은 기준ATR 7일·방향별 쿨다운이 조금 높았지만, 한 주 내내 들썩인 코인(9-16 LSK)의 반복 급등을 놓쳤다.
          - 감지 뒤 곧바로 추종·반전·페이드하는 진입 8개 조합은 수수료·펀딩 뒤 우위가 검증되지 않았다.
소진 숏    2026-09-17 4개월 분석(보고서 「급등 코인 소진 숏」): 급등 감지 코인은 72시간 중앙 −11.6% 흘러내렸지만 감지 직후 더 오르는 일이 많아,
          감지 뒤 SCAN_FADE_WAIT_HOURS 안에 1시간봉 종가가 EMA(SCAN_FADE_EMA) 아래로 마감하는 첫 봉에서 숏을 연다 (전략 SCAN_FADE).
          손절·목표가는 그 종가 기준 ±SCAN_FADE_STOP_PCT / SCAN_FADE_TP_PCT % (마크), 보유 SCAN_FADE_HOLD_HOURS 시간. 공용 가상 장부만 — 계정 live 는 주문하지 않는다.
          대기 목록은 alert_settings binance_scan_fade_pending 에 두어 재시작해도 이어지고, 대기 코인은 상위 30 에서 빠져도 계속 본다.
          급락 감지는 매매하지 않는다 (급락 뒤 숏·롱 모두 표본 밖에서 우위가 없었다).
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from statistics import median

import requests

from . import db
from .binance_crash import fetch_klines, fmt_price
from .binance_follow import base_atr_pct
from .config import (BINANCE_FAPI, SCAN_ATR_MULT, SCAN_BASE_ATR_BARS, SCAN_EXCLUDE, SCAN_FADE_EMA, SCAN_FADE_HOLD_HOURS, SCAN_FADE_STOP_PCT,
                     SCAN_FADE_TP_PCT, SCAN_FADE_WAIT_HOURS, SCAN_INTERVAL, SCAN_KLINES, SCAN_MAX_LINES, SCAN_REFRESH_MIN, SCAN_TOP_N,
                     SCAN_WINDOW_BARS)
from .models import Signal

log = logging.getLogger("binance")

STEP_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}
INTERVAL_TEXT = {"15m": "15분봉", "1h": "1시간봉", "4h": "4시간봉"}
INCLUDE_KEY, EXCLUDE_KEY, SNAPSHOT_KEY = "binance_scan_include", "binance_scan_exclude", "binance_scan_universe"
FADE_KEY = "binance_scan_fade_pending"
ALERT_KEY = "binance_scan_last_alert"
FADE_STRATEGY = "SCAN_FADE"


def window_text() -> str:
    hours = SCAN_WINDOW_BARS * STEP_MS[SCAN_INTERVAL] / 3_600_000
    return f"{hours:g}시간"


# -- 유니버스 ----------------------------------------------------------------------

def fetch_exchange_info(session=None) -> dict:
    r = (session or requests).get(f"{BINANCE_FAPI}/fapi/v1/exchangeInfo", timeout=10)
    r.raise_for_status()
    return r.json()


def fetch_tickers(session=None) -> list:
    r = (session or requests).get(f"{BINANCE_FAPI}/fapi/v1/ticker/24hr", timeout=10)
    r.raise_for_status()
    return r.json()


def eligible(info: dict) -> set:
    """감시할 수 있는 코인 — USDT 무기한 코인만 (주식·금·원유 TRADIFI_PERPETUAL, 지수 INDEX, 거래 중지, 스테이블·금 연동 토큰 제외)."""
    return {s["symbol"] for s in info.get("symbols", [])
            if s.get("contractType") == "PERPETUAL" and s.get("status") == "TRADING" and s.get("underlyingType") == "COIN"
            and s.get("quoteAsset") == "USDT" and s["symbol"] not in SCAN_EXCLUDE}


def parse_list(text: str) -> list:
    """'lsk, ARBUSDT zec' → ['LSKUSDT', 'ARBUSDT', 'ZECUSDT'] — 쉼표·공백으로 나누고 USDT 가 없으면 붙인다. 순서 유지, 중복 제거."""
    out = []
    for part in re.split(r"[,\s]+", text or ""):
        symbol = part.strip().upper()
        if not symbol:
            continue
        if not symbol.endswith("USDT"):
            symbol += "USDT"
        if symbol not in out:
            out.append(symbol)
    return out


def pick(tickers: list, allowed: set, include: list, exclude: list, n: int) -> tuple:
    """(감시 심볼, 자동 선정 행 [{symbol, rank, quote_volume}], 거래할 수 없는 추가 심볼). 제외를 먼저 빼고 순위를 매긴다."""
    ranked = sorted((t for t in tickers if t["symbol"] in allowed and t["symbol"] not in exclude),
                    key=lambda t: float(t["quoteVolume"]), reverse=True)[:n]
    top = [{"symbol": t["symbol"], "rank": i + 1, "quote_volume": float(t["quoteVolume"])} for i, t in enumerate(ranked)]
    symbols = [r["symbol"] for r in top]
    symbols += [s for s in include if s in allowed and s not in symbols and s not in exclude]
    return symbols, top, [s for s in include if s not in allowed]


class Universe:
    """상위 N + 추가 − 제외. 거래소 조회는 갱신 주기에만 하고, 추가·제외 설정이 바뀌면 조회 없이 곧바로 다시 고른다.
    조회에 실패하면 직전 목록을 쓰고 1분 뒤 다시 시도한다. 고른 결과는 백오피스가 보도록 alert_settings 에 적는다."""

    def __init__(self, store, fetch_info=fetch_exchange_info, fetch_tickers=fetch_tickers, n: int = SCAN_TOP_N,
                 refresh_min: int = SCAN_REFRESH_MIN):
        self.store, self.fetch_info, self.fetch_tickers = store, fetch_info, fetch_tickers
        self.n, self.refresh = n, timedelta(minutes=refresh_min)
        self.allowed, self.tickers = set(), []
        self.next_fetch = None
        self.lists = None                    # (include, exclude) — 마지막으로 반영한 설정
        self.current, self.ranks = [], {}

    def symbols(self, now: datetime) -> list:
        lists = self._lists()
        due = self.next_fetch is None or now >= self.next_fetch
        if due:
            try:
                self.allowed, self.tickers = eligible(self.fetch_info()), self.fetch_tickers()
                self.next_fetch = now + self.refresh
            except Exception as e:
                log.warning("급변 감시 유니버스 갱신 실패 — 직전 목록 유지: %s", e)
                self.next_fetch = now + timedelta(minutes=1)
                due = False
        if due or lists != self.lists:
            self.lists = lists
            self.current, top, invalid = pick(self.tickers, self.allowed, lists[0], lists[1], self.n)
            self.ranks = {r["symbol"]: r["rank"] for r in top}
            self._snapshot(now, top, lists, invalid)
        return self.current

    def _lists(self) -> tuple:
        if self.store is None:
            return self.lists or ((), ())
        try:
            s = db.get_settings(self.store)
        except Exception as e:                   # 설정을 못 읽으면 직전 설정대로
            log.warning("급변 감시 추가·제외 설정 조회 실패: %s", e)
            return self.lists or ((), ())
        return tuple(parse_list(s.get(INCLUDE_KEY))), tuple(parse_list(s.get(EXCLUDE_KEY)))

    def _snapshot(self, now: datetime, top: list, lists: tuple, invalid: list):
        if self.store is None or not self.tickers:
            return
        snap = {"at": now.isoformat(timespec="seconds"), "top": top, "include": list(lists[0]), "exclude": list(lists[1]),
                "invalid": invalid, "symbols": self.current}
        try:
            db.set_setting(self.store, SNAPSHOT_KEY, json.dumps(snap, ensure_ascii=False))
        except Exception as e:
            log.warning("급변 감시 목록 저장 실패: %s", e)


# -- 감지 ------------------------------------------------------------------------

def move(bars: list, base_bars: int = SCAN_BASE_ATR_BARS, window: int = SCAN_WINDOW_BARS):
    """마지막 완성봉의 급변 지표. 기준 ATR·직전 창 극값은 신호봉을 뺀다. 표본이 모자라면 None."""
    if len(bars) < window + 2:
        return None
    base = base_atr_pct(bars, base_bars)
    if base <= 0:
        return None
    sig, prior = bars[-1], bars[-1 - window:-1]
    low, high = min(b["low"] for b in prior), max(b["high"] for b in prior)
    up, down = (sig["close"] / low - 1) * 100, (1 - sig["close"] / high) * 100
    vols = [b["volume"] for b in bars[-1 - window * 6:-1]]
    vol_med = median(vols) if vols else 0.0
    return {"open_time": sig["open_time"], "close": sig["close"], "base": base, "up": up, "down": down,
            "up_mult": up / base, "down_mult": down / base, "rvol": sig["volume"] / vol_med if vol_med > 0 else 0.0}


def ema(values: list, period: int) -> float:
    """지수이동평균의 마지막 값. indicators.compute_ema 와 같은 식이지만 반올림하지 않는다 — 1센트 미만 코인도 종가와 비교해야 한다.
    표본이 period 보다 적으면 0."""
    if len(values) < period:
        return 0.0
    k, e = 2 / (period + 1), sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def build_signal(side: str, rows: list, ranks: dict, fade: bool = False) -> Signal:
    """rows = [(symbol, 지표)] 배수 내림차순. 여러 코인이면 한 알림에 SCAN_MAX_LINES 줄까지. fade 면 급등 알림에 가상 장부 소진 숏 규칙을 적는다."""
    up = side == "up"
    single = len(rows) == 1                 # 한 종목이면 제목에 이름이 있으니 본문은 '현재가' 로 시작한다
    lines = []
    for symbol, m in rows[:SCAN_MAX_LINES]:
        rank = ranks.get(symbol)
        lines.append(f"{'현재가' if single else symbol} {fmt_price(m['close'])} · {window_text()} {'+' if up else '-'}{m[side]:.1f}% · "
                     f"거래량 {m['rvol']:.1f}배 · "
                     + (f"거래대금 {rank}위" if rank else "추가 코인"))
    if len(rows) > SCAN_MAX_LINES:
        lines.append(f"외 {len(rows) - SCAN_MAX_LINES}종목")
    lines.append(f"{SCAN_FADE_WAIT_HOURS}시간 안에 1시간 종가가 EMA{SCAN_FADE_EMA} 아래면 가상 숏 (실제 주문 없음)" if up and fade
                 else "관찰 알림 — 매매 신호 아님")
    label = rows[0][0] if single else f"코인 {len(rows)}종목"
    return Signal("SCAN_SURGE" if up else "SCAN_CRASH", "🚀 급등 감지" if up else "💥 급락 감지", label, "\n".join(lines),
                  rows[0][0] if single else None)


class ScanWorker:
    """거래대금 상위 코인을 완성봉마다 한 번씩 본다. 새 봉이 아직 안 생긴 코인은 조회하지 않는다 (사이클 20초, 봉 1시간).
    trader 가 있으면 급등 감지 코인을 소진 숏 대기 목록에 올리고, 대기 코인의 새 완성봉마다 EMA 이탈을 확인해 가상 장부에 숏을 연다."""

    def __init__(self, universe: Universe, notifier, fetch_bars=fetch_klines, trader=None):
        self.universe, self.notify, self.fetch_bars, self.trader = universe, notifier, fetch_bars, trader
        self.store = getattr(universe, "store", None)
        self.last_bar = {}          # symbol -> 마지막으로 판정한 완성봉 open_time
        # symbol -> 마지막 알림 시각 (방향과 무관한 쿨다운, 24시간 감지 수). DB 에 두어 재시작 직후 같은 봉·쿨다운 안의 코인을 다시 알리지 않는다
        self.last_alert = {s: datetime.fromtimestamp(ms / 1000, tz=timezone.utc) for s, ms in self._load(ALERT_KEY).items()}
        self.status = {}            # symbol -> 마지막 완성봉 지표 (시황 요약용)
        # symbol -> {deadline: 대기 끝 ms, after: 감지 봉 open_time} (소진 숏 대기)
        self.pending = {s: {"deadline": int(p["deadline"]), "after": int(p["after"])} for s, p in self._load(FADE_KEY).items()}

    def poll_once(self, now: datetime = None) -> list:
        now = now or datetime.now(timezone.utc)
        now_ms, step = int(now.timestamp() * 1000), STEP_MS[SCAN_INTERVAL]
        cooldown = timedelta(milliseconds=SCAN_WINDOW_BARS * step)
        symbols = self.universe.symbols(now)
        hits = {"up": [], "down": []}
        changed = self._expire(now_ms, step)
        for symbol in list(symbols) + [s for s in self.pending if s not in symbols]:     # 대기 코인은 상위 30 에서 빠져도 본다
            last = self.last_bar.get(symbol)
            if last is not None and now_ms < last + 2 * step + 3000:       # 다음 봉이 아직 안 끝났다
                continue
            try:
                bars = self.fetch_bars(symbol, SCAN_INTERVAL, SCAN_KLINES)
                if not bars or bars[-1]["open_time"] == last:
                    continue
                self.last_bar[symbol] = bars[-1]["open_time"]
                if symbol in self.pending:
                    changed |= self._check_fade(symbol, bars, now)
                if symbol not in symbols:           # 대기 때문에만 본 코인은 감지·시황에 넣지 않는다
                    continue
                m = move(bars)
                self.status[symbol] = m
                if m is None:
                    continue
                prev = self.last_alert.get(symbol)
                if prev is not None and now - prev < cooldown:
                    continue
                side = max(("up", "down"), key=lambda s: m[f"{s}_mult"])
                if m[f"{side}_mult"] >= SCAN_ATR_MULT:
                    hits[side].append((symbol, m))
            except Exception as e:              # 한 코인의 오류가 나머지를 막지 않는다
                log.warning("%s 급변 감시 실패: %s", symbol, e)
        for gone in set(self.status) - set(symbols):
            self.status.pop(gone, None)
        sent = []
        for side in ("up", "down"):
            rows = sorted(hits[side], key=lambda r: r[1][f"{side}_mult"], reverse=True)
            if not rows:
                continue
            signal = build_signal(side, rows, self.universe.ranks, fade=self.trader is not None)
            self.notify.send(signal)
            for symbol, m in rows:
                self.last_alert[symbol] = now
                if side == "up" and self.trader is not None:     # 같은 코인이 다시 급등하면 대기를 새로 잡는다
                    self.pending[symbol] = {"deadline": m["open_time"] + step + SCAN_FADE_WAIT_HOURS * 3_600_000,
                                            "after": m["open_time"]}
                    changed = True
            sent.append(signal)
        if sent:
            self._save(ALERT_KEY, {s: int(t.timestamp() * 1000) for s, t in self.last_alert.items() if now - t < timedelta(hours=24)})
        if changed:
            self._save(FADE_KEY, self.pending)
        return sent

    # -- 급등 소진 숏 ----------------------------------------------------------------
    def _check_fade(self, symbol: str, bars: list, now: datetime) -> bool:
        """대기 코인의 새 완성봉 — 대기 시간이 지났으면 거두고, 종가가 EMA 아래로 마감했으면 가상 장부에 숏을 연다. 대기 목록이 바뀌면 True.
        첫 이탈 봉에서 대기를 끝낸다 — 같은 코인 보유 중·동시 보유 상한으로 보류돼도 다시 기다리지 않는다 (분석의 규칙과 같다)."""
        p, last = self.pending[symbol], bars[-1]
        if last["open_time"] <= p["after"]:
            return False
        if last["open_time"] + STEP_MS[SCAN_INTERVAL] > p["deadline"]:
            del self.pending[symbol]
            log.info("%s 소진 숏 대기 만료 — %d시간 안에 EMA%d 아래로 꺾이지 않았다", symbol, SCAN_FADE_WAIT_HOURS, SCAN_FADE_EMA)
            return True
        line = ema([b["close"] for b in bars], SCAN_FADE_EMA)
        if line <= 0 or last["close"] >= line:
            return False
        del self.pending[symbol]
        price = last["close"]
        result = {"stop": price * (1 + SCAN_FADE_STOP_PCT / 100), "take_profit": price * (1 - SCAN_FADE_TP_PCT / 100),
                  "open_time": last["open_time"], "funding": None}
        log.info("%s 종가 %s 가 EMA%d %s 아래 — 소진 숏 진입", symbol, fmt_price(price), SCAN_FADE_EMA, fmt_price(line))
        try:
            self.trader.on_entry(FADE_STRATEGY, symbol, "short", result, SCAN_FADE_HOLD_HOURS, now, notify_skip=False)
        except Exception as e:                  # 가상 장부 오류가 감시를 멈추면 안 된다
            log.warning("%s 소진 숏 진입 처리 실패: %s", symbol, e)
        return True

    def _expire(self, now_ms: int, step: int) -> bool:
        """조회가 계속 실패해 대기 끝을 두 봉 넘게 지난 코인을 거둔다."""
        stale = [s for s, p in self.pending.items() if now_ms > p["deadline"] + 2 * step]
        for s in stale:
            del self.pending[s]
        return bool(stale)

    def _load(self, key: str) -> dict:
        """alert_settings 의 JSON 상태 (쿨다운·소진 숏 대기). 못 읽으면 빈 상태로 시작한다 — 감지는 계속된다."""
        if self.store is None:
            return {}
        try:
            return json.loads(db.get_settings(self.store).get(key) or "{}")
        except Exception as e:
            log.warning("급변 감시 상태 복원 실패 (%s): %s", key, e)
            return {}

    def _save(self, key: str, value: dict):
        if self.store is None:
            return
        try:
            db.set_setting(self.store, key, json.dumps(value))
        except Exception as e:
            log.warning("급변 감시 상태 저장 실패 (%s): %s", key, e)

    def status_lines(self, now: datetime = None) -> list:
        """시황 요약 한 줄 — 감시 수, 가장 크게 오른·내린 코인, 24시간 동안 알린 코인 수. 이름 부분은 고정이라 백오피스 시계열의 한 행이 된다."""
        now = now or datetime.now(timezone.utc)
        head = f"급변 감시 {INTERVAL_TEXT[SCAN_INTERVAL]} 상위{SCAN_TOP_N}"
        seen = {s: m for s, m in self.status.items() if m}
        if not seen:
            return [f"{head}  데이터 부족 (감시 {len(self.universe.current)}종목)"]
        up_sym = max(seen, key=lambda s: seen[s]["up_mult"])
        dn_sym = max(seen, key=lambda s: seen[s]["down_mult"])
        u, d = seen[up_sym], seen[dn_sym]
        recent = sum(1 for t in self.last_alert.values() if now - t < timedelta(hours=24))
        fade = f" · 숏 대기 {len(self.pending)}종목" if self.trader is not None else ""
        return [f"{head}  최대 상승 {up_sym} {u['up']:+.1f}% · 최대 하락 {dn_sym} {-d['down']:+.1f}% | 24시간 감지 {recent}종목{fade}"]
