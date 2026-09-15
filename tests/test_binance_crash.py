"""Binance 5분봉 급락 매수 알림 — 판정·파싱·워커 쿨다운."""
from datetime import datetime, timedelta, timezone

from alertbot.binance_crash import CrashWorker, base_atr_pct, build_signal, evaluate, h4_regime, parse_klines
from alertbot.config import CRASH_ATR_MULT, CRASH_LOOKBACK

T0 = 1_789_000_000_000          # 임의의 5분봉 open_time (ms)
STEP = 300_000


def make_bars(n: int = 1000, close: float = 7.5, crash_bars: int = 0, crash_to: float = 6.9,
              reversal: bool = True, volume: float = 1000.0) -> list:
    """조용한 봉 n개 뒤에 crash_bars 봉 동안 crash_to 까지 내리는 급락을 붙인다.

    마지막 봉은 reversal 이면 저가에서 튀어 오른 망치형(종가 위치 0.77), 아니면 저가 마감이다.
    """
    bars = []
    for i in range(n - crash_bars):
        c = close + ((i * 7) % 5 - 2) * 0.004           # ±0.008 결정적 잡음
        bars.append({"open_time": T0 + i * STEP, "open": c, "high": c + 0.01, "low": c - 0.01, "close": c,
                     "volume": volume, "close_time": T0 + i * STEP + STEP - 1, "taker_buy": volume / 2})
    start = len(bars)
    for j in range(crash_bars):
        c = close + (crash_to - close) * (j + 1) / crash_bars
        prev = bars[-1]["close"]
        bars.append({"open_time": T0 + (start + j) * STEP, "open": prev, "high": prev, "low": c, "close": c,
                     "volume": volume * 3, "close_time": T0 + (start + j) * STEP + STEP - 1, "taker_buy": volume})
    if crash_bars:
        last = bars[-1]
        last["low"] = crash_to - 0.05
        last["volume"] = volume * 8
        last["taker_buy"] = volume * 2.4
        if reversal:
            last["high"], last["close"] = crash_to + 0.08, crash_to + 0.05     # (0.10)/(0.13) = 0.77
        else:
            last["high"], last["close"] = crash_to + 0.02, crash_to - 0.05
    return bars


def make_h4(down: bool = True, n: int = 120) -> list:
    """4시간봉 n개. down 이면 꾸준히 내려 EMA9 < EMA21, 아니면 올라 EMA9 > EMA21."""
    step, out = 14_400_000, []
    for i in range(n):
        c = 8.0 - 0.01 * i if down else 7.0 + 0.01 * i
        out.append({"open_time": T0 - (n - i) * step, "open": c, "high": c + 0.02, "low": c - 0.02, "close": c,
                    "volume": 100.0, "close_time": T0 - (n - i) * step + step - 1, "taker_buy": 50.0})
    return out


def test_parse_klines_drops_in_progress_bar():
    rows = [[T0, "7.5", "7.6", "7.4", "7.55", "100", T0 + STEP - 1, "0", 10, "40", "0", "0"],
            [T0 + STEP, "7.55", "7.6", "7.5", "7.52", "50", T0 + 2 * STEP - 1, "0", 5, "20", "0", "0"]]
    bars = parse_klines(rows, now_ms=T0 + STEP + 1000)       # 두 번째 봉은 진행 중
    assert len(bars) == 1 and bars[0]["close"] == 7.55 and bars[0]["taker_buy"] == 40.0


def test_quiet_market_no_signal():
    assert evaluate(make_bars()) is None


def test_crash_with_reversal_bar_signals():
    bars = make_bars(crash_bars=20)
    r = evaluate(bars)
    assert r is not None
    assert r["mult"] >= CRASH_ATR_MULT and r["rsi"] <= 30 and r["close_pos"] >= 0.6
    assert r["drop"] < -7                                     # 7.5 → 6.95 는 -7.3%
    assert r["ref_high"] == max(b["high"] for b in bars[-1 - CRASH_LOOKBACK:-1])
    assert r["rvol"] > 5 and abs(r["stop"] - r["close"] * 0.97) < 1e-9 and r["low"] < r["retrace50"] < r["ref_high"]
    assert 0 < base_atr_pct(bars) < 1


def test_crash_without_reversal_bar_is_ignored():
    assert evaluate(make_bars(crash_bars=20, reversal=False)) is None


