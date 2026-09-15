"""Binance 4시간봉 급등 추종 알림 — 급등 관찰·눌림 재돌파 판정, 국면 게이트, 워커 쿨다운."""
from datetime import datetime, timedelta, timezone

from alertbot.binance_surge import SurgeWorker, build_signal, evaluate, regime
from alertbot.config import SURGE_ATR_MULT

T0 = 1_780_000_000_000
H4 = 4 * 3_600_000
DAY = 24 * 3_600_000


def bar(i: int, o: float, h: float, l: float, c: float, vol: float = 1000.0, step: int = H4) -> dict:
    return {"open_time": T0 + i * step, "open": o, "high": h, "low": l, "close": c, "volume": vol,
            "close_time": T0 + i * step + step - 1, "taker_buy": vol / 2}


def quiet(n: int = 400, price: float = 70000.0) -> list:
    """±300 범위의 조용한 4시간봉 (ATR ≈ 0.9%)."""
    out = []
    for i in range(n):
        c = price + ((i * 7) % 5 - 2) * 60
        out.append(bar(i, c, c + 300, c - 300, c))
    return out


def surge_then_pullback() -> tuple:
    """조용한 400봉 → 15봉 동안 +10% 급등 → 3봉 눌림(EMA9 아래) → 재돌파 봉. (bars, 급등 시작 idx, 재돌파 idx)"""
    bars = quiet()
    n0 = len(bars)
    for j in range(15):
        prev = bars[-1]["close"]
        c = 70000 * (1 + 0.10 * (j + 1) / 15)
        bars.append(bar(n0 + j, prev, c + 150, prev - 100, c, vol=3000))
    for c in (76400, 75600, 75000):                       # 눌림: EMA9(≈76,300) 아래로
        prev = bars[-1]["close"]
        bars.append(bar(len(bars), prev, prev + 100, c - 200, c, vol=1200))
    prev = bars[-1]["close"]
    bars.append(bar(len(bars), prev, 77400, prev - 100, 77200, vol=2500))   # 재돌파
    return bars, n0, len(bars) - 1


def daily(n: int = 400, up: bool = True) -> list:
    out = []
    for i in range(n):
        c = 40000 + i * 100 if up else 120000 - i * 150
        out.append(bar(i, c, c + 500, c - 500, c, step=DAY))
    return out


def test_quiet_no_signal():
    assert evaluate(quiet()) is None


def test_spike_stage_fires_once_on_first_qualifying_bar():
    bars, n0, _ = surge_then_pullback()
    stages = [(i, evaluate(bars[:i + 1])) for i in range(n0, n0 + 15)]
    spikes = [(i, r) for i, r in stages if r and r["stage"] == "spike"]
    assert len(spikes) == 1
    i, r = spikes[0]
    assert r["mult"] >= SURGE_ATR_MULT and r["rsi"] >= 70 and r["rise"] > 5
    assert r["stop"] < r["close"] < r["target"] and r["spike_ago"] == 0
    assert all(res is None for j, res in stages if j > i)          # 급등이 이어지는 동안은 다시 알리지 않는다


def test_reentry_after_pullback():
    bars, n0, last = surge_then_pullback()
    assert all(evaluate(bars[:i + 1]) is None for i in range(last - 3, last))   # 눌림 중에는 무신호
    r = evaluate(bars)
    assert r is not None and r["stage"] == "reentry"
    assert r["spike_ago"] == 2 and r["pullback_low"] == 74800 and r["close"] > r["ema9"]   # 급등 봉(k=2) 뒤 눌림 1봉


def test_regime_from_daily_bars_and_signal_text():
    bars, _, _ = surge_then_pullback()
    assert regime(daily(150)) is None
    r = evaluate(bars, daily(up=True), funding=0.0005)
    assert r["bull"] is True and r["daily_close"] > r["ema200"]
    s = build_signal("BTCUSDT", r)
    assert s.kind == "SURGE_ENTRY" and s.severity == "action"
    body = s.text()
    assert "재돌파" in body and "강세" in body and "과열" in body and "보유 한도 7일" in body
    r2 = evaluate(bars, daily(up=False))
    assert r2["bull"] is False


class Recorder:
    def __init__(self):
        self.sent = []

    def send(self, signal, force=False):
        self.sent.append(signal)
        return {"test": "ok"}


def test_worker_gates_on_regime_and_cooldown():
    bars, n0, last = surge_then_pullback()
    spike_i = next(i for i in range(n0, n0 + 15) if evaluate(bars[:i + 1]))
    data = {"4h": bars[:spike_i + 1], "1d": daily(up=False)}
    fetch = lambda s, iv, n: data[iv]
    rec = Recorder()
    w = SurgeWorker(["BTCUSDT"], rec, fetch_bars=fetch, fetch_fund=lambda s: None)
    t = datetime(2026, 9, 15, tzinfo=timezone.utc)
    assert w.poll_once(t) == []                                   # 약세 국면 → 보류
    data["1d"] = daily(up=True)
    w.last_bar.clear()
    assert [s.kind for s in w.poll_once(t)] == ["SURGE_WATCH"]
    data["4h"] = bars                                             # 재돌파 봉
    assert [s.kind for s in w.poll_once(t + timedelta(hours=16))] == ["SURGE_ENTRY"]
    # 같은 봉은 다시 판정하지 않고, 새 급등 관찰은 42봉(7일) 안이면 쿨다운
    assert w.poll_once(t + timedelta(hours=20)) == []
    data["4h"] = bars[:spike_i + 1]
    w.last_bar.clear()
    assert w.poll_once(t + timedelta(days=3)) == []
    w.last_bar.clear()
    assert [s.kind for s in w.poll_once(t + timedelta(days=8))] == ["SURGE_WATCH"]
    assert len(rec.sent) == 3
