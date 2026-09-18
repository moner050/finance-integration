"""전략 랩(tools/backtest/lab.py) — 합성 봉으로 셋업 진입 시각·가격, 청산 경로, 필터 플래그를 확인한다."""
from datetime import datetime, timedelta

import pytest

from tests.conftest import TZ, bar
from tools.backtest import lab


def day_bars(date: str, closes: list, volumes: list, start="09:00", market="KR"):
    """1분봉: 시가 = 직전 종가, 고가 = 종가 +0.05, 저가 = 종가 −0.15 (종가가 봉 상단 → 강봉)."""
    y, m, d = map(int, date.split("-")); hh, mm = map(int, start.split(":"))
    t0 = datetime(y, m, d, hh, mm, tzinfo=TZ[market])
    return [bar(t0 + timedelta(minutes=i), c, v, high=c + 0.05, low=c - 0.15, open_=closes[i - 1] if i else c, market=market)
            for i, (c, v) in enumerate(zip(closes, volumes))]


def daily_rows(dates=None, closes=None, opens=None, vols=None):
    """일봉 행. dates 를 안 주면 2026-04-01 부터 평일 25일, 종가 closes(기본 100) — avg_vol20·MA20 이 채워지게."""
    if dates is None:
        dates, day = [], datetime(2026, 4, 1)
        while len(dates) < 25:
            if day.weekday() < 5:
                dates.append(day.strftime("%Y-%m-%d"))
            day += timedelta(days=1)
        closes = closes or [100.0] * 25
    out = []
    for i, d in enumerate(dates):
        c = closes[i]; o = (opens or closes)[i]; v = (vols or [1_000_000] * len(dates))[i]
        out.append({"timestamp": f"{d}T00:00:00+09:00", "openPrice": o, "highPrice": max(o, c) * 1.01, "lowPrice": min(o, c) * 0.99,
                    "closePrice": c, "volume": v})
    return out


def flat_session(date, n=390, close=100.0, vol=1000):
    return day_bars(date, [close] * n, [vol] * n)


def make_sym(sessions: dict, daily=None, market="KR"):
    raw = [b for bars in sessions.values() for b in bars]
    return lab.Sym("TEST", raw=raw, daily=daily or [], market=market)


# 프로파일(같은 시각 직전 세션 중앙값)을 만들 조용한 3세션 + 시험 세션
QUIET = ["2026-05-04", "2026-05-05", "2026-05-06"]


def test_rvol_breakout_enters_next_bar_after_surge_above_vwap():
    closes = [100.0] * 60 + [100.6] + [100.7] * 329
    vols = [1000] * 60 + [3000] + [1000] * 329
    s = make_sym({d: flat_session(d) for d in QUIET} | {"2026-05-07": day_bars("2026-05-07", closes, vols)})
    ents = lab.setup_rvol_breakout(s, "2026-05-07")
    assert len(ents) == 1
    e = ents[0]
    a, _ = s.days["2026-05-07"]
    assert e["sig"] == a + 60 and e["i"] == a + 61 and e["entry"] == 100.6 and e["rvol"] == 3.0     # 다음 봉 시가(= 신호봉 종가)에 진입
    assert e["sig_low"] == pytest.approx(100.45) and e["mfo"] == 61 and e["watch"] is False


def test_orb_enters_on_first_close_above_opening_range_high():
    closes = [100.0 + (i % 3) * 0.1 for i in range(30)] + [100.1] * 20 + [100.5] + [100.6] * 19 + [100.8] + [100.9] * 319   # 30분 고점 100.25
    vols = [1000] * 50 + [2000] + [1000] * 19 + [2000] + [1000] * 319
    s = make_sym({d: flat_session(d) for d in QUIET} | {"2026-05-07": day_bars("2026-05-07", closes, vols)})
    ents = lab.setup_orb(s, "2026-05-07", 30)
    a, _ = s.days["2026-05-07"]
    assert len(ents) == 1 and ents[0]["sig"] == a + 50 and ents[0]["entry"] == 100.5 and ents[0]["orh"] == pytest.approx(100.25)
    e60 = lab.setup_orb(s, "2026-05-07", 60)
    assert e60 and e60[0]["sig"] == a + 70 and e60[0]["orh"] == pytest.approx(100.65)          # 60분 고점 100.65 를 넘는 첫 봉


def test_pullback_waits_for_vwap_touch_then_reclaim_bar():
    # 급등봉(11번째) → 눌림(VWAP 근처) → 직전 봉 고점을 넘는 양봉
    closes = [100.0] * 11 + [101.0] + [100.9, 100.7, 100.4, 100.2, 100.15] + [100.5] + [100.6] * 371
    vols = [1000] * 11 + [4000] + [1000] * 378
    s = make_sym({d: flat_session(d) for d in QUIET} | {"2026-05-07": day_bars("2026-05-07", closes, vols)})
    ents = lab.setup_pullback(s, "2026-05-07")
    a, _ = s.days["2026-05-07"]
    assert len(ents) == 1 and ents[0]["surge"] == a + 11 and ents[0]["sig"] == a + 17 and ents[0]["entry"] == 100.5


def test_close_bet_requires_strong_close_near_high_with_volume():
    closes = [100.0] * 100 + [102.5] * 290
    s = make_sym({d: flat_session(d) for d in QUIET} | {"2026-05-07": day_bars("2026-05-07", closes, [5000] * 390)}, daily=daily_rows())
    ctx = s.ctx("2026-05-07")
    ents = lab.setup_close_bet(s, "2026-05-07", ctx)
    assert len(ents) == 1 and ents[0]["close_entry"] and ents[0]["entry"] == 102.5
    weak = make_sym({d: flat_session(d) for d in QUIET} | {"2026-05-07": day_bars("2026-05-07", closes, [500] * 390)}, daily=daily_rows())
    assert lab.setup_close_bet(weak, "2026-05-07", weak.ctx("2026-05-07")) == []        # 거래량 부족


