"""캘린더 자동 수집 파서 — 공식 페이지 구조(발췌)로 일정·결과를 읽는다."""
from alertbot.macro import calendar_sources as C

FED = """
<div class="panel panel-default"><div class="panel-heading"><h4><a id="42828">2026 FOMC Meetings</a></h4></div>
 <div class="row fomc-meeting">
  <div class="fomc-meeting__month col-md-2"><strong>September</strong></div>
  <div class="fomc-meeting__date col-lg-1">15-16*</div>
  <div><a href="/newsevents/pressreleases/monetary20260916a.htm">HTML</a></div>
 </div>
 <div class="fomc-meeting--shaded row fomc-meeting">
  <div class="fomc-meeting__month col-md-2"><strong>October</strong></div>
  <div class="fomc-meeting__date col-lg-1">27-28</div>
 </div>
 <div class="row fomc-meeting">
  <div class="fomc-meeting__month col-md-2"><strong>December/January</strong></div>
  <div class="fomc-meeting__date col-lg-1">31-1</div>
 </div>
</div>
<div class="panel panel-default"><div class="panel-heading"><h4><a id="45694">2027 FOMC Meetings</a></h4></div>
 <div class="row fomc-meeting">
  <div class="fomc-meeting__month col-md-2"><strong>March</strong></div>
  <div class="fomc-meeting__date col-lg-1">16-17*</div>
 </div>
</div>
"""


def test_fomc_calendar_dates_sep_and_year_rollover():
    rows = {r["event_date"]: r for r in C.parse_fomc_calendar(FED)}
    assert rows["2026-09-16"]["sep"] and rows["2026-09-16"]["stamp"] == "20260916"
    assert rows["2026-10-28"]["sep"] is False and rows["2026-10-28"]["first_day"] == "2026-10-27"
    assert rows["2027-01-01"]["first_day"] == "2026-12-31"          # 해를 넘기는 회의
    assert rows["2027-03-17"]["sep"]
    evs = {e["event_date"]: e for e in C.fomc_events(FED)}
    assert evs["2026-09-16"]["title"] == "FOMC 금리 결정 + 점도표" and evs["2026-10-28"]["title"] == "FOMC 금리 결정"
    assert evs["2026-10-28"]["country"] == "US" and evs["2026-10-28"]["time_local"] == "14:00" and evs["2026-10-28"]["importance"] == 3


HIKE = "<p>The Committee decided to raise the target range for the federal funds rate by 1/4 percentage point " \
       "to 3-3/4 to 4 percent, in support of the dual mandate.</p>"
HOLD = "<p>the Committee decided to maintain the target range for the federal funds rate at 3-1/2 to 3-3/4 percent.</p>"
CUT = "<p>The Committee decided to lower the target range for the federal funds rate by 1/2 percentage point to 3 to 3-1/4 percent.</p>"


def test_fomc_statement_reads_range_and_action():
    assert C.parse_fomc_statement(HIKE) == {"action": "raise", "low": 3.75, "high": 4.0}
    assert C.parse_fomc_statement(HOLD) == {"action": "maintain", "low": 3.5, "high": 3.75}
    assert C.parse_fomc_statement(CUT) == {"action": "lower", "low": 3.0, "high": 3.25}
    assert C.parse_fomc_statement("<p>준비 중</p>") == {}


BOJ = """<table><caption>Table : 2026</caption>
 <tr><th>Date of MPM</th><th>Release Schedule</th></tr>
 <tr><td><a href="/x.pdf">Sept. 17 (Thurs.), 18 (Fri.)</a></td><td>-</td><td>Oct. 1</td></tr>
 <tr><td>Oct. 29 (Thurs.), 30 (Fri.)</td><td>Oct. 30 (Fri.)</td><td>Nov. 10</td></tr>
 <tr><td>Apr. 30 (Thurs.), May 1 (Fri.)</td><td>-</td><td>May 20</td></tr>
</table>
<table><caption>Table : 2027</caption>
 <tr><td>Jan. 21 (Thurs.), 22 (Fri.)</td><td>Jan. 22 (Fri.)</td></tr>
</table>"""


def test_boj_schedule_outlook_and_cross_month():
    rows = {r["event_date"]: r for r in C.parse_boj_schedule(BOJ)}
    assert rows["2026-09-18"]["outlook"] is False and rows["2026-10-30"]["outlook"] is True
    assert rows["2026-05-01"]["first_day"] == "2026-04-30"          # 달을 넘기는 회의
    assert rows["2027-01-22"]["outlook"] is True
    evs = {e["event_date"]: e for e in C.boj_events(BOJ)}
    assert evs["2026-10-30"]["title"] == "BOJ 금융정책결정회의 + 전망보고서" and evs["2026-10-30"]["time_local"] is None
    assert evs["2026-09-18"]["title"] == "BOJ 금융정책결정회의" and evs["2026-09-18"]["country"] == "JP"


JP_SCHEDULE = """<table>
 <tr><th>Survey month</th><th>Date of release</th><th>Survey month</th><th>Date of release</th></tr>
 <tr><td>December, 2025</td><td>January 23, 2026</td><td>January, 2026</td><td>January 30, 2026</td></tr>
 <tr><td>January, 2026</td><td>February 20</td><td>February</td><td>February&nbsp;27</td></tr>
 <tr><td>August</td><td>September 18</td><td>September</td><td>September 25</td></tr>
 <tr><td>December</td><td>January 22, 2027</td><td>January, 2027</td><td>January 29, 2027</td></tr>
</table>"""


def test_jp_cpi_schedule_carries_year():
    rows = C.parse_jp_cpi_schedule(JP_SCHEDULE)
    assert ("2026-01-23", "12월분") in rows and ("2026-02-20", "1월분") in rows
    assert ("2026-09-18", "8월분") in rows and ("2027-01-22", "12월분") in rows
    ev = next(e for e in C.jp_cpi_events(JP_SCHEDULE) if e["event_date"] == "2026-09-18")
    assert ev["title"] == "일본 전국 CPI" and ev["time_local"] == "08:30" and ev["country"] == "JP"


def test_earnings_event_marks_estimated_dates():
    payload = {"quoteSummary": {"result": [{"calendarEvents": {"earnings": {"earningsDate": [{"fmt": "2026-09-30"}],
                                                                           "isEarningsDateEstimate": False}}}]}}
    ev = C.earnings_event("MU", "마이크론", payload)
    assert ev["event_date"] == "2026-09-30" and ev["title"] == "마이크론 실적" and "확정" in ev["note"] and ev["time_local"]
    guess = {"quoteSummary": {"result": [{"calendarEvents": {"earnings": {"earningsDate": [{"fmt": "2026-11-03"}],
                                                                         "isEarningsDateEstimate": True}}}]}}
    ev2 = C.earnings_event("AMD", "AMD", guess)
    assert "예상일" in ev2["note"] and ev2["time_local"] is None
    assert C.earnings_event("MU", "마이크론", {}) is None
    assert C.earnings_event("TSM", "TSMC", payload)["country"] == "TW"
