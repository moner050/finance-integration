"""Binance 선물 알림 워커 진입점 — 5분봉 급락 매수 + 상위 봉 추종 알림(급등 추종 롱·급락 추종 숏)을 한 프로세스에서.

실행:  python run_binance.py
감시 심볼은 .env 의 ALERT_BINANCE_SYMBOLS(5분봉 급락 매수, 기본 ETCUSDT) 와 추종 사양별 키
(ALERT_BINANCE_SURGE_SYMBOLS 4시간봉 BTCUSDT · ALERT_BINANCE_SURGE_1D_SYMBOLS 일봉 BTCUSDT ·
ALERT_BINANCE_CRASHFOLLOW_1D_SYMBOLS 일봉 ETCUSDT). 토스 엔진과 독립적으로 돈다. 공개 REST 라 Binance API 키는 필요 없다.
ALERT_BINANCE_TRADE_MODE=dry 면 진입 후보를 가상 체결하는 자동매매(alertbot/binance_trade.py)도 같이 돈다 — 역시 키 불필요.
live 면 .env ALERT_BINANCE_API_KEY/SECRET 로 실제 주문을 낸다 — 기동 때 헤지 모드·격리·배율을 맞추고, DB 킬 스위치가 켜져야 진입한다.
"""

import logging
import time
from datetime import datetime, timedelta, timezone

from alertbot import db
from alertbot.binance_book import SignalBook
from alertbot.binance_crash import KST, CrashWorker, fetch_klines
from alertbot.binance_follow import FollowWorker
from alertbot.binance_broker import BinanceFutures, BrokerError
from alertbot.binance_summary import summary_signal
from alertbot.binance_trade import Trader
from alertbot.config import (BINANCE_LOG_PATH, BINANCE_POLL_SEC, BINANCE_SIGNAL_TRADE_FILE, BINANCE_SYMBOLS,
                             BINANCE_TRADE_CAPITAL, BINANCE_API_KEY, BINANCE_API_SECRET, BINANCE_TRADE_EXCHANGE_LEV,
                             BINANCE_TRADE_MODE, CRASH_H4_KLINES, DATA_DIR, FOLLOW_KLINES, FOLLOW_SPECS,
                             SUMMARY_INTERVAL_MIN, setup_logging)
from alertbot.models import Signal
from alertbot.notify import build_channels
from alertbot.notify.dispatcher import Dispatcher
from alertbot.tracking import SignalTradeLog

log = logging.getLogger("binance")


def check_symbols():
    """심볼이 틀리면 기동 때 멈춘다 — 매 사이클 400 오류를 내며 돌지 않는다."""
    plan = [(s, "5m", None) for s in BINANCE_SYMBOLS] + [(s, "4h", CRASH_H4_KLINES) for s in BINANCE_SYMBOLS]
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


def watch_list() -> list:
    """시작 알림용 — 전략별 감시 심볼. 판정 조건은 코드·README 에 있으니 알림에는 싣지 않는다."""
    return [f"급락 매수 5분봉: {', '.join(BINANCE_SYMBOLS)}"] + \
           [f"{spec['name']} {spec['label']}: {', '.join(spec['symbols'])}" for spec in FOLLOW_SPECS]


def live_broker():
    """live 사전 조건 — 키, 서버 시각, 심볼 필터, 헤지 모드·격리·배율. 하나라도 안 되면 기동을 멈춘다."""
    if not (BINANCE_API_KEY and BINANCE_API_SECRET):
        raise SystemExit("live 모드에는 .env ALERT_BINANCE_API_KEY / ALERT_BINANCE_API_SECRET 이 필요하다 (선물 거래 권한만, 출금 권한 없이)")
    symbols = sorted({*BINANCE_SYMBOLS, *(s for spec in FOLLOW_SPECS for s in spec["symbols"])})
    broker = BinanceFutures(BINANCE_API_KEY, BINANCE_API_SECRET)
    try:
        broker.sync_time()
        broker.load_filters(symbols)
        broker.setup(symbols, BINANCE_TRADE_EXCHANGE_LEV)
        log.info("Binance live 준비: %s 격리 %d배 헤지 모드 · 가용 %.2f USDT", ", ".join(symbols), BINANCE_TRADE_EXCHANGE_LEV, broker.balance())
    except BrokerError as e:
        raise SystemExit(f"Binance live 준비 실패: {e}") from e
    return broker


