"""Binance 추종 알림 — 급등 추종 롱(4시간봉·일봉)과 급락 추종 숏(일봉)의 관찰·재돌파/재이탈 판정, 국면 게이트, 워커 쿨다운."""
from datetime import datetime, timedelta, timezone

from alertbot.binance_follow import FollowWorker, build_signal, evaluate, regime
from alertbot.config import FOLLOW_SPECS

SPEC_4H, SPEC_1D_LONG, SPEC_1D_SHORT = FOLLOW_SPECS
T0 = 1_780_000_000_000
H4 = 4 * 3_600_000
DAY = 24 * 3_600_000


def bar(i: int, o: float, h: float, l: float, c: float, vol: float = 1000.0, step: int = H4) -> dict:
    return {"open_time": T0 + i * step, "open": o, "high": h, "low": l, "close": c, "volume": vol,
            "close_time": T0 + i * step + step - 1, "taker_buy": vol / 2}


def quiet(n: int = 400, price: float = 70000.0, rng: float = 300.0, step: int = H4) -> list:
    """±rng 범위의 조용한 봉 n개."""
    out = []
    for i in range(n):
        c = price + ((i * 7) % 5 - 2) * rng / 5
        out.append(bar(i, c, c + rng, c - rng, c, step=step))
    return out


def path(bars: list, closes: list, highs: list = None, lows: list = None, step: int = H4, vol: float = 2000.0) -> list:
    """주어진 종가 경로를 이어 붙인다. 고가/저가를 안 주면 직전 종가와 이번 종가로 만든다."""
    out = list(bars)
    for j, c in enumerate(closes):
        prev = out[-1]["close"]
        h = highs[j] if highs else max(prev, c) + abs(c - prev) * 0.1
        l = lows[j] if lows else min(prev, c) - abs(c - prev) * 0.1
        out.append(bar(len(out), prev, h, l, c, vol=vol, step=step))
    return out


def surge_4h() -> tuple:
    """조용한 400봉 → 15봉 +10% 급등 → 눌림 → 재돌파. (bars, 급등 시작 idx)"""
    bars = quiet()
    n0 = len(bars)
    bars = path(bars, [70000 * (1 + 0.10 * (j + 1) / 15) for j in range(15)])
    bars = path(bars, [76400, 75600, 75000], lows=[76200, 75400, 74800])          # 눌림: EMA9(≈75,400) 아래로
    bars = path(bars, [77200], highs=[77400], lows=[74900])                        # 재돌파
    return bars, n0


def daily_long() -> list:
    """BTC 일봉: 조용한 400일(±2%) → 10일 +20% → 2일 눌림 → 재돌파."""
    bars = quiet(price=70000, rng=1400, step=DAY)
    bars = path(bars, [70000 + 1400 * (j + 1) for j in range(10)], step=DAY)      # → 84,000
    bars = path(bars, [78500, 78000], lows=[78000, 77500], step=DAY)              # EMA9(≈79,000) 아래
    return path(bars, [80500], highs=[80800], lows=[78200], step=DAY)


def daily_short() -> list:
    """ETC 일봉: 조용한 400일(±2%) → 10일 -30% 급락 → 2일 반등(EMA9 위) → 재이탈."""
    bars = quiet(price=20.0, rng=0.4, step=DAY)
    bars = path(bars, [20.0 - 0.6 * (j + 1) for j in range(10)], step=DAY)        # → 14.0
    bars = path(bars, [16.5, 16.8], highs=[16.7, 17.0], step=DAY)                 # EMA9(≈16.1) 위로 반등
    return path(bars, [15.5], highs=[16.9], lows=[15.3], step=DAY)                # 재이탈


def test_quiet_no_signal():
    assert evaluate(quiet(), SPEC_4H) is None
    assert evaluate(quiet(step=DAY), SPEC_1D_LONG) is None
    assert evaluate(quiet(step=DAY), SPEC_1D_SHORT) is None


def test_4h_watch_fires_once_then_reentry():
    bars, n0 = surge_4h()
    stages = [(i, evaluate(bars[:i + 1], SPEC_4H)) for i in range(n0, n0 + 15)]
    watches = [(i, r) for i, r in stages if r and r["stage"] == "watch"]
    assert len(watches) == 1
    i, r = watches[0]
    assert r["mult"] >= SPEC_4H["atr_mult"] and r["rsi"] >= 70 and r["ago"] == 0 and r["stop"] is None
    assert "눌림 뒤 EMA9 재돌파를 10봉 안에 기다린다" in build_signal("BTCUSDT", r, SPEC_4H).text()
    assert all(res is None for j, res in stages if j > i)                          # 급등이 이어지는 동안은 다시 알리지 않는다
    assert all(evaluate(bars[:k], SPEC_4H) is None for k in range(len(bars) - 3, len(bars)))   # 눌림 중 무신호
    r = evaluate(bars, SPEC_4H)
    assert r["stage"] == "entry" and r["ago"] == 2 and r["pull"] == 74800 and r["close"] > r["ema9"]
    assert abs(r["stop"] - r["pull"] * (1 - 2.5 * r["base"] / 100)) < 1e-6 and r["stop"] < r["pull"]
    text = build_signal("BTCUSDT", r, SPEC_4H).text()
    assert text.startswith("🔵 추종 매수 후보 | BTCUSDT 4시간봉\n현재가 ") and "손절 " in text and "· 7일 보유" in text and "기준ATR" not in text


