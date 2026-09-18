"""백오피스 홈(SOXX 매크로)·매크로 관리 — 라우트·권한·시나리오 폼. 지표·이벤트는 수집기가 채우므로 입력 폼이 없다."""
import pytest

import alertbot.backoffice.app as A
from alertbot import db as DBM
from alertbot.macro import store as MS
from alertbot.macro.worker import seed
from tests.backoffice_login import logged_in


@pytest.fixture
def store(monkeypatch):
    s = DBM.DB.sqlite().init_schema()
    monkeypatch.setattr(A, "_store", s)
    return s


def collected(store):
    """수집기가 한 번 돈 상태."""
    seed(store)
    MS.upsert_series(store, [("ust_10y", "2026-09-15", 5.0), ("ust_10y", "2026-09-16", 5.03)], "fred")
    MS.upsert_series(store, [("jp_policy", "2026-09-16", 1.0)], "bis")
    MS.upsert_series(store, [("jp_core_cpi_yoy", "2026-08-01", 1.7)], "stat")
    MS.upsert_series(store, [("dram_spot", "2026-09-16", 45.79), ("dram_dir", "2026-09-16", 1), ("dram_30d_pct", "2026-09-16", 7.7)], "dram")
    MS.upsert_series(store, [("eps_rev_dir", "2026-09-17", 1), ("eps_rev_30d_pct", "2026-09-17", 22.3)], "yahoo")
    MS.upsert_event(store, {"event_date": "2026-10-28", "time_local": "14:00", "country": "US", "kind": "FOMC",
                            "title": "FOMC 금리 결정", "importance": 3, "source": "auto", "note": "10-27 시작"})
    MS.upsert_event(store, {"event_date": "2026-10-30", "time_local": None, "country": "JP", "kind": "BOJ",
                            "title": "BOJ 금융정책결정회의 + 전망보고서", "importance": 3, "source": "auto", "note": "10-29 시작"})
    MS.upsert_event(store, {"event_date": "2026-11-17", "time_local": "16:20", "country": "US", "kind": "EARNINGS",
                            "title": "엔비디아 실적", "importance": 3, "source": "auto", "note": "확정 · Yahoo Finance (NVDA)"})


def test_home_empty_db_renders_guidance(store):
    c = logged_in(A, store, "member@example.com", role="member")
    r = c.get("/")
    assert r.status_code == 200 and "SOXX 매크로" in r.text and "python -m alertbot.macro seed" in r.text
    assert 'href="/status"' in r.text and 'href="/macro/manage"' not in r.text          # 일반 사용자에겐 관리 링크 없음


def test_home_shows_collected_indicators_and_calendar(store):
    collected(store)
    c = logged_in(A, store, "member@example.com", role="member")
    r = c.get("/")
    assert r.status_code == 200
    for text in ("가장 유력한 연말 경로", "박스권", "판별 지표", "FOMC 기준금리", "BOJ 기준금리", "미 국채 10년", "5.03",
                 "상향 (30일 +22.3%)", "강세 (30일 +7.7%)", "1.7%", "이벤트 캘린더", "엔비디아 실적",
                 "분석 프레임이며 예측·컨센서스·투자 자문이 아님"):
        assert text in r.text, text
    assert "수집 대기" not in r.text.split("판별 지표")[1].split("미국 · 일본")[0]        # 지표 표에 빈칸이 없다
    assert c.get("/partials/home-market").status_code == 200
    month = c.get("/partials/home-calendar?view=month&ym=2026-10")
    assert month.status_code == 200 and "2026년 10월" in month.text and "BOJ" in month.text


def test_manage_is_admin_only_and_read_only(store):
    collected(store)
    member = logged_in(A, store, "member@example.com", role="member")
    assert member.get("/macro/manage").status_code == 403
    admin = logged_in(A, store)
    r = admin.get("/macro/manage")
    assert r.status_code == 200 and "매크로 관리" in r.text
    assert "수집 작업" in r.text and "수집 지표" in r.text and "BIS 정책금리(일별)" in r.text
    assert "엔비디아 실적" in r.text and "수동 지표 입력" not in r.text and "이벤트 추가" not in r.text
    for gone in ("/macro/manage/point", "/macro/manage/event"):
        assert gone not in r.text
    assert admin.post("/macro/manage/point", data={"key": "dram_dir", "obs_date": "2026-09-17", "value": "1"}).status_code == 404
    assert admin.post("/macro/manage/event", data={"event_date": "2026-09-17"}).status_code == 404


def test_scenarios_must_sum_to_100(store):
    seed(store)
    admin = logged_in(A, store)
    form = {}
    for s in MS.list_scenarios(store):
        form.update({f"{s['code']}_name": s["name"], f"{s['code']}_trigger": s["trigger_text"],
                     f"{s['code']}_low": s["soxx_low"], f"{s['code']}_high": s["soxx_high"], f"{s['code']}_prob": s["base_prob"]})
    r = admin.post("/macro/manage/scenarios", data={**form, "S1_prob": 40})
    assert r.status_code == 400 and "합이 100" in r.text
    assert admin.post("/macro/manage/scenarios", data={**form, "S1_prob": 30, "S2_prob": 30},
                      follow_redirects=False).status_code == 303
    assert {s["code"]: s["base_prob"] for s in MS.list_scenarios(store)}["S1"] == 30
    assert admin.post("/macro/manage/scenarios", data={**form, "S4_low": 400, "S4_high": 300}).status_code == 400


def test_status_moved_and_summary_redirect(store):
    admin = logged_in(A, store)
    assert admin.get("/summary?limit=8", follow_redirects=False).headers["location"] == "/status?limit=8"
    assert "엔진 상태" in admin.get("/status").text
