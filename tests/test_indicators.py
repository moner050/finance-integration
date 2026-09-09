"""지표 테스트.

golden_indicators.json 은 분리 전 scalping_alert.py 원본으로 만든 값이다. 동작을 바꾸지
않은 함수는 그 값과 같아야 한다. 감지기 수정(P0-1 세션 누적, P0-2 정규장 기준선,
P2-2 Wilder RSI, P2-3 True Range)은 손으로 계산한 기대값으로 따로 검증한다.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

from alertbot import indicators as I
from tests.conftest import TZ, bar, kr_fixture, make_candles

GOLDEN = json.loads(Path(__file__).with_name("golden_indicators.json").read_text(encoding="utf-8"))


def test_unchanged_functions_match_original_golden():
    cur, hist = kr_fixture()
    closes = [float(c["closePrice"]) for c in cur]
    profile = I.build_volume_profile(hist + cur, "KR", exclude_session="2026-03-25")
    got = {
        "ema9": I.compute_ema(closes, 9), "ema20": I.compute_ema(closes, 20), "ema50": I.compute_ema(closes, 50),
        "ema_alignment": I.ema_alignment(cur),
        "profile_buckets": len(profile), "profile_0930": profile.get("09:30"),
        "rvol_ma": list(I.compute_rvol(cur, "KR", None)),
        "rvol_profile": list(I.compute_rvol(cur, "KR", profile)),
        "rvol_at_40_profile": I.rvol_at(cur, 40, "KR", profile)[0],
        "rvol_at_40_ma": I.rvol_at(cur, 40, "KR", None)[0],
        "rvol_at_10": I.rvol_at(cur, 10, "KR", None)[0],
        "bucket_first": list(I._bucket(cur[0], "KR")),
        "bucket_us": list(I._bucket(make_candles("US", "2026-03-25", "09:30", 1)[0], "US")),
        "strong": [I.strong_bar(bar(datetime(2026, 3, 25, 9, 0, tzinfo=TZ["KR"]), c, 1, high=h, low=l))
                   for c, h, l in ((100.0, 101.0, 99.0), (100.6, 101.0, 99.0), (99.4, 101.0, 99.0), (100.0, 100.0, 100.0))],
        "vwap_pos": [I.vwap_position(p, 100.0, b) for p, b in ((100.2, 0.15), (99.8, 0.15), (100.1, 0.15), (100.4, 0.5), (0, 0.15))],
        "is_regular": [I._is_regular(t, "KR") for t in ("08:59", "09:00", "15:29", "15:30")]
                      + [I._is_regular(t, "US") for t in ("09:29", "09:30", "15:59", "16:00")],
        "minutes_from_open": [I._minutes_from_open("09:05", "KR"), I._minutes_from_open("09:35", "US"),
                              I._minutes_from_open("08:50", "KR")],
    }
    for key, value in got.items():
        assert value == GOLDEN[key], key
    # 세션 누적값도 정규장 봉만 있는 픽스처에서는 원본의 창 계산과 같아야 한다
    ss = I.SessionState("KR")
    ss.update(cur, profile)
    assert (ss.vwap, ss.peak) == (GOLDEN["vwap"], GOLDEN["peak_profile"])
    ss_ma = I.SessionState("KR")
    ss_ma.update(cur, None)
    assert ss_ma.peak == GOLDEN["peak_ma"]
    part = I.SessionState("KR")
    part.update(cur[:50])
    assert part.vwap == GOLDEN["vwap_partial_50"]


# --- P0-2 정규장 기준선 -------------------------------------------------------

def us_with_premarket():
    """09:00~09:29 프리마켓 30봉(거래량 10) + 09:30~09:54 정규장 25봉(거래량 1000, 마지막 2000)."""
    pre = make_candles("US", "2026-03-25", "09:00", 30, volume=10)
    reg = make_candles("US", "2026-03-25", "09:30", 25, volume=1000)
    reg[-1]["volume"] = "2000"
    return pre + reg


def test_rvol_ma_uses_only_regular_bars():
    candles = us_with_premarket()
    assert I.rvol_at(candles, 54, "US") == (2.0, "이동평균")      # 직전 정규장 20봉 평균 1000
    assert I.rvol_at(candles, 40, "US") == (0.0, "부족")          # 정규장 봉이 10개뿐
    prev, cur, method = I.compute_rvol(candles, "US")
    assert (prev, cur, method) == (1.0, 2.0, "이동평균")
    assert I.compute_rvol(candles[:45], "US")[2] == "부족"


def test_compute_rvol_mixed_method():
    cur, hist = kr_fixture()
    profile = I.build_volume_profile(hist, "KR")
    profile.pop("10:09")                 # 현재봉 시각의 표본만 없앤다 → 이동평균으로 떨어짐
    prev, now, method = I.compute_rvol(cur, "KR", profile)
    assert method == "혼합"
    assert I.compute_rvol(cur[:1], "KR")[2] == "부족"


# --- P0-1 세션 누적 -------------------------------------------------------------

def test_session_state_incremental_equals_one_shot():
    cur, hist = kr_fixture()
    profile = I.build_volume_profile(hist, "KR")
    one = I.SessionState("KR")
    one.update(cur, profile)
    inc = I.SessionState("KR")
    window = 30
    for end in range(window, len(cur) + 1):
        inc.update(cur[end - window:end], profile)     # 30봉 창이 흘러가도 정점(40번째 봉)은 남는다
    assert (inc.vwap, inc.peak, inc.session) == (one.vwap, one.peak, "2026-03-25")
    assert inc.peak == 6.0
    assert inc.update(cur[-window:], profile) == 0     # 같은 봉은 두 번 넣지 않는다


def test_session_state_vwap_hand_computed_and_regular_only():
    t0 = datetime(2026, 3, 25, 9, 29, tzinfo=TZ["US"])
    candles = [bar(t0, 500.0, 999, high=600, low=400, market="US")]          # 프리마켓 — 무시
    candles += [bar(t0 + timedelta(minutes=1), 100, 10, high=101, low=99, market="US"),
                bar(t0 + timedelta(minutes=2), 101, 20, high=102, low=100, market="US"),
                bar(t0 + timedelta(minutes=3), 102, 30, high=103, low=101, market="US")]
    ss = I.SessionState("US")
    assert ss.update(candles) == 3
    assert ss.vwap == round((100 * 10 + 101 * 20 + 102 * 30) / 60, 4)       # 101.3333
    # 다음 세션 봉이 오면 누적을 새로 시작한다
    nxt = make_candles("US", "2026-03-26", "09:30", 2, volume=5, close=50)
    ss.update(nxt)
    assert ss.session == "2026-03-26" and ss.vwap == 50.0 and ss.peak == 0.0
    # 창에 남은 전 세션 봉은 무시한다
    assert ss.update(candles) == 0


def test_session_state_roundtrip():
    cur, _ = kr_fixture()
    ss = I.SessionState("KR")
    ss.update(cur)
    back = I.SessionState.from_dict("KR", json.loads(json.dumps(ss.to_dict())))
    assert (back.vwap, back.peak, back.session, back.last_dt) == (ss.vwap, ss.peak, ss.session, ss.last_dt)
    assert back.update(cur) == 0


# --- P2-2 Wilder RSI / P2-3 True Range -----------------------------------------

def _wilder_reference(closes, period=14):
    diffs = [b - a for a, b in zip(closes, closes[1:])]
    g = [max(d, 0) for d in diffs]
    l = [max(-d, 0) for d in diffs]
    ag, al = sum(g[:period]) / period, sum(l[:period]) / period
    out = [100.0 if al == 0 else round(100 - 100 / (1 + ag / al), 1)]
    for i in range(period, len(diffs)):
        ag = (ag * (period - 1) + g[i]) / period
        al = (al * (period - 1) + l[i]) / period
        out.append(100.0 if al == 0 else round(100 - 100 / (1 + ag / al), 1))
    return out[-2], out[-1]


def test_rsi_wilder():
    up = make_candles("KR", "2026-03-25", "09:00", 20, step=0.5)
    assert I.compute_rsi(up) == (100.0, 100.0)
    down = make_candles("KR", "2026-03-25", "09:00", 20, close=120, step=-0.5)
    assert I.compute_rsi(down) == (0.0, 0.0)
    cur, _ = kr_fixture()
    closes = [float(c["closePrice"]) for c in cur]
    assert I.compute_rsi(cur) == _wilder_reference(closes)
    assert I.compute_rsi(cur[:15]) == (0.0, 0.0)          # period+2 미만


def test_atr_true_range_counts_gaps():
    # 봉마다 1.0 씩 갭업. 고저폭 1.0 이지만 전봉 종가 대비 고가는 1.5 → TR 1.5
    candles = make_candles("KR", "2026-03-25", "09:00", 21, close=100, step=1.0)
    expect = round(sum(1.5 / (100 + i) * 100 for i in range(1, 21)) / 20, 3)
    assert I.compute_atr_pct(candles) == expect
    assert I.compute_atr_pct(candles[:20]) == 0.0         # n+1 봉 필요
    assert I.effective_band(candles) == max(0.15, round(expect * 0.5, 3))
