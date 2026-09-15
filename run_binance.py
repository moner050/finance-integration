"""Binance 선물 알림 워커 진입점 — 5분봉 급락 매수 + 상위 봉 추종 알림(급등 추종 롱·급락 추종 숏)을 한 프로세스에서.

실행:  python run_binance.py
감시 심볼은 .env 의 ALERT_BINANCE_SYMBOLS(5분봉 급락 매수, 기본 ETCUSDT) 와 추종 사양별 키
(ALERT_BINANCE_SURGE_SYMBOLS 4시간봉 BTCUSDT · ALERT_BINANCE_SURGE_1D_SYMBOLS 일봉 BTCUSDT ·
ALERT_BINANCE_CRASHFOLLOW_1D_SYMBOLS 일봉 ETCUSDT). 토스 엔진과 독립적으로 돈다. 공개 REST 라 Binance API 키는 필요 없다.
ALERT_BINANCE_TRADE_MODE=dry 면 진입 후보를 가상 체결하는 자동매매(alertbot/binance_trade.py)도 같이 돈다 — 역시 키 불필요.
"""

import logging
import time

from alertbot import db
from alertbot.binance_crash import CrashWorker, fetch_klines
from alertbot.binance_follow import BAR_HOURS, FollowWorker, stop_text
from alertbot.binance_trade import DryTrader
from alertbot.config import (BINANCE_LOG_PATH, BINANCE_POLL_SEC, BINANCE_SYMBOLS, BINANCE_TRADE_CAPITAL,
                             BINANCE_TRADE_LEVERAGE, BINANCE_TRADE_MODE, CRASH_ATR_MULT,
                             CRASH_CLOSE_POS_MIN, CRASH_HOLD_HOURS, CRASH_LOOKBACK, CRASH_RSI_MAX, CRASH_STOP_PCT,
                             FOLLOW_KLINES, FOLLOW_SPECS, setup_logging)
from alertbot.models import Signal
from alertbot.notify import build_channels
from alertbot.notify.dispatcher import Dispatcher

log = logging.getLogger("binance")


def check_symbols():
    """심볼이 틀리면 기동 때 멈춘다 — 매 사이클 400 오류를 내며 돌지 않는다."""
    plan = [(s, "5m", None) for s in BINANCE_SYMBOLS]
    for spec in FOLLOW_SPECS:
        for s in spec["symbols"]:
            plan.append((s, spec["interval"], FOLLOW_KLINES))
            if spec["interval"] != "1d":
                plan.append((s, "1d", FOLLOW_KLINES))
    for symbol, interval, limit in dict.fromkeys(plan):
        try:
            bars = fetch_klines(symbol, interval, limit) if limit else fetch_klines(symbol)
        except Exception as e:
            raise SystemExit(f"Binance {interval} 봉 조회 실패 ({symbol}): {e}") from e
        log.info("%s %s 완성봉 %d개 확보 (마지막 종가 %s)", symbol, interval, len(bars), bars[-1]["close"] if bars else "-")


def describe(spec: dict) -> str:
    long = spec["side"] == "long"
    return (f"{spec['name']} {spec['label']} {', '.join(spec['symbols'])}: {spec['lookback']}봉 {'저점' if long else '고점'} 대비 "
            f"≥ 기준ATR×{spec['atr_mult']:g} · RSI14 {'≥' if long else '≤'} {spec['rsi']:g} · "
            f"{'눌림 뒤 EMA9 재돌파' if long else '반등 뒤 EMA9 재이탈'} · 일봉 EMA200 {'위' if spec['regime'] == 'bull' else '아래'} · "
            f"손절 참고 {stop_text(spec)} · 보유 {spec['hold_bars'] * BAR_HOURS[spec['interval']] / 24:g}일")


def run(workers: list, trader=None):
    while True:
        for w in workers:
            try:
                w.poll_once()
            except Exception as e:      # 네트워크·파싱 오류는 다음 사이클에 다시 시도한다
                log.warning("%s 사이클 오류: %s", type(w).__name__, e)
        if trader is not None:
            try:
                trader.poll()           # 열린 가상 포지션의 손절·보유 한도·펀딩
            except Exception as e:
                log.warning("자동매매 감시 오류: %s", e)
        time.sleep(BINANCE_POLL_SEC)


def main():
    setup_logging(BINANCE_LOG_PATH)
    store = db.connect()            # 신호 이력은 토스 엔진과 같은 alert_signal_log 에 남긴다
    check_symbols()
    notifier = Dispatcher(build_channels(), record=lambda s, r: db.log_signal(store, s, r))
    body = [f"급락 매수 5분봉 {', '.join(BINANCE_SYMBOLS)}: 하락 ≥ 기준ATR×{CRASH_ATR_MULT:g} (직전 {CRASH_LOOKBACK}봉 고점 대비) "
            f"· RSI14 ≤ {CRASH_RSI_MAX:g} · 종가위치 ≥ {CRASH_CLOSE_POS_MIN:g} · 손절 참고 종가 -{CRASH_STOP_PCT:g}% · "
            f"보유 {CRASH_HOLD_HOURS}시간"] + [describe(s) for s in FOLLOW_SPECS]
    trader = DryTrader(store, notifier) if BINANCE_TRADE_MODE == "dry" else None
    if trader is not None:
        body.append(f"자동매매 dry (가상 체결): 전략별 자본 {BINANCE_TRADE_CAPITAL:,.0f} USDT · 유효 배율 "
                    + " · ".join(f"{k} {v:g}배" for k, v in BINANCE_TRADE_LEVERAGE.items()))
    notifier.send(Signal("SYSTEM", "⚪ 시스템", "Binance 감시 시작", "\n".join(body)))
    run([CrashWorker(BINANCE_SYMBOLS, notifier, trader=trader)]
        + [FollowWorker(spec, notifier, trader=trader) for spec in FOLLOW_SPECS], trader)


if __name__ == "__main__":
    main()
