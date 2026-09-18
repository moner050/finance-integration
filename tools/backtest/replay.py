"""토스 엔진(alertbot.engine.SignalEngine)을 과거 1분봉으로 그대로 재생하는 백테스트 (tools/backtest/replay.py).

- 시각은 가짜 datetime 으로 1분씩 전진 (매 분 :30 초에 한 사이클). 완성봉 = 그 분 이전 봉, 현재가 = 그 분 봉의 시가.
- 보유는 신호 포지션만 (가상 장부 없음, holdings={}) → 확정 매수 신호가 → 확정 청산 신호 시점 가격이 한 건.
- 프로파일·전일 종가·선행 바스켓·쿨다운(Dispatcher)·상태기계 전부 실제 코드.
사용: python tools/backtest/replay.py KR|US [report_since] [종목,종목] [최근N세션]
  환경변수 BT_OVERRIDES(JSON, config 상수 덮어쓰기·ENTRY_WINDOW), BT_TAG(결과 파일 접미). 데이터는 universe.DATA_DIR, 결과는 그 아래 results/
"""
import bisect, json, logging, os, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, r"C:\workspace\personal\finance-integration")
import alertbot.engine as E
import alertbot.market_hours as MH
import alertbot.notify.dispatcher as DP
import alertbot.indicators as IND
from alertbot.notify.dispatcher import Dispatcher
from alertbot.indicators import is_regular_bar
from alertbot.timeutil import parse_ts

from tools.backtest.universe import DATA_DIR, WATCH   # noqa: E402

HERE = DATA_DIR / "results"
HERE.mkdir(parents=True, exist_ok=True)
DATA = DATA_DIR
TZ = {"US": ZoneInfo("America/New_York"), "KR": ZoneInfo("Asia/Seoul")}
OPEN_CLOSE = {"KR": (9 * 60, 15 * 60 + 30), "US": (9 * 60 + 30, 16 * 60)}
MARGIN = 10


SIM = {"now": datetime(2026, 1, 1, tzinfo=timezone.utc)}      # UTC aware


class FakeDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        n = SIM["now"]
        return n.astimezone(tz) if tz else n.replace(tzinfo=None)


def fake_now_local(market):
    return SIM["now"].astimezone(TZ[market])


class HistClient:
    def __init__(self, symbols: dict):
        """symbols: symbol -> market"""
        self.bars, self.epochs, self.daily = {}, {}, {}
        for s, m in symbols.items():
            bars = json.loads((DATA / f"{s}.json").read_text(encoding="utf-8"))
            bars.sort(key=lambda b: b["timestamp"])
            self.bars[s] = bars
            self.epochs[s] = [datetime.fromisoformat(b["timestamp"]).timestamp() for b in bars]
            days = {}
            for b in bars:
                if is_regular_bar(b, m):
                    days[parse_ts(b["timestamp"], m).strftime("%Y-%m-%d")] = b
            self.daily[s] = [{"timestamp": datetime.fromisoformat(f"{d}T15:30:00").replace(tzinfo=TZ[m]).isoformat(),
                              "closePrice": b["closePrice"]} for d, b in sorted(days.items())]
            # 토스 일봉이 있으면 그것을 쓴다 (시가 포함 → 추격 배제 판정). 타임스탬프는 그 세션의 현지 날짜로 맞춘다
            p1d = DATA / f"{s}_1d.json"
            if p1d.exists():
                rows = []
                for c in json.loads(p1d.read_text(encoding="utf-8")):
                    d = parse_ts(c["timestamp"], m).strftime("%Y-%m-%d")
                    rows.append({"timestamp": datetime.fromisoformat(f"{d}T15:30:00").replace(tzinfo=TZ[m]).isoformat(),
                                 "openPrice": c["openPrice"], "closePrice": c["closePrice"]})
                self.daily[s] = sorted(rows, key=lambda r: r["timestamp"])

    def _idx(self, s):
        """현재 분 시작 이전 봉의 끝 인덱스 (봉 timestamp < cur_min)."""
        cur_min = SIM["now"].replace(second=0, microsecond=0).timestamp()
        return bisect.bisect_left(self.epochs[s], cur_min)

    def get_candles(self, symbol, interval="1m", count=120):
        if interval == "1d":
            today = SIM["now"].timestamp()
            return [d for d in self.daily.get(symbol, []) if datetime.fromisoformat(d["timestamp"]).timestamp() <= today][-max(count, 6):]
        i = self._idx(symbol)
        return self.bars[symbol][max(0, i - count):i]

    def get_candles_paged(self, symbol, pages):
        i = self._idx(symbol)
        return self.bars[symbol][max(0, i - pages * 200):i]

    def get_prices(self, symbols):
        out = {}
        for s in symbols:
            if s not in self.bars:
                continue
            i = self._idx(s)
            eps = self.epochs[s]
            cur_min = SIM["now"].replace(second=0, microsecond=0).timestamp()
            if i < len(eps) and eps[i] == cur_min:
                out[s] = float(self.bars[s][i]["openPrice"])
            elif i > 0:
                out[s] = float(self.bars[s][i - 1]["closePrice"])
        return out

    def get_market_calendar(self, market):
        return None

    def session_dates(self, symbols, market):
        days = set()
        for s in symbols:
            for d in self.daily[s]:
                days.add(d["timestamp"][:10])
        return sorted(days)


