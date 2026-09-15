"""Binance 무기한 선물 4시간봉 급등 추종 알림 — 급등 확인(관찰) → 첫 눌림 뒤 EMA9 재돌파(진입 후보).

run_binance.py 가 5분봉 급락 워커와 같은 프로세스에서 돌린다. 데이터·지표 도우미는 binance_crash 를 재사용한다.

판정 — 2026-09-15 스윙 분석 「스윙의 급등과 급락」에서 4시간봉 BTC 에 우위가 확인된 'M2' 트리거:
  급등    직전 30봉(5일) 저점 대비 종가 상승폭 ≥ 기준 ATR × 6, RSI14 ≥ 70. 처음 성립한 봉에 📈 관찰 알림(review)
  재돌파  급등 봉 뒤 10봉 안에 종가가 EMA9 아래로 눌렸다가 다시 위로 마감한 봉에 🔵 진입 후보(action).
          '급등 뒤 n봉'은 눌림 이전의 마지막 급등 봉 기준이고, 눌림 저점은 그 뒤 봉들의 최저가다
  국면    일봉 종가가 EMA200 위일 때만 (약세 국면 표본은 기대값이 음수였다)
기준 ATR = 직전 30일(180봉) ATR14% 의 중앙값. 손절·목표 참고선은 ±8 기준 ATR(1:1), 보유 한도 7일(42봉).
급등 숏은 모든 봉에서 손실이라 만들지 않는다 — 급등은 추종 후보다.
"""

import logging
from datetime import datetime, timedelta, timezone
from statistics import median

from .binance_crash import KST, atr_pct_series, fetch_funding, fetch_klines, fmt_price
from .config import (SURGE_ATR_MULT, SURGE_BASE_ATR_BARS, SURGE_BRACKET_ATR, SURGE_DAILY_KLINES,
                     SURGE_FUNDING_WARN, SURGE_HOLD_BARS, SURGE_INTERVAL, SURGE_KLINES, SURGE_LOOKBACK,
                     SURGE_REENTRY_BARS, SURGE_REQUIRE_BULL, SURGE_RSI_MIN, SURGE_RVOL_WINDOW)
from .indicators import compute_ema, compute_rsi
from .models import Signal

log = logging.getLogger("binance")


def base_atr_pct(bars: list) -> float:
    """직전 30일 ATR% 중앙값 (마지막 봉 제외). 표본이 절반도 안 되면 0 — 판정을 보류한다."""
    series = atr_pct_series(bars)[:-1][-SURGE_BASE_ATR_BARS:]
    if len(series) < SURGE_BASE_ATR_BARS // 2:
        return 0.0
    return median(series)


def spike_metrics(bars: list, base: float):
    """마지막 봉의 급등 지표. 봉이 모자라면 None."""
    if len(bars) < SURGE_LOOKBACK + 16:
        return None
    sig = bars[-1]
    ref_low = min(b["low"] for b in bars[-1 - SURGE_LOOKBACK:-1])
    rise = (sig["close"] / ref_low - 1) * 100
    _, rsi = compute_rsi([{"closePrice": b["close"]} for b in bars])
    mult = rise / base
    return {"ref_low": ref_low, "rise": rise, "mult": mult, "rsi": rsi,
            "spike": mult >= SURGE_ATR_MULT and rsi >= SURGE_RSI_MIN}


def regime(daily_bars: list):
    """(강세 여부, 마지막 완성 일봉 종가, EMA200). 일봉이 200개 미만이면 None."""
    closes = [b["close"] for b in daily_bars or []]
    if len(closes) < 200:
        return None
    ema = compute_ema(closes, 200)
    return closes[-1] > ema, closes[-1], ema


def evaluate(bars: list, daily_bars: list = None, funding=None):
    """마지막 완성봉에서 관찰(spike)·진입(reentry) 판정. 해당 없으면 None.

    국면은 판정하지 않고 값만 싣는다 — 보류 여부는 워커가 SURGE_REQUIRE_BULL 로 정한다.
    """
    base = base_atr_pct(bars)
    if base <= 0:
        return None
    now, prev = spike_metrics(bars, base), spike_metrics(bars[:-1], base)
    if now is None or prev is None:
        return None
    closes = [b["close"] for b in bars]
    ema_now, ema_prev = compute_ema(closes, 9), compute_ema(closes[:-1], 9)
    stage, ago = None, 0
    if closes[-1] > ema_now and closes[-2] <= ema_prev:          # EMA9 재돌파 봉
        dip = 1                                                   # 직전 봉부터 이어진 EMA9 아래 봉 수
        while dip < SURGE_REENTRY_BARS and closes[-2 - dip] <= compute_ema(closes[:-1 - dip], 9):
            dip += 1
        for k in range(dip + 1, SURGE_REENTRY_BARS + 1):          # 눌림 이전의 마지막 급등 봉을 찾는다
            m = spike_metrics(bars[:-k], base)
            if m and m["spike"]:
                stage, ago = "reentry", k
                break
    if stage is None and now["spike"] and not prev["spike"]:    # 급등이 처음 성립한 봉
        stage = "spike"
    if stage is None:
        return None
    sig = bars[-1]
    vol_med = median(b["volume"] for b in bars[-1 - SURGE_RVOL_WINDOW:-1])
    out = {
        "stage": stage, "open_time": sig["open_time"], "close": sig["close"], "ref_low": now["ref_low"],
        "rise": now["rise"], "mult": now["mult"], "rsi": now["rsi"], "base": base, "ema9": ema_now,
        "rvol": sig["volume"] / vol_med if vol_med > 0 else 0.0, "spike_ago": ago,
        "pullback_low": min(b["low"] for b in bars[-ago:]) if ago else sig["low"],   # 급등 봉 이후 ~ 현재 봉
        "stop": sig["close"] * (1 - SURGE_BRACKET_ATR * base / 100),
        "target": sig["close"] * (1 + SURGE_BRACKET_ATR * base / 100),
        "funding": funding,
    }
    reg = regime(daily_bars)
    if reg is not None:
        out["bull"], out["daily_close"], out["ema200"] = reg
    return out


