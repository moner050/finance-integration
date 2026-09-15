"""Binance 선물 알림 워커 진입점 — 5분봉 급락 매수(ETC) + 4시간봉 급등 추종(BTC) 을 한 프로세스에서.

실행:  python run_binance.py
감시 심볼은 .env 의 ALERT_BINANCE_SYMBOLS(급락, 기본 ETCUSDT) / ALERT_BINANCE_SURGE_SYMBOLS(급등, 기본 BTCUSDT).
토스 엔진과 독립적으로 돈다. 공개 REST 라 Binance API 키는 필요 없다.
"""

import logging
import time

from alertbot import db
from alertbot.binance_crash import CrashWorker, fetch_klines
from alertbot.binance_surge import SurgeWorker
from alertbot.config import (BINANCE_LOG_PATH, BINANCE_POLL_SEC, BINANCE_SYMBOLS, CRASH_ATR_MULT,
                             CRASH_CLOSE_POS_MIN, CRASH_LOOKBACK, CRASH_RSI_MAX, SURGE_ATR_MULT,
                             SURGE_DAILY_KLINES, SURGE_INTERVAL, SURGE_KLINES, SURGE_LOOKBACK, SURGE_RSI_MIN,
                             SURGE_SYMBOLS, setup_logging)
from alertbot.models import Signal
from alertbot.notify import build_channels
from alertbot.notify.dispatcher import Dispatcher

log = logging.getLogger("binance")


def check_symbols():
    """심볼이 틀리면 기동 때 멈춘다 — 매 사이클 400 오류를 내며 돌지 않는다."""
    plan = [(s, "5m", None) for s in BINANCE_SYMBOLS]
    plan += [(s, iv, n) for s in SURGE_SYMBOLS for iv, n in ((SURGE_INTERVAL, SURGE_KLINES), ("1d", SURGE_DAILY_KLINES))]
    for symbol, interval, limit in plan:
        try:
            bars = fetch_klines(symbol, interval, limit) if limit else fetch_klines(symbol)
        except Exception as e:
            raise SystemExit(f"Binance {interval} 봉 조회 실패 ({symbol}): {e}") from e
        log.info("%s %s 완성봉 %d개 확보 (마지막 종가 %s)", symbol, interval, len(bars), bars[-1]["close"] if bars else "-")


def run(workers: list):
    while True:
        for w in workers:
            try:
                w.poll_once()
            except Exception as e:      # 네트워크·파싱 오류는 다음 사이클에 다시 시도한다
                log.warning("%s 사이클 오류: %s", type(w).__name__, e)
        time.sleep(BINANCE_POLL_SEC)


def main():
    setup_logging(BINANCE_LOG_PATH)
    store = db.connect()            # 신호 이력은 토스 엔진과 같은 alert_signal_log 에 남긴다
    check_symbols()
    notifier = Dispatcher(build_channels(), record=lambda s, r: db.log_signal(store, s, r))
    notifier.send(Signal("SYSTEM", "⚪ 시스템", "Binance 감시 시작",
                         f"급락 매수 5분봉 {', '.join(BINANCE_SYMBOLS)}: 하락 ≥ 기준ATR×{CRASH_ATR_MULT:g} "
                         f"(직전 {CRASH_LOOKBACK}봉 고점 대비) · RSI14 ≤ {CRASH_RSI_MAX:g} · 종가위치 ≥ {CRASH_CLOSE_POS_MIN:g}\n"
                         f"급등 추종 4시간봉 {', '.join(SURGE_SYMBOLS)}: 상승 ≥ 기준ATR×{SURGE_ATR_MULT:g} "
                         f"(직전 {SURGE_LOOKBACK}봉 저점 대비) · RSI14 ≥ {SURGE_RSI_MIN:g} · 눌림 뒤 EMA9 재돌파 · 일봉 EMA200 위"))
    run([CrashWorker(BINANCE_SYMBOLS, notifier), SurgeWorker(SURGE_SYMBOLS, notifier)])


if __name__ == "__main__":
    main()