def run(market: str, report_since: str, only=None, max_days=None):
    logging.basicConfig(level=logging.ERROR)
    logging.getLogger("scalper").setLevel(logging.ERROR)
    watch = {t: c for t, c in WATCH.items() if c["market"] == market and (not only or t in only)}
    leaders = sorted({s for c in watch.values() for s in (c.get("leaders") or [])})
    client = HistClient({**{t: market for t in watch}, **{s: market for s in leaders}})

    OV = json.loads(os.environ.get("BT_OVERRIDES") or "{}")
    TAG = os.environ.get("BT_TAG") or ""
    window = OV.pop("ENTRY_WINDOW", None)          # [개장 후 분 시작, 끝] 밖에서는 새 매수 판단을 하지 않는다
    for k, v in OV.items():
        for mod in (E, IND):
            if hasattr(mod, k):
                setattr(mod, k, v)
    E.datetime, DP.datetime = FakeDatetime, FakeDatetime
    E.now_local, MH.now_local = fake_now_local, fake_now_local
    E.DATA_DIR = HERE / f"out_{market}{TAG}"
    E.DATA_DIR.mkdir(exist_ok=True)

    signals, trades = [], []
    disp = Dispatcher(channels=[], record=None)
    orig_send = disp.send

    def send(signal, force=False):
        r = orig_send(signal, force)
        if r is not None:
            signals.append({"at": SIM["now"].isoformat(), "kind": signal.kind, "title": signal.title,
                            "symbol": signal.symbol, "body": signal.body})
        return r
    disp.send = send
    eng = E.SignalEngine(client, disp, watch, store=None)

    def add_trade(ticker, label, mkt, opened_at, entry, exit_price, pnl, reason, now=None):
        opened = datetime.fromisoformat(opened_at)
        trades.append({"ticker": ticker, "label": label, "market": mkt, "opened_at": opened_at,
                       "closed_at": SIM["now"].isoformat(), "entry": entry, "exit": exit_price, "pnl": pnl,
                       "reason": reason, "hold_min": round((SIM["now"] - opened).total_seconds() / 60, 1)})
    eng.signal_trades.add = add_trade

    o, c = OPEN_CLOSE[market]
    tz = TZ[market]
    dates = client.session_dates(list(watch), market)
    if max_days:
        dates = dates[-max_days:]
    need = {s: market for s in leaders}
    n_eval = 0
    for d in dates:
        y, mo, da = map(int, d.split("-"))
        for hm in range(o - MARGIN, c + MARGIN + 1):
            local = datetime(y, mo, da, hm // 60, hm % 60, 30, tzinfo=tz)
            SIM["now"] = local.astimezone(timezone.utc)
            eng.refresh_volume_profile(list(watch))
            if hasattr(eng, "refresh_daily"):
                eng.refresh_daily(list(watch))
            if need:
                eng.refresh_prev_closes(need)
            prices = client.get_prices(list(watch) + leaders)
            eng._record_prices(prices)
            for t in watch:
                if window and eng.state.get(t, "관망") == "관망" and not (window[0] <= hm - o < window[1]):
                    continue
                eng.evaluate(t, prices, {})
                n_eval += 1
        print(d, "signals", len(signals), "trades", len(trades), flush=True)

    # 아직 열린 신호 포지션은 마지막 가격으로 평가
    prices = client.get_prices(list(watch))
    open_pos = [{"ticker": t, "label": watch[t]["name"], "opened_at": s["at"], "entry": s["price"],
                 "mark": prices.get(t), "pnl": round((prices[t] - s["price"]) / s["price"] * 100, 2) if prices.get(t) else None}
                for t, s in eng.signal_pos.items()]
    out = {"market": market, "dates": dates, "report_since": report_since, "evals": n_eval,
           "signals": signals, "trades": trades, "open": open_pos}
    (HERE / f"result_{market}{TAG}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("DONE", market, "sessions", len(dates), "signals", len(signals), "trades", len(trades), "open", len(open_pos))


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "2026-06-15",
        sys.argv[3].split(",") if len(sys.argv) > 3 else None, int(sys.argv[4]) if len(sys.argv) > 4 else None)