def build_signal(symbol: str, r: dict) -> Signal:
    when = datetime.fromtimestamp(r["open_time"] / 1000, tz=timezone.utc).astimezone(KST)
    if "bull" in r:
        reg = f"국면 {'강세' if r['bull'] else '약세'} (일봉 {fmt_price(r['daily_close'])} / EMA200 {fmt_price(r['ema200'])})"
    else:
        reg = "국면 불명 (일봉 부족)"
    lines = [f"{fmt_price(r['close'])} ({when:%m-%d %H:%M} KST 봉) · 5일 저점 {fmt_price(r['ref_low'])} 대비 "
             f"{r['rise']:+.2f}% (기준ATR {r['mult']:.1f}배) · RSI14 {r['rsi']:.1f} · RVOL {r['rvol']:.1f}배"]
    if r["stage"] == "reentry":
        pull = (r["pullback_low"] / r["close"] - 1) * 100
        lines.append(f"급등 뒤 {r['spike_ago']}봉 만에 EMA9 {fmt_price(r['ema9'])} 재돌파 · "
                     f"눌림 저점 {fmt_price(r['pullback_low'])} ({pull:+.2f}%)")
    else:
        lines.append(f"EMA9 {fmt_price(r['ema9'])} · 눌림 뒤 재돌파를 {SURGE_REENTRY_BARS}봉 안에 기다린다")
    fund = ""
    if r.get("funding") is not None:
        fund = f" · 펀딩 {r['funding'] * 100:+.4f}%/8h"
        if r["funding"] > SURGE_FUNDING_WARN:
            fund += " ⚠ 과열 — 크기 축소"
    lines.append(reg + fund)
    lines.append(f"참고: 손절 {fmt_price(r['stop'])} · 목표 {fmt_price(r['target'])} (±{SURGE_BRACKET_ATR:g} 기준ATR) · "
                 f"보유 한도 {SURGE_HOLD_BARS * 4 // 24}일")
    if r["stage"] == "reentry":
        return Signal("SURGE_ENTRY", "🔵 눌림 재돌파 — 추종 진입 후보", f"{symbol} 4시간봉", "\n".join(lines), symbol)
    return Signal("SURGE_WATCH", "📈 급등 확인 — 추종 관찰", f"{symbol} 4시간봉", "\n".join(lines), symbol)


class SurgeWorker:
    """심볼별로 새 4시간 완성봉이 생길 때마다 한 번 판정. 단계별로 보유 한도(42봉) 동안 한 번만 알린다."""

    def __init__(self, symbols: list, notifier, fetch_bars=fetch_klines, fetch_fund=fetch_funding):
        self.symbols = list(symbols)
        self.notify = notifier
        self.fetch_bars = fetch_bars
        self.fetch_fund = fetch_fund
        self.last_bar = {}          # symbol -> 마지막으로 판정한 완성봉 open_time
        self.last_alert = {}        # (symbol, stage) -> 마지막 알림 시각

    def poll_once(self, now: datetime = None) -> list:
        now = now or datetime.now(timezone.utc)
        sent = []
        for symbol in self.symbols:
            bars = self.fetch_bars(symbol, SURGE_INTERVAL, SURGE_KLINES)
            if not bars or bars[-1]["open_time"] == self.last_bar.get(symbol):
                continue
            self.last_bar[symbol] = bars[-1]["open_time"]
            result = evaluate(bars, self.fetch_bars(symbol, "1d", SURGE_DAILY_KLINES))
            if result is None:
                continue
            if SURGE_REQUIRE_BULL and result.get("bull") is False:
                log.info("%s 4시간봉 급등(%s)이지만 일봉 EMA200 아래라 보류", symbol, result["stage"])
                continue
            key = (symbol, result["stage"])
            last = self.last_alert.get(key)
            if last and now - last < timedelta(hours=4 * SURGE_HOLD_BARS):
                log.info("%s %s 조건 충족했지만 쿨다운 중 (마지막 알림 %s)", symbol, result["stage"],
                         last.isoformat(timespec="minutes"))
                continue
            result["funding"] = self.fetch_fund(symbol)
            signal = build_signal(symbol, result)
            self.notify.send(signal)
            self.last_alert[key] = now
            sent.append(signal)
        return sent