def test_gap_ep_needs_gap_volume_and_orh_break():
    closes = [106.0] * 30 + [106.2] * 10 + [106.8] + [107.0] * 349
    s = make_sym({d: flat_session(d) for d in QUIET} | {"2026-05-07": day_bars("2026-05-07", closes, [40000] * 390)}, daily=daily_rows())
    ents = lab.setup_gap_ep(s, "2026-05-07", s.ctx("2026-05-07"))
    a, _ = s.days["2026-05-07"]
    assert len(ents) == 1 and ents[0]["gap"] == pytest.approx(6.0) and ents[0]["sig"] == a + 30 and ents[0]["entry"] == 106.2   # 30분 고점 106.05 를 첫 봉이 넘는다
    small = make_sym({d: flat_session(d) for d in QUIET} | {"2026-05-07": day_bars("2026-05-07", [102.0] * 390, [40000] * 390)}, daily=daily_rows())
    assert lab.setup_gap_ep(small, "2026-05-07", small.ctx("2026-05-07")) == []          # 갭 2% 는 기준(KR 3%) 미달


def test_simulate_stop_target_partial_and_carry():
    # 진입 100 (i=a+61). 이후 105 까지 오르고 마감 104, 다음날 시가 103 → 종가 101
    d1 = [100.0] * 61 + [100.0, 101.0, 103.0, 105.0] + [104.0] * 325
    d2 = [103.0] * 200 + [101.0] * 190
    s = make_sym({"2026-05-07": day_bars("2026-05-07", d1, [1000] * 390), "2026-05-08": day_bars("2026-05-08", d2, [1000] * 390)})
    a, _ = s.days["2026-05-07"]
    e = {"entry": 100.0, "i": a + 61, "sig": a + 60, "sig_low": 99.9, "day_low": 99.0, "date": "2026-05-07", "close_entry": False}
    assert lab.simulate(s, e, dict(stop="day_low", target="none", hold="day")) == (pytest.approx(4.0), "close")
    assert lab.simulate(s, e, dict(stop="day_low", target="r2", hold="day")) == (pytest.approx(2.0), "target")      # R=1 → 목표 102
    assert lab.simulate(s, e, dict(stop="day_low", target="pct1", hold="day")) == (pytest.approx(1.0), "target")
    r, why = lab.simulate(s, e, dict(stop="day_low", target="none", hold="d1", carry="always"))
    assert why == "close" and r == pytest.approx(1.0)                                     # 다음날 종가 101
    r, why = lab.simulate(s, e, dict(stop="day_low", target="none", hold="d1", carry="near_high"))
    assert why == "close" and r == pytest.approx(4.0)                                     # 마감 104 는 고점 105 −0.5% 밖 → 당일 종가
    r, why = lab.simulate(s, e, dict(stop="day_low", target="none", hold="d3", partial=True, carry="always"))
    assert r == pytest.approx(0.5 * 2.0 + 0.5 * 1.0) and why == "close"                  # 2R 에 절반, 나머지 다음날 종가
    assert lab.simulate(s, e, dict(stop="pct5", target="none", hold="next_open")) == (pytest.approx(3.0), "next_open")
    # 손절: 당일 저가 99 를 뚫는 날
    d3 = [100.0] * 61 + [100.0, 99.5, 98.0] + [98.5] * 326
    s2 = make_sym({"2026-05-07": day_bars("2026-05-07", d3, [1000] * 390)})
    e2 = dict(e, i=s2.days["2026-05-07"][0] + 61)
    assert lab.simulate(s2, e2, dict(stop="day_low", target="none", hold="day")) == (pytest.approx(-1.0), "stop")
    r, why = lab.simulate(s2, e2, dict(stop="sig_low", target="none", hold="day", close_stop=True))
    assert why == "close_stop" and r == pytest.approx(-0.5)                                  # 종가 99.5 < 99.9 → 다음 봉 시가 99.5


def test_filter_flags_trend_extension_regime_and_chase():
    ctx = {"prev_close": 100.0, "prev_open": 99.0, "prev_ret": 1.0, "ma20": 95.0, "ma50": 90.0, "ma100": 80.0,
           "ema10": 99.0, "ema20": 97.0, "adr5": 1.0, "adr20": 2.0, "ret1m": 5.0, "ret5": 2.0}
    idx = {"prev_close": 100.0, "ema20": 98.0, "ret1m": 3.0}
    e = {"entry": 100.5, "mfo": 45, "rvol": 5.0, "session_open": 100.2}
    f = lab.filter_flags(e, ctx, idx, 80.0)
    assert f["trend2"] and f["trend3"] and f["ext10_3"] and f["ext20_5"] and f["contract"] and f["rs_top30"] and f["rs_pos"]
    assert f["regime"] and f["tw60"] and f["tw240"] and f["rv_mid"] and not f["rv_lo"] and f["chase_ok"]
    e2 = dict(e, entry=104.0, session_open=101.5)                                          # 갭 +1.5% → 추격
    assert not lab.filter_flags(e2, ctx, idx, 80.0)["chase_ok"]
    assert not lab.filter_flags(e, {**ctx, "ma20": 101.0}, idx, 80.0)["trend2"]


def test_metrics_net_of_cost():
    m = lab.metrics([1.0, 1.0, -0.5, 0.35], cost=0.35)
    assert m["n"] == 4 and m["wr"] == 50.0 and m["avg"] == pytest.approx((0.65 + 0.65 - 0.85 + 0.0) / 4, abs=1e-3)