def test_btc_context_labels_co_movement():
    etc = make_bars(crash_bars=20)
    btc_together = make_bars(close=78000, crash_bars=20, crash_to=73000)      # -6.4% 동반
    r = evaluate(etc, btc_together)
    assert r["btc_label"] == "BTC 동반" and r["btc_drop"] < -5
    btc_quiet = make_bars(close=78000)
    assert evaluate(etc, btc_quiet)["btc_label"] == "ETC 단독"
    # 시각이 안 맞는 BTC 봉은 무시한다
    assert "btc_drop" not in evaluate(etc, btc_together[:-1])


def test_signal_text_contains_key_numbers():
    r = evaluate(make_bars(crash_bars=20), None, funding=-0.0001)
    s = build_signal("ETCUSDT", r)
    assert s.kind == "CRASH_BUY" and s.severity == "action" and s.symbol == "ETCUSDT"
    body = s.text()
    assert "기준ATR" in body and "RSI14" in body and "펀딩 -0.0100%/8h" in body and "손절" in body
    assert "(종가 -3%)" in body and "보유 한도 8시간" in body and "목표 지정가 없음" in body and "50% 되돌림선" in body


class Recorder:
    def __init__(self):
        self.sent = []

    def send(self, signal, force=False):
        self.sent.append(signal)
        return {"test": "ok"}


def test_worker_evaluates_each_bar_once_and_respects_cooldown():
    crash = make_bars(crash_bars=20)
    fetched = {"ETCUSDT": crash, "BTCUSDT": make_bars(close=78000)}
    rec = Recorder()
    w = CrashWorker(["ETCUSDT"], rec, fetch_bars=lambda s: fetched[s], fetch_fund=lambda s: None, fetch_h4=lambda s: make_h4())
    t = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    assert len(w.poll_once(t)) == 1 and "하락 배열" in rec.sent[0].body
    assert w.poll_once(t + timedelta(minutes=5)) == []           # 같은 완성봉은 다시 판정하지 않는다
    # 5분 뒤 새 봉도 급락 조건이면 쿨다운(60분) 안이라 보내지 않는다
    nxt = crash + [dict(crash[-1], open_time=crash[-1]["open_time"] + STEP, close_time=crash[-1]["close_time"] + STEP)]
    fetched["ETCUSDT"] = nxt
    assert w.poll_once(t + timedelta(minutes=5)) == []
    fetched["ETCUSDT"] = nxt + [dict(nxt[-1], open_time=nxt[-1]["open_time"] + STEP, close_time=nxt[-1]["close_time"] + STEP)]
    assert len(w.poll_once(t + timedelta(minutes=61))) == 1
    assert len(rec.sent) == 2 and all(s.kind == "CRASH_BUY" for s in rec.sent)


def test_h4_regime_gate_holds_alert_in_uptrend():
    assert h4_regime(make_h4(n=30)) is None                                   # 봉 부족 → 불명
    reg = h4_regime(make_h4(down=False))
    assert reg["down"] is False and reg["ema9"] > reg["ema21"] and h4_regime(make_h4())["down"] is True
    fetched = {"ETCUSDT": make_bars(crash_bars=20), "BTCUSDT": make_bars(close=78000)}
    rec, calls = Recorder(), []

    class Stub:
        def on_entry(self, *a):
            calls.append(a)
    w = CrashWorker(["ETCUSDT"], rec, fetch_bars=lambda s: fetched[s], fetch_fund=lambda s: None,
                    fetch_h4=lambda s: make_h4(down=False), trader=Stub())
    t = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    assert w.poll_once(t) == [] and rec.sent == [] and calls == []            # 상승 배열 → 알림·자동매매 모두 보류
    w.last_bar.clear()
    w.fetch_h4 = lambda s: make_h4(down=True)
    sent = w.poll_once(t)
    assert len(sent) == 1 and "하락 배열" in sent[0].body and calls[0][4] == 8   # 보유 한도 8시간을 넘긴다
    w.last_bar.clear()
    w.last_alert.clear()

    def boom(s):
        raise RuntimeError("timeout")
    w.fetch_h4 = boom
    sent = w.poll_once(t + timedelta(hours=2))
    assert len(sent) == 1 and "배열 불명" in sent[0].body                      # 조회 실패면 알리되 본문에 표기
