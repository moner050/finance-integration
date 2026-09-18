"""매크로 워커·저장소 — 가짜 fetch 로 SQLite 에 쌓고, 출처 우선순위·작업 격리·주기·결과 판정을 본다."""
from datetime import date, datetime, timedelta, timezone

from alertbot import db as DBM
from alertbot.macro import store, view
from alertbot.macro.worker import INTERVAL_MIN, MacroWorker, seed

NOW = datetime(2026, 9, 17, 8, 0, tzinfo=timezone.utc)


def mk():
    return DBM.DB.sqlite().init_schema()


def test_source_priority_official_over_market_feed():
    d = mk()
    store.upsert_series(d, [("ust_10y", "2026-09-16", 5.01)], "yahoo")
    store.upsert_series(d, [("ust_10y", "2026-09-16", 5.00)], "fred")
    store.upsert_series(d, [("ust_10y", "2026-09-16", 5.05)], "yahoo")          # 장중 시세가 공식 종가를 덮지 않는다
    assert store.load_series(d, ["ust_10y"], "2026-01-01")["ust_10y"][0] == {"d": "2026-09-16", "v": 5.0, "source": "fred", "note": None}


def test_seed_only_scenarios():
    d = mk()
    assert seed(d) == {"scenarios": 4}
    assert seed(d) == {"scenarios": 0}                                          # 다시 불러도 그대로
    assert {s["code"] for s in store.list_scenarios(d)} == {"S1", "S2", "S3", "S4"}
    assert store.list_events(d) == []                                           # 캘린더는 수집기 몫


def test_upsert_event_updates_schedule_but_keeps_result():
    d = mk()
    ev = {"event_date": "2026-10-30", "time_local": None, "country": "JP", "kind": "BOJ",
          "title": "BOJ 금융정책결정회의 + 전망보고서", "importance": 3, "source": "auto", "note": "10-29 시작"}
    assert store.upsert_event(d, ev) is True
    assert store.upsert_event(d, ev) is False                                   # 바뀐 게 없으면 건드리지 않는다
    row = store.list_events(d)[0]
    store.set_event_result(d, row["id"], "동결 (1.00%)", "hold")
    assert store.upsert_event(d, {**ev, "note": "10-29 시작 · 전망보고서 당일 공표"}) is True
    row = store.list_events(d)[0]
    assert row["note"].endswith("공표") and row["result"] == "동결 (1.00%)" and row["flag"] == "hold"


def fake_fetchers(fail=(), eps_pct=22.3, jp_policy=(("2026-06-16", 0.75), ("2026-09-16", 1.0), ("2026-09-19", 1.0))):
    calls = []

    def fred(key, start):
        calls.append(("fred", key))
        if key in fail:
            raise RuntimeError("boom")
        if key == "us_core_cpi":
            return [(key, "2025-08-01", 100.0), (key, "2026-08-01", 102.4)]
        return [(key, "2026-09-15", 4.0), (key, "2026-09-16", 4.1)]

    def yahoo(key, range_):
        calls.append(("yahoo", key, range_))
        return [(key, "2026-09-16", 100.0), (key, "2026-09-17", 101.0)]

    def jgb(since, history=False):
        calls.append(("mof", history))
        return [("jgb_10y", "2026-09-16", 3.0)]

    def rel(release_id, start, end):
        return ["2026-10-14"] if release_id == 10 else []

    def bis(country, key, start):
        calls.append(("bis", country))
        return [(key, d, v) for d, v in jp_policy]

    def jp_cpi():
        calls.append(("jp_cpi",))
        return [("jp_core_cpi_yoy", "2026-08-01", 1.7), ("jp_cpi_yoy", "2026-08-01", 1.9)]

    def dram(today):
        calls.append(("dram",))
        return [("dram_spot", "2026-08-17", 42.5), ("dram_spot", "2026-09-16", 45.786)]

    def summary(symbol):
        calls.append(("summary", symbol))
        if symbol in fail:
            raise RuntimeError("차단")
        base = 100.0
        return {"quoteSummary": {"result": [{
            "earningsTrend": {"trend": [{"period": "+1y", "epsTrend": {"current": {"raw": base * (1 + eps_pct / 100)},
                                                                       "30daysAgo": {"raw": base}}}]},
            "calendarEvents": {"earnings": {"earningsDate": [{"fmt": "2026-11-17"}], "isEarningsDateEstimate": False}}}]}}

    def fomc_cal():
        calls.append(("fomc_cal",))
        return [{"event_date": "2026-09-16", "time_local": "14:00", "country": "US", "kind": "FOMC",
                 "title": "FOMC 금리 결정 + 점도표", "importance": 3, "source": "auto", "note": "09-15 시작"},
                {"event_date": "2026-10-28", "time_local": "14:00", "country": "US", "kind": "FOMC",
                 "title": "FOMC 금리 결정", "importance": 3, "source": "auto", "note": "10-27 시작"}]

    def boj_cal():
        calls.append(("boj_cal",))
        return [{"event_date": "2026-09-16", "time_local": None, "country": "JP", "kind": "BOJ",
                 "title": "BOJ 금융정책결정회의", "importance": 3, "source": "auto", "note": "09-15 시작"}]

    def jp_cal():
        return [{"event_date": "2026-10-23", "time_local": "08:30", "country": "JP", "kind": "CPI",
                 "title": "일본 전국 CPI", "importance": 2, "source": "auto", "note": "9월분"}]

    def fomc_result(event_date):
        calls.append(("fomc_result", event_date))
        return {"result": "25bp 인상 → 3.75~4.00%", "flag": "hike"} if event_date == "2026-09-16" else {}

    return calls, dict(fetch_fred=fred, fetch_yahoo=yahoo, fetch_jgb=jgb, fetch_release_dates=rel, fetch_bis=bis,
                       fetch_jp_cpi=jp_cpi, fetch_dram=dram, fetch_summary=summary, fetch_fomc_calendar=fomc_cal,
                       fetch_boj_calendar=boj_cal, fetch_jp_cpi_calendar=jp_cal, fetch_fomc_result=fomc_result)


