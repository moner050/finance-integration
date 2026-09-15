"""Binance 무기한 선물 5분봉 급락 매수 알림 — 공개 REST(klines) 폴링 → 완성봉 판정 → 알림.

토스 엔진(run_engine.py)과 별개 워커다. 코인 선물은 24시간이라 장 시간·보유·세션 개념이 없고
데이터 소스도 다르므로 엔진 상태기계에 끼워 넣지 않는다. 공유하는 것은 알림 채널·쿨다운·
신호 이력(MySQL alert_signal_log)뿐이다. API 키는 필요 없다 (공개 엔드포인트).

판정 — 2026-09-15 분석 「급락 바닥과 급등 천장」에서 5분봉 ETC 에 우위가 확인된 'B +반전봉' 트리거:
  급락    직전 48봉(4시간) 고점 대비 종가 하락폭 ≥ 기준 ATR × 10
  과매도  RSI14 ≤ 30
  반전봉  신호봉 종가가 봉 범위의 상위 40% (종가 위치 ≥ 0.6)
기준 ATR = 직전 3일(864봉) ATR14% 의 중앙값. 급락 자체가 ATR 을 부풀리는 효과를 뺀 변동성 척도다.
RVOL·꼬리·테이커·BTC 동반·펀딩은 조건이 아니라 판단 참고로 메시지에 싣는다.
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from statistics import median

import requests

from .config import (BINANCE_FAPI, BINANCE_INTERVAL, BINANCE_KLINES, BINANCE_POLL_SEC, CRASH_ATR_MULT,
                     CRASH_BASE_ATR_BARS, CRASH_BETA_BTC, CRASH_CLOSE_POS_MIN, CRASH_COOLDOWN_MIN,
                     CRASH_LOOKBACK, CRASH_RSI_MAX, CRASH_RVOL_WINDOW)
from .indicators import compute_rsi
from .models import Signal

log = logging.getLogger("binance")

KST = timezone(timedelta(hours=9))


# -- 데이터 --------------------------------------------------------------------

def parse_klines(rows: list, now_ms: int) -> list:
    """Binance kline 배열 → 완성봉 dict 목록(시간순). 종료 시각이 아직 안 지난 마지막 봉은 뺀다."""
    out = []
    for k in rows:
        if int(k[6]) > now_ms:
            continue
        out.append({"open_time": int(k[0]), "open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
                    "close": float(k[4]), "volume": float(k[5]), "close_time": int(k[6]),
                    "taker_buy": float(k[9])})
    return out


def fetch_klines(symbol: str, session=None) -> list:
    r = (session or requests).get(f"{BINANCE_FAPI}/fapi/v1/klines",
                                  params={"symbol": symbol, "interval": BINANCE_INTERVAL, "limit": BINANCE_KLINES},
                                  timeout=10)
    r.raise_for_status()
    return parse_klines(r.json(), int(time.time() * 1000))


def fetch_funding(symbol: str, session=None):
    """직전 펀딩비 (소수, 0.0001 = 0.01%). 실패하면 None — 참고 정보라 알림을 막지 않는다."""
    try:
        r = (session or requests).get(f"{BINANCE_FAPI}/fapi/v1/premiumIndex", params={"symbol": symbol}, timeout=10)
        r.raise_for_status()
        return float(r.json()["lastFundingRate"])
    except (requests.RequestException, ValueError, KeyError, TypeError) as e:
        log.warning("%s 펀딩비 조회 실패: %s", symbol, e)
        return None


# -- 지표 ----------------------------------------------------------------------

def atr_pct_series(bars: list, period: int = 14) -> list:
    """봉마다 Wilder ATR 을 종가 대비 % 로."""
    out, atr, prev_close = [], None, None
    for b in bars:
        hl = b["high"] - b["low"]
        tr = hl if prev_close is None else max(hl, abs(b["high"] - prev_close), abs(b["low"] - prev_close))
        atr = tr if atr is None else (atr * (period - 1) + tr) / period
        out.append(atr / b["close"] * 100 if b["close"] > 0 else 0.0)
        prev_close = b["close"]
    return out


def base_atr_pct(bars: list) -> float:
    """직전 3일 ATR% 중앙값 (신호봉 제외). 표본이 절반도 안 되면 0 — 판정을 보류한다."""
    series = atr_pct_series(bars)[:-1][-CRASH_BASE_ATR_BARS:]
    if len(series) < CRASH_BASE_ATR_BARS // 2:
        return 0.0
    return median(series)


def evaluate(bars: list, btc_bars: list = None, funding=None):
    """마지막 완성봉을 신호봉으로 판정. 조건을 만족하면 메시지에 쓸 값들을 dict 로, 아니면 None."""
    if len(bars) < CRASH_LOOKBACK + 2:
        return None
    base = base_atr_pct(bars)
    if base <= 0:
        return None
    sig = bars[-1]
    ref_high = max(b["high"] for b in bars[-1 - CRASH_LOOKBACK:-1])
    drop = (sig["close"] / ref_high - 1) * 100
    mult = -drop / base
    _, rsi = compute_rsi([{"closePrice": b["close"]} for b in bars])
    rng = sig["high"] - sig["low"]
    close_pos = (sig["close"] - sig["low"]) / rng if rng > 0 else 1.0
    if mult < CRASH_ATR_MULT or rsi > CRASH_RSI_MAX or close_pos < CRASH_CLOSE_POS_MIN:
        return None
    vol_med = median(b["volume"] for b in bars[-1 - CRASH_RVOL_WINDOW:-1])
    out = {
        "open_time": sig["open_time"], "close": sig["close"], "low": sig["low"], "ref_high": ref_high,
        "drop": drop, "mult": mult, "base": base, "rsi": rsi, "close_pos": close_pos,
        "rvol": sig["volume"] / vol_med if vol_med > 0 else 0.0,
        "lower_wick": (min(sig["open"], sig["close"]) - sig["low"]) / rng if rng > 0 else 0.0,
        "taker": sig["taker_buy"] / sig["volume"] if sig["volume"] > 0 else 0.5,
        "stop": sig["low"] * (1 - 2 * base / 100),                 # 신호봉 저가 - 2 기준 ATR
        "target": sig["low"] + 0.5 * (ref_high - sig["low"]),       # 이동폭의 50% 되돌림
        "funding": funding,
    }
    # BTC 동반 여부: 같은 시각 봉이 있을 때만. 베타 보정한 BTC 하락폭이 ETC 하락폭의 몇 배인지.
    if btc_bars and btc_bars[-1]["open_time"] == sig["open_time"] and len(btc_bars) > CRASH_LOOKBACK:
        btc_high = max(b["high"] for b in btc_bars[-1 - CRASH_LOOKBACK:-1])
        btc_drop = (btc_bars[-1]["close"] / btc_high - 1) * 100
        share = btc_drop * CRASH_BETA_BTC / drop
        out.update(btc_drop=btc_drop, btc_share=share,
                   btc_label="BTC 동반" if share >= 0.6 else ("ETC 단독" if share < 0.25 else "부분 동반"))
    return out


# -- 알림 ----------------------------------------------------------------------

def fmt_price(x: float) -> str:
    return f"{x:,.1f}" if x >= 1000 else (f"{x:.3f}" if x >= 1 else f"{x:.5f}")


def build_signal(symbol: str, r: dict) -> Signal:
    when = datetime.fromtimestamp(r["open_time"] / 1000, tz=timezone.utc).astimezone(KST)
    lines = [
        f"{fmt_price(r['close'])} ({when:%m-%d %H:%M} KST 봉) · 4시간 고점 {fmt_price(r['ref_high'])} 대비 "
        f"{r['drop']:+.2f}% (기준ATR {r['mult']:.1f}배)",
        f"RSI14 {r['rsi']:.1f} · RVOL {r['rvol']:.1f}배 · 종가위치 {r['close_pos']:.2f} · "
        f"아래꼬리 {r['lower_wick'] * 100:.0f}% · 테이커 매수비 {r['taker']:.2f}",
    ]
    if "btc_drop" in r:
        lines.append(f"BTC 같은 구간 {r['btc_drop']:+.2f}% → {r['btc_label']} (기여 {r['btc_share']:.2f})")
    if r.get("funding") is not None:
        lines.append(f"펀딩 {r['funding'] * 100:+.4f}%/8h")
    lines.append(f"참고: 손절 {fmt_price(r['stop'])} (신호봉 저가 -2 기준ATR) · "
                 f"1차 목표 {fmt_price(r['target'])} (50% 되돌림)")
    return Signal("CRASH_BUY", "🔵 급락 매수 후보", f"{symbol} 5분봉", "\n".join(lines), symbol)


class CrashWorker:
    """심볼별로 새 완성봉이 생길 때마다 한 번 판정한다. 같은 심볼은 CRASH_COOLDOWN_MIN 동안 한 번만 알린다."""

    def __init__(self, symbols: list, notifier, fetch_bars=fetch_klines, fetch_fund=fetch_funding):
        self.symbols = list(symbols)
        self.notify = notifier
        self.fetch_bars = fetch_bars
        self.fetch_fund = fetch_fund
        self.last_bar = {}          # symbol -> 마지막으로 판정한 완성봉 open_time
        self.last_alert = {}        # symbol -> 마지막 알림 시각

    def poll_once(self, now: datetime = None) -> list:
        """한 사이클. 보낸 Signal 목록을 돌려준다 (테스트용)."""
        now = now or datetime.now(timezone.utc)
        sent = []
        btc = self.fetch_bars("BTCUSDT")
        for symbol in self.symbols:
            bars = btc if symbol == "BTCUSDT" else self.fetch_bars(symbol)
            if not bars or bars[-1]["open_time"] == self.last_bar.get(symbol):
                continue
            self.last_bar[symbol] = bars[-1]["open_time"]
            result = evaluate(bars, None if symbol == "BTCUSDT" else btc)
            if result is None:
                continue
            last = self.last_alert.get(symbol)
            if last and now - last < timedelta(minutes=CRASH_COOLDOWN_MIN):
                log.info("%s 급락 조건 충족했지만 쿨다운 중 (마지막 알림 %s)", symbol, last.isoformat(timespec="minutes"))
                continue
            result["funding"] = self.fetch_fund(symbol)
            signal = build_signal(symbol, result)
            self.notify.send(signal)
            self.last_alert[symbol] = now
            sent.append(signal)
        return sent

    def run(self):
        while True:
            try:
                self.poll_once()
            except Exception as e:      # 네트워크·파싱 오류는 다음 사이클에 다시 시도한다
                log.warning("사이클 오류: %s", e)
            time.sleep(BINANCE_POLL_SEC)