def run(workers: list, trader=None, notifier=None, book=None):
    # 기동 직후 첫 시황이 바로 나가도록 과거 시각으로 시작한다 — 한 주기를 기다리면 '돌고 있는 건지' 확인이 늦다
    last_summary = datetime.now(timezone.utc) - timedelta(minutes=SUMMARY_INTERVAL_MIN)
    last_day = datetime.now(timezone.utc).astimezone(KST).date()      # 날짜(KST)가 바뀌면 지난 하루의 신호 성적표
    while True:
        for w in workers:
            try:
                w.poll_once()
            except Exception as e:      # 네트워크·파싱 오류는 다음 사이클에 다시 시도한다
                log.warning("%s 사이클 오류: %s", type(w).__name__, e)
        if book is not None:
            try:
                book.poll()             # 독자의 신호 포지션 — 손절선·보유 한도 청산 알림
            except Exception as e:
                log.warning("신호 포지션 감시 오류: %s", e)
        if trader is not None:
            try:
                trader.poll()           # 열린 가상 포지션의 손절·보유 한도·펀딩
            except Exception as e:
                log.warning("자동매매 감시 오류: %s", e)
        now = datetime.now(timezone.utc)
        today = now.astimezone(KST).date()
        if today != last_day:
            if book is not None and notifier is not None:
                try:
                    report = book.daily_report(last_day.isoformat())
                    if report:
                        notifier.send(Signal("SIGNAL_REPORT", "📈 오늘 코인 신호 성적", f"{last_day:%m-%d}", report))
                except Exception as e:
                    log.warning("코인 신호 성적표 오류: %s", e)
            last_day = today
        if notifier is not None and now - last_summary >= timedelta(minutes=SUMMARY_INTERVAL_MIN):
            last_summary = now
            try:
                signal = summary_signal(workers, trader, now)
                if signal is not None:
                    notifier.send(signal)
            except Exception as e:
                log.warning("시황 요약 오류: %s", e)
        time.sleep(BINANCE_POLL_SEC)


def main():
    setup_logging(BINANCE_LOG_PATH)
    store = db.connect()            # 신호 이력은 토스 엔진과 같은 alert_signal_log 에 남긴다
    check_symbols()
    notifier = Dispatcher(build_channels(), record=lambda s, r: db.log_signal(store, s, r))
    body = watch_list()
    trader = None
    if BINANCE_TRADE_MODE == "off":
        body.append("자동매매: off")
    else:
        broker = live_broker() if BINANCE_TRADE_MODE == "live" else None
        trader = Trader(store, notifier, BINANCE_TRADE_MODE, broker)
        if broker is None:
            body.append(f"자동매매: dry (가상 체결, 전략별 자본 {BINANCE_TRADE_CAPITAL:,.0f} USDT)")
        else:
            on = db.get_settings(store)["binance_trade_enabled"] == "1"
            body.append(f"자동매매: LIVE (전략별 자본 {BINANCE_TRADE_CAPITAL:,.0f} USDT · 격리 {BINANCE_TRADE_EXCHANGE_LEV}배 · "
                        f"가용 {broker.balance():,.0f} USDT · 킬 스위치 {'ON' if on else 'OFF'})")
    # 진입 후보 뒤의 손절·보유 한도 청산 알림과 모의 성적(자정 KST 성적표). 재시작 전의 신호 포지션도 이어받는다
    book = SignalBook(store, notifier, trades=SignalTradeLog(DATA_DIR / BINANCE_SIGNAL_TRADE_FILE))
    if book.open:
        body.append("진행 중 신호 포지션: " + ", ".join(f"{p['symbol']} {p['name']}" for p in book.open.values()))
    body.append(f"시황 요약 {SUMMARY_INTERVAL_MIN}분마다")
    notifier.send(Signal("SYSTEM", "⚪ 시스템", "Binance 감시 시작", "\n".join(body)))
    run([CrashWorker(BINANCE_SYMBOLS, notifier, trader=trader, book=book)]
        + [FollowWorker(spec, notifier, trader=trader, book=book) for spec in FOLLOW_SPECS], trader, notifier, book)


if __name__ == "__main__":
    main()
