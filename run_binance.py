"""Binance 선물 급락 매수 알림 워커 진입점 — 5분봉 완성마다 판정.

실행:  python run_binance.py
감시 심볼은 .env 의 ALERT_BINANCE_SYMBOLS (쉼표, 기본 ETCUSDT). 토스 엔진과 독립적으로 돈다.
"""

import logging

from alertbot import db
from alertbot.binance_crash import CrashWorker, fetch_klines
from alertbot.config import (BINANCE_LOG_PATH, BINANCE_SYMBOLS, CRASH_ATR_MULT, CRASH_CLOSE_POS_MIN,
                             CRASH_LOOKBACK, CRASH_RSI_MAX, setup_logging)
from alertbot.models import Signal
from alertbot.notify import build_channels
from alertbot.notify.dispatcher import Dispatcher

log = logging.getLogger("binance")


def main():
    setup_logging(BINANCE_LOG_PATH)
    store = db.connect()            # 신호 이력은 토스 엔진과 같은 alert_signal_log 에 남긴다
    for symbol in BINANCE_SYMBOLS:  # 심볼이 틀리면 여기서 멈춘다 — 매 사이클 400 오류를 내며 돌지 않는다
        try:
            bars = fetch_klines(symbol)
        except Exception as e:
            raise SystemExit(f"Binance 5분봉 조회 실패 ({symbol}): {e}") from e
        log.info("%s 완성봉 %d개 확보 (마지막 종가 %s)", symbol, len(bars), bars[-1]["close"] if bars else "-")
    notifier = Dispatcher(build_channels(), record=lambda s, r: db.log_signal(store, s, r))
    notifier.send(Signal("SYSTEM", "⚪ 시스템", "Binance 감시 시작",
                         f"{', '.join(BINANCE_SYMBOLS)} 5분봉 · 급락 ≥ 기준ATR×{CRASH_ATR_MULT:g} (직전 {CRASH_LOOKBACK}봉 고점 대비) "
                         f"· RSI14 ≤ {CRASH_RSI_MAX:g} · 종가위치 ≥ {CRASH_CLOSE_POS_MIN:g}"))
    CrashWorker(BINANCE_SYMBOLS, notifier).run()


if __name__ == "__main__":
    main()