def worker(d, clock=None, fail=(), **kw):
    calls, fx = fake_fetchers(fail=fail, **kw)
    return calls, MacroWorker(d, clock=clock or (lambda: NOW), fred_enabled=True, **fx)


def test_poll_collects_every_source_and_isolates_failures():
    d = mk()
    seed(d)
    clock = [NOW]
    calls, w = worker(d, clock=lambda: clock[0], fail={"ust_2y"})
    jobs = w.poll_once()
    for name in ("yahoo", "mof", "bis", "jp_cpi", "dram", "eps", "calendar", "fred_calendar", "results", "scenario_log"):
        assert jobs[name]["ok"], (name, jobs[name]["error"])
    assert not jobs["fred"]["ok"] and "ust_2y" in jobs["fred"]["error"] and jobs["fred"]["rows"] > 0   # 나머지 FRED 는 저장
    series = store.load_series(d, ["jp_policy", "jp_core_cpi_yoy", "dram_spot", "dram_dir", "eps_rev_dir", "eps_rev_30d_pct"], "2026-01-01")
    assert series["jp_policy"][-1]["v"] == 1.0 and series["jp_core_cpi_yoy"][-1]["v"] == 1.7
    assert series["dram_spot"][-1]["v"] == 45.786 and series["dram_dir"][-1]["v"] == 1
    assert series["eps_rev_dir"][-1]["v"] == 1 and series["eps_rev_30d_pct"][-1]["v"] == 22.3
    kinds = {(e["kind"], e["event_date"]) for e in store.list_events(d)}
    assert ("FOMC", "2026-10-28") in kinds and ("BOJ", "2026-09-16") in kinds and ("CPI", "2026-10-14") in kinds
    assert ("EARNINGS", "2026-11-17") in kinds
    n = len(calls)
    clock[0] = NOW + timedelta(minutes=INTERVAL_MIN["yahoo"] - 1)
    w.poll_once()
    assert len(calls) == n                                                    # 아직 주기 전
    clock[0] = NOW + timedelta(minutes=INTERVAL_MIN["yahoo"])
    w.poll_once()
    assert sum(1 for c in calls[n:] if c[0] == "yahoo") == 4 and not any(c[0] == "bis" for c in calls[n:])


def test_eps_direction_flips_on_downgrades():
    d = mk()
    _, w = worker(d, eps_pct=-3.0)
    w.run_job("eps", {}, NOW)
    assert store.load_series(d, ["eps_rev_dir"], "2026-01-01")["eps_rev_dir"][-1]["v"] == -1
    d2 = mk()
    _, w2 = worker(d2, eps_pct=0.1)
    w2.run_job("eps", {}, NOW)
    assert store.load_series(d2, ["eps_rev_dir"], "2026-01-01")["eps_rev_dir"][-1]["v"] == 0      # 상향 중단


