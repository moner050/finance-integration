"""매크로 점수 — 등급 임계·곡선·시나리오 보정."""
import pytest

from alertbot.macro import scoring as SC

SCEN = [{"code": "S1", "soxx_low": 580, "soxx_high": 650, "base_prob": 25},
        {"code": "S2", "soxx_low": 470, "soxx_high": 560, "base_prob": 35},
        {"code": "S3", "soxx_low": 380, "soxx_high": 450, "base_prob": 25},
        {"code": "S4", "soxx_low": 300, "soxx_high": 380, "base_prob": 15}]


def test_floor_and_move_grades():
    assert SC.floor_grade("ust_10y", 4.74) == 0 and SC.floor_grade("ust_10y", 4.75) == 1 and SC.floor_grade("ust_10y", 5.4) == 3
    assert SC.floor_grade("ust_2y", 5.0) == 2
    assert SC.move_grade("ust_10y", 20) == 2 and SC.move_grade("ust_10y", -20) == 1 and SC.move_grade("jgb_30y", 7) == 0


def test_percentile_alone_caps_at_caution_level():
    values = [1.0 + i * 0.001 for i in range(300)]          # 3년 최고 = 백분위 100, 절대 하한·속도는 없음
    g = SC.grade_series("jgb_2y", values)
    assert g["grade"] == 2 and "3년 최고권" in g["reasons"]


def test_usdjpy_yen_surge_is_danger():
    values = [150.0] * 100 + [147.5]                        # 5일 −1.7%
    g = SC.grade_series("usdjpy", values)
    assert g["grade"] == 3 and any("엔 급강세" in r for r in g["reasons"])
    assert SC.grade_series("usdjpy", [150.0] * 100)["grade"] == 0


def test_curve_and_carry():
    assert SC.curve_state(-0.1, [])["label"] == "역전"
    assert SC.curve_state(0.3, [-0.2, 0.1])["label"] == "역전 해소"
    assert SC.curve_state(0.3, [0.2, 0.3])["label"] == "정상"
    assert SC.carry_grade(2.0) == 1 and SC.carry_grade(1.5) == 3 and SC.carry_grade(2.5) == 0


def test_yoy_needs_same_month_last_year():
    pts = [{"d": "2025-08-01", "v": 100.0}, {"d": "2026-07-01", "v": 102.0}, {"d": "2026-08-01", "v": 102.4}]
    v, month = SC.yoy(pts)
    assert v == pytest.approx(2.4) and month == "2026-08-01"
    assert SC.yoy(pts[1:])[0] is None


def test_adjust_without_signals_keeps_base_and_ev():
    res = SC.adjust(SCEN, {"us_core_cpi": None, "eps_rev": None})
    assert res["adjusted"] == {"S1": 25.0, "S2": 35.0, "S3": 25.0, "S4": 15.0} and res["top"] == "S2"
    assert res["ev"] == pytest.approx(488.75) and res["contributions"] == []


def test_eps_downgrade_shifts_mass_to_s3_s4():
    res = SC.adjust(SCEN, {"eps_rev": SC.signal_eps(-20)})
    p = res["adjusted"]
    assert sum(p.values()) == pytest.approx(100, abs=0.2)
    assert p["S3"] + p["S4"] > 40 and p["S1"] < 25
    assert res["contributions"][0]["key"] == "eps_rev" and res["contributions"][0]["delta"] > 0


def test_signals_are_continuous_between_thresholds():
    """임계를 살짝 넘었다고 확률이 왈칵 쏠리지 않는다."""
    assert SC.signal_us_cpi(2.2) == 1 and SC.signal_us_cpi(2.7) == -1 and SC.signal_us_cpi(2.45) == 0
    assert SC.signal_us_cpi(2.4) == pytest.approx(0.2) and SC.signal_us_cpi(1.0) == 1     # 범위 밖은 잘린다
    assert SC.signal_jp_cpi(1.7) == 1 and SC.signal_jp_cpi(2.25) == 0 and SC.signal_jp_cpi(2.6) == -1
    assert SC.signal_dram(7.7) == pytest.approx(0.77) and SC.signal_dram(-30) == -1
    mild = SC.adjust(SCEN, {"dram": SC.signal_dram(2.0)})["adjusted"]
    strong = SC.adjust(SCEN, {"dram": SC.signal_dram(20.0)})["adjusted"]
    assert 0 < mild["S1"] - 25 < strong["S1"] - 25


def test_eps_stall_is_a_warning_not_neutral():
    assert SC.signal_eps(0.1) == -SC.EPS_STALL and SC.signal_eps(0.2) < 0                 # 제자리 = 상향 중단
    assert SC.signal_eps(5) == 1 and SC.signal_eps(1.0) == pytest.approx(0.2) and SC.signal_eps(None) is None
    assert SC.signal_eps(-5) == pytest.approx(-1.3, abs=0.31) and SC.signal_eps(-100) == -1


def test_bullish_signals_make_s1_top():
    res = SC.adjust(SCEN, {"us_core_cpi": 1, "eps_rev": 1, "soxx_spy": 1, "dram": 1, "fomc": 1})
    assert res["top"] == "S1"


def test_event_flags_count_only_after_base_date():
    events = [{"kind": "FOMC", "event_date": "2026-09-16", "flag": "hike"},
              {"kind": "BOJ", "event_date": "2026-09-18", "flag": "hawkish"}]
    assert SC.latest_flag(events, "FOMC", "2026-09-17", "2026-09-20") is None
    assert SC.latest_flag(events, "BOJ", "2026-09-17", "2026-09-20")["flag"] == "hawkish"
    assert SC.latest_flag(events, "BOJ", "2026-09-17", "2026-09-17") is None          # 미래 이벤트는 아직


def test_soxx_spy_relative_low():
    spy = [{"d": f"d{i:04d}", "v": 100.0} for i in range(300)]
    soxx = [{"d": f"d{i:04d}", "v": 50.0 - i * 0.01} for i in range(300)]
    assert SC.soxx_spy_state(soxx, spy)["signal"] == -1
    up = [{"d": f"d{i:04d}", "v": 50.0 + i * 0.01} for i in range(300)]
    assert SC.soxx_spy_state(up, spy)["signal"] == 1
    assert SC.soxx_spy_state(soxx[:10], spy)["signal"] is None
