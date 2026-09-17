"""Binance 선물 알림 워커 진입점 — 5분봉 급락 매수 + 상위 봉 추종 알림(급등 추종 롱·급락 추종 숏) + 거래대금 상위 30 코인 급변 감시를 한 프로세스에서.

실행:  python run_binance.py   (보통은 python run.py 가 엔진·Binance 워커·백오피스를 함께 띄우고 지킨다)
감시 심볼은 .env 의 ALERT_BINANCE_SYMBOLS(5분봉 급락 매수, 기본 ETCUSDT) 와 추종 사양별 키
(ALERT_BINANCE_SURGE_SYMBOLS 4시간봉 BTCUSDT · ALERT_BINANCE_SURGE_1D_SYMBOLS 일봉 BTCUSDT ·
ALERT_BINANCE_CRASHFOLLOW_1D_SYMBOLS 일봉 ETCUSDT). 토스 엔진과 독립적으로 돈다. 시세는 공개 REST 라 키가 필요 없다.
공용 가상 장부(alertbot/binance_trade.py dry)는 모드와 무관하게 늘 돈다 — 진입 후보를 가상 체결해 실계좌와 격리된 기록을 쌓고 공용 채널로 알린다.
ALERT_BINANCE_TRADE_MODE=live 면 백오피스에서 Binance live 스위치를 켠 계정마다 그 계정 키로 실제 주문을 내고 그 계정 텔레그램으로 알린다
(계정·키·스위치 변경은 재시작 없이 반영).
"""

import logging
from datetime import datetime, timedelta, timezone

from alertbot import db, lifecycle
from alertbot.binance_book import SignalBook
from alertbot.binance_crash import KST, CrashWorker, fetch_klines
from alertbot.binance_follow import FollowWorker
from alertbot.binance_scan import ScanWorker, Universe
from alertbot.binance_summary import summary_signal
from alertbot.binance_trade import AccountTraders, Trader, TraderGroup
from alertbot.config import (BINANCE_LOG_PATH, BINANCE_POLL_SEC, BINANCE_SIGNAL_TRADE_FILE, BINANCE_SYMBOLS,
                             BINANCE_TRADE_CAPITAL, BINANCE_TRADE_EXCHANGE_LEV, BINANCE_TRADE_MODE, CRASH_H4_KLINES, DATA_DIR,
                             FOLLOW_KLINES, FOLLOW_SPECS, SCAN_TOP_N, SUMMARY_INTERVAL_MIN, setup_logging)
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
    """기동 로그용 — 전략별 감시 심볼. 판정 조건은 코드·README 에 있으니 싣지 않는다."""
    return [f"급락 매수 5분봉: {', '.join(BINANCE_SYMBOLS)}"] + \
           [f"{spec['name']} {spec['label']}: {', '.join(spec['symbols'])}" for spec in FOLLOW_SPECS] + \
           [f"급변 감시 1시간봉: 거래대금 상위 {SCAN_TOP_N} 코인 (백오피스 '종목' 에서 추가·제외, 관찰 알림)"]


def trade_symbols() -> list:
    return sorted({*BINANCE_SYMBOLS, *(s for spec in FOLLOW_SPECS for s in spec["symbols"])})


def run(workers: list, trader=None, notifier=None, book=None, paper=None, live=None):
    """trader 는 워커와 공유하는 TraderGroup, paper 는 공용 가상 트레이더(시황의 포지션 줄), live 는 계정별 트레이더 묶음(live 모드만)."""
    # 기동 직후 첫 시황이 바로 나가도록 과거 시각으로 시작한다 — 한 주기를 기다리면 '돌고 있는 건지' 확인이 늦다
    last_summary = datetime.now(timezone.utc) - timedelta(minutes=SUMMARY_INTERVAL_MIN)
    last_day = datetime.now(timezone.utc).astimezone(KST).date()      # 날짜(KST)가 바뀌면 지난 하루의 신호 성적표
    while lifecycle.running():
        if live is not None and trader is not None:
            trader.traders = [paper] + live.refresh()       # 백오피스에서 바뀐 계정·키·live 스위치 반영
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
                trader.poll()           # 열린 포지션(가상·계정별)의 손절·보유 한도·펀딩
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
            for t in (live.traders if live is not None else []):
                try:
                    report = t.daily_report(last_day.isoformat())
                    if report:
                        t.notify.send(Signal("DAILY_REPORT", "📈 오늘 코인 성적", f"{last_day:%m-%d}", report, account_id=t.account_id))
                except Exception as e:
                    log.warning("계정 %s 코인 성적표 오류: %s", t.account_id, e)
            last_day = today
        if notifier is not None and now - last_summary >= timedelta(minutes=SUMMARY_INTERVAL_MIN):
            last_summary = now
            try:
                signal = summary_signal(workers, now, paper=paper)
                if signal is not None:
                    notifier.send(signal)
            except Exception as e:
                log.warning("시황 요약 오류: %s", e)
        lifecycle.sleep(BINANCE_POLL_SEC)
    log.info("Binance 감시 종료 — 관리 프로세스의 멈춤 요청")


def main():
    setup_logging(BINANCE_LOG_PATH)
    store = db.connect()            # 신호 이력은 토스 엔진과 같은 alert_signal_log 에 남긴다
    lifecycle.install(heartbeat=lambda: db.touch_service(store, "binance"))     # run.py 가 띄웠으면 멈춤 요청·heartbeat
    check_symbols()
    notifier = Dispatcher(build_channels(), record=lambda s, r: db.log_signal(store, s, r))
    body = watch_list()
    paper = Trader(store, notifier, "dry")          # 공용 가상 장부 — 모드와 무관하게 늘 돈다
    trader = TraderGroup([paper])                   # 워커가 공유한다. live 모드면 사이클마다 계정별 트레이더를 갈아 끼운다
    live = None
    body.append(f"가상매매: 진입 후보 전부 (전략별 자본 {BINANCE_TRADE_CAPITAL:,.0f} USDT)")
    if BINANCE_TRADE_MODE == "live":
        live = AccountTraders(store, trade_symbols(), BINANCE_TRADE_EXCHANGE_LEV)
        trader.traders = [paper] + live.refresh()
        body.append(f"계정별 live: on (격리 {BINANCE_TRADE_EXCHANGE_LEV}배 · 준비된 계정 {len(live.traders)}개)")
    else:
        body.append("계정별 live: off")
    # 진입 후보 뒤의 손절·보유 한도 청산 알림과 모의 성적(자정 KST 성적표). 재시작 전의 신호 포지션도 이어받는다
    book = SignalBook(store, notifier, trades=SignalTradeLog(DATA_DIR / BINANCE_SIGNAL_TRADE_FILE))
    if book.open:
        body.append("진행 중 신호 포지션: " + ", ".join(f"{p['symbol']} {p['name']}" for p in book.open.values()))
    body.append(f"시황 요약 {SUMMARY_INTERVAL_MIN}분마다")
    log.info("Binance 감시 시작\n%s", "\n".join(body))       # 공용 채널로는 보내지 않는다 — 로그(콘솔·파일)에만
    run([CrashWorker(BINANCE_SYMBOLS, notifier, trader=trader, book=book)]
        + [FollowWorker(spec, notifier, trader=trader, book=book) for spec in FOLLOW_SPECS]
        + [ScanWorker(Universe(store), notifier)], trader, notifier, book, paper, live)


if __name__ == "__main__":
    main()