def test_eps_partial_failure_still_stores_rest():
    d = mk()
    jobs = {}
    _, w = worker(d, fail={"NVDA"})
    w.run_job("eps", jobs, NOW)
    assert not jobs["eps"]["ok"] and "NVDA" in jobs["eps"]["error"]
    assert store.load_series(d, ["eps_rev_dir"], "2026-01-01")["eps_rev_dir"]                     # 나머지 4종목으로 계산


def test_results_fill_fomc_from_statement_and_boj_from_policy_rate():
    d = mk()
    seed(d)
    _, w = worker(d)
    w.poll_once()
    events = {(e["kind"], e["event_date"]): e for e in store.list_events(d)}
    fomc = events[("FOMC", "2026-09-16")]
    assert fomc["flag"] == "hike" and "3.75~4.00%" in fomc["result"]
    boj = events[("BOJ", "2026-09-16")]
    assert boj["flag"] == "hike" and boj["result"] == "25bp 인상 → 1.00%"


def test_boj_hold_and_pending_are_distinguished():
    d = mk()
    seed(d)
    _, w = worker(d, jp_policy=(("2026-09-15", 1.0), ("2026-09-17", 1.0)))
    w.poll_once()
    boj = next(e for e in store.list_events(d) if e["kind"] == "BOJ")
    assert boj["flag"] == "hold" and boj["result"] == "동결 (1.00%)"
    d2 = mk()
    seed(d2)
    _, w2 = worker(d2, jp_policy=(("2026-09-14", 1.0), ("2026-09-15", 1.0)))       # 회의일 뒤 값이 아직 없다
    w2.poll_once()
    boj2 = next(e for e in store.list_events(d2) if e["kind"] == "BOJ")
    assert not boj2["result"]


def test_backfill_uses_history_ranges():
    d = mk()
    calls, w = worker(d)
    w.backfill(years=3)
    assert ("yahoo", "dxy", "3y") in calls and ("mof", True) in calls and ("bis", "JP") in calls


def test_home_context_end_to_end():
    d = mk()
    seed(d)
    _, w = worker(d)
    w.poll_once()
    ctx = view.home_context(d, NOW)
    assert ctx["top"]["code"] in {"S1", "S2", "S3", "S4"} and ctx["soxx"]["price"] == 101.0
    us = ctx["market"]["us"]
    assert us["policy"]["rate_text"] == "3.75–4.00%" and us["policy"]["pending_fred"]      # 9/16 성명이 FRED(9/16)보다 최신
    assert us["policy"]["next"]["event_date"] == "2026-10-28" and us["policy"]["next_dday"] == 41
    assert ctx["market"]["jp"]["policy"]["rate_text"] == "1.00%"
    by_key = {i["key"]: i for i in ctx["scen"]["indicators"]}
    assert by_key["us_core_cpi"]["value"] == "2.4%" and by_key["jp_core_cpi"]["value"] == "1.7%"
    assert "상향" in by_key["eps_rev"]["value"] and "30일" in by_key["eps_rev"]["value"]
    assert "강세" in by_key["dram"]["value"]
    # 예전에 수동이던 셋은 이제 미반영이 아니다 (상대강도는 봉이 60개 미만, 결정 결과는 기준일 이전이라 빠진다)
    assert not ({"일본 근원 CPI (전년비)", "반도체 EPS 추정치", "DRAM 현물가"} & set(ctx["scen"]["missing"]))


def test_event_kst_handles_dst():
    summer = view.event_kst({"event_date": "2026-10-28", "time_local": "14:00", "country": "US"})
    winter = view.event_kst({"event_date": "2026-12-09", "time_local": "14:00", "country": "US"})
    assert summer.strftime("%m/%d %H:%M") == "10/29 03:00" and winter.strftime("%m/%d %H:%M") == "12/10 04:00"
    assert view.event_kst({"event_date": "2026-09-18", "time_local": None, "country": "JP"}) is None


def test_calendar_month_grid_starts_sunday():
    d = mk()
    _, w = worker(d)
    w.run_job("calendar", {}, NOW)
    m = view.calendar_month(d, "2026-10", date(2026, 9, 17))
    assert m["weeks"][0][0]["date"] == date(2026, 9, 27) and all(len(w) == 7 for w in m["weeks"])
    assert m["prev"] == "2026-09" and m["next"] == "2026-11"
    oct28 = next(c for wk in m["weeks"] for c in wk if c["date"] == date(2026, 10, 28))
    assert any(e["kind"] == "FOMC" for e in oct28["events"])