def test_daily_long_reentry_uses_own_bars_for_regime():
    bars = daily_long()
    r = evaluate(bars, SPEC_1D_LONG)
    assert r["stage"] == "entry" and r["side"] == "long" and r["ago"] == 3 and r["pull"] == 77500
    assert r["bull"] is True and r["mult"] >= 4
    assert abs(r["stop"] - r["close"] * 0.9) < 1e-9
    s = build_signal("BTCUSDT", r, SPEC_1D_LONG)
    assert s.kind == "SURGE_ENTRY_1D" and s.label == "BTCUSDT 일봉" and "(-10%) · 20일 보유" in s.text()
    watch = [k for k in range(400, 410) if (x := evaluate(bars[:k + 1], SPEC_1D_LONG)) and x["stage"] == "watch"]
    assert len(watch) == 1


def test_daily_short_reexit_after_bounce():
    bars = daily_short()
    assert all(evaluate(bars[:k], SPEC_1D_SHORT) is None for k in (len(bars) - 2, len(bars) - 1))   # 반등 중 무신호
    r = evaluate(bars, SPEC_1D_SHORT, funding=-0.0005)
    assert r["stage"] == "entry" and r["side"] == "short" and r["ago"] == 3 and r["pull"] == 17.0
    assert r["bull"] is False and abs(r["stop"] - r["close"] * 1.25) < 1e-9      # 재이탈 봉의 RSI 는 조건이 아니다 (급락 봉이 조건)
    s = build_signal("ETCUSDT", r, SPEC_1D_SHORT)
    assert s.kind == "CRASH_SHORT_1D" and s.severity == "action"
    body = s.text()
    assert s.title == "🔴 추종 숏 후보" and "반등 고점" in body and "재이탈" in body and "약세" in body and "펀딩 과밀" in body
    assert "(+25%) · 20일 보유" in body
    watch = [k for k in range(400, 410) if (x := evaluate(bars[:k + 1], SPEC_1D_SHORT)) and x["stage"] == "watch"]
    assert len(watch) == 1 and build_signal("ETCUSDT", evaluate(bars[:watch[0] + 1], SPEC_1D_SHORT), SPEC_1D_SHORT).kind == "CRASH_WATCH_1D"


def test_regime_helper():
    assert regime(quiet(150, step=DAY)) is None
    bull, close, ema = regime(daily_long())
    assert bull and close > ema


class Recorder:
    def __init__(self):
        self.sent = []

    def send(self, signal, force=False):
        self.sent.append(signal)
        return {"test": "ok"}


def test_worker_gates_on_regime_and_cooldown():
    bars, n0 = surge_4h()
    watch_i = next(i for i in range(n0, n0 + 15) if evaluate(bars[:i + 1], SPEC_4H))
    data = {"4h": bars[:watch_i + 1], "1d": quiet(price=120000, rng=1000, step=DAY)}    # 일봉이 EMA200 아래(약세)로 흘러내린 상태
    data["1d"] = path(data["1d"], [120000 - 200 * j for j in range(1, 60)], step=DAY)
    rec = Recorder()
    w = FollowWorker(SPEC_4H, rec, fetch_bars=lambda s, iv, n: data[iv], fetch_fund=lambda s: None)
    t = datetime(2026, 9, 15, tzinfo=timezone.utc)
    assert w.poll_once(t) == []                                   # 약세 국면 → 보류
    data["1d"] = daily_long()
    w.last_bar.clear()
    assert [s.kind for s in w.poll_once(t)] == ["SURGE_WATCH"]
    data["4h"] = bars
    assert [s.kind for s in w.poll_once(t + timedelta(hours=16))] == ["SURGE_ENTRY"]
    assert w.poll_once(t + timedelta(hours=20)) == []             # 같은 봉은 다시 판정하지 않는다
    data["4h"] = bars[:watch_i + 1]
    w.last_bar.clear()
    assert w.poll_once(t + timedelta(days=3)) == []               # 7일(42봉) 쿨다운
    w.last_bar.clear()
    assert [s.kind for s in w.poll_once(t + timedelta(days=8))] == ["SURGE_WATCH"]
    assert len(rec.sent) == 3
    # 일봉 숏 사양: 국면은 자기 봉으로 재고, 약세라 통과한다
    ws = FollowWorker(SPEC_1D_SHORT, rec, fetch_bars=lambda s, iv, n: daily_short(), fetch_fund=lambda s: None)
    assert [s.kind for s in ws.poll_once(t)] == ["CRASH_SHORT_1D"]
