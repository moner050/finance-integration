"""지표 골든 테스트.

golden_indicators.json 은 분리 전 scalping_alert.py 원본으로 만든 값이다.
모듈 분리(Phase 1)가 동작을 바꾸지 않았음을 이 파일이 보증한다.
"""
import json
from datetime import datetime
from pathlib import Path

from alertbot import indicators as I
from tests.conftest import TZ, bar, kr_fixture, make_candles

GOLDEN = json.loads(Path(__file__).with_name("golden_indicators.json").read_text(encoding="utf-8"))


def _compute():
    cur, hist = kr_fixture()
    closes = [float(c["closePrice"]) for c in cur]
    profile = I.build_volume_profile(hist + cur, "KR", exclude_session="2026-03-25")
    return {
        "ema9": I.compute_ema(closes, 9), "ema20": I.compute_ema(closes, 20), "ema50": I.compute_ema(closes, 50),
        "ema_alignment": I.ema_alignment(cur),
        "rsi_cutler": list(I.compute_rsi(cur)),
        "atr_pct": I.compute_atr_pct(cur), "band": I.effective_band(cur),
        "profile_buckets": len(profile), "profile_0930": profile.get("09:30"),
        "rvol_ma": list(I.compute_rvol(cur, "KR", None)),
        "rvol_profile": list(I.compute_rvol(cur, "KR", profile)),
        "rvol_at_40_profile": I.rvol_at(cur, 40, "KR", profile),
        "rvol_at_40_ma": I.rvol_at(cur, 40, "KR", None),
        "rvol_at_10": I.rvol_at(cur, 10, "KR", None),
        "peak_profile": I.session_peak_rvol(cur, "KR", profile),
        "peak_ma": I.session_peak_rvol(cur, "KR", None),
        "vwap": I.compute_vwap(cur, "KR"),
        "vwap_partial_50": I.compute_vwap(cur[:50], "KR"),
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


def test_indicators_match_original_golden():
    got = _compute()
    assert set(got) == set(GOLDEN)
    for key in GOLDEN:
        assert got[key] == GOLDEN[key], key
