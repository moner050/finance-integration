"""Binance 자동매매 dry 확인 — 실제 신호를 기다리지 않고, 과거 신호봉을 워커에 먹여 알림 → 진입 → 감시 → 종료 기록을 실제 경로로 돌린다.

실행:  python check_binance_dry.py crash "2026-09-14 07:15"          (KST, 신호봉 시작 시각)
       python check_binance_dry.py surge_4h "2026-09-05 21:00" --quiet   (텔레그램 대신 콘솔 출력)
사양: crash(ETC 5분봉 급락 매수) · surge_4h(BTC 4시간봉 급등 추종) · surge_1d(BTC 일봉 급등 추종) · crash_1d(ETC 일봉 급락 숏)
봉은 Binance 에서 그 시각까지 받아 오고, 체결 시세는 현재 값이다. 진입 뒤 강제 종료해 📤 까지 확인하고 확인용 포지션 행은 지운다.
ALERT_BINANCE_TRADE_MODE 와 무관하게 항상 dry 트레이더를 쓴다 (실제 주문은 내지 않는다).
"""

import argparse
import time
from datetime import datetime, timezone

import requests

from alertbot import db
from alertbot.binance_crash import KST, CrashWorker, fetch_funding, parse_klines
from alertbot.binance_follow import FollowWorker
from alertbot.binance_trade import Trader
from alertbot.config import BINANCE_FAPI, FOLLOW_SPECS, setup_logging
from alertbot.notify import build_channels
from alertbot.notify.dispatcher import Dispatcher

SPECS = {"surge_4h": FOLLOW_SPECS[0], "surge_1d": FOLLOW_SPECS[1], "crash_1d": FOLLOW_SPECS[2]}
MS = {"5m": 300_000, "4h": 14_400_000, "1d": 86_400_000}


def bars_until(symbol, interval, open_ms, limit):
    """신호봉(open_ms 에 시작)까지의 완성봉 limit 개."""
    r = requests.get(f"{BINANCE_FAPI}/fapi/v1/klines", params={"symbol": symbol, "interval": interval, "limit": limit,
                                                              "endTime": open_ms + MS[interval] - 1}, timeout=15)
    r.raise_for_status()
    bars = parse_klines(r.json(), open_ms + MS[interval])
    if not bars or bars[-1]["open_time"] != open_ms:
        raise SystemExit(f"{symbol} {interval} 봉이 {datetime.fromtimestamp(open_ms / 1000, KST)} 에 없다 (봉 시작 시각인지 확인)")
    return bars


class Console:
    def send(self, signal, force=False):
        print(f"--- {signal.text()}")
        return {"console": "ok"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("strategy", choices=["crash", *SPECS])
    ap.add_argument("when", help='신호봉 시작 시각 KST "YYYY-MM-DD HH:MM"')
    ap.add_argument("--quiet", action="store_true", help="텔레그램 대신 콘솔에 찍는다")
    a = ap.parse_args()
    setup_logging()
    open_ms = int(datetime.strptime(a.when, "%Y-%m-%d %H:%M").replace(tzinfo=KST).timestamp() * 1000)
    store = db.connect()
    notifier = Console() if a.quiet else Dispatcher(build_channels(), record=lambda s, r: db.log_signal(store, s, r))
    trader = Trader(store, notifier, "dry")
    if a.strategy == "crash":
        feed = {"ETCUSDT": bars_until("ETCUSDT", "5m", open_ms, 1000), "BTCUSDT": bars_until("BTCUSDT", "5m", open_ms, 1000)}
        h4_open = (open_ms + MS["5m"]) // MS["4h"] * MS["4h"] - MS["4h"]                  # 신호봉 종료 전 마지막 완성 4시간봉
        h4 = bars_until("ETCUSDT", "4h", h4_open, 120)
        worker, symbol = CrashWorker(["ETCUSDT"], notifier, fetch_bars=lambda s: feed[s], fetch_fund=fetch_funding, trader=trader,
                                     fetch_h4=lambda s: h4), "ETCUSDT"
    else:
        spec = SPECS[a.strategy]
        symbol = spec["symbols"][0]
        feed = {spec["interval"]: bars_until(symbol, spec["interval"], open_ms, 400)}
        if spec["interval"] != "1d":
            day_open = (open_ms + MS[spec["interval"]]) // MS["1d"] * MS["1d"] - MS["1d"]     # 신호봉 종료 전 마지막 완성 일봉
            feed["1d"] = bars_until(symbol, "1d", day_open, 400)
        worker = FollowWorker(spec, notifier, fetch_bars=lambda s, iv, n: feed[iv], fetch_fund=fetch_funding, trader=trader)
    sent = worker.poll_once()
    print("알림:", [s.kind for s in sent] or "없음 — 신호 조건이 아니거나 4시간봉 상승 배열로 보류됐다 (로그 참고)")
    rows = [r for r in db.binance_positions(store, status="open") if r["symbol"] == symbol and r["mode"] == "dry"]
    if not rows:
        print("dry 진입 없음 (알림이 관찰 단계이거나 한도·중복에 걸렸다 — 위 보류 알림 참고)")
        return
    p = rows[-1]
    print("진입:", {k: p[k] for k in ("id", "strategy", "side", "qty", "entry_price", "notional", "leverage", "stop", "deadline")})
    print("감시 1회 종료 목록:", trader.poll())
    closed = trader._record_close(p, trader.fetch_price(symbol), "manual", datetime.now(timezone.utc))
    print("강제 종료:", {k: closed[k] for k in ("exit_price", "exit_reason", "pnl")})
    store.execute("DELETE FROM alert_binance_positions WHERE id = %s", (p["id"],))
    print("확인용 포지션 행 삭제 완료")
    store.close()


if __name__ == "__main__":
    main()
