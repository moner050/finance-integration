"""매크로 수집 워커 — 작업별 주기로 소스를 불러 alert_macro_series·events 에 쌓는다. 수동 입력은 없다.

작업은 서로 격리한다: 하나가 실패해도 나머지는 저장하고, 결과는 alert_settings.macro_jobs 에 남겨 홈·관리 화면이 보여 준다.
알림은 보내지 않는다. fetch 함수는 주입할 수 있다(테스트는 가짜로).

    series   Yahoo(10분) · FRED(60분) · MOF(60분) · BIS 정책금리·일본 CPI·DRAM(3시간) · EPS 추정치(12시간)
    calendar FOMC·BOJ·일본 CPI 일정(12시간) · FRED 발표 일정(12시간) · 실적 발표일(EPS 작업과 함께)
    results  지난 FOMC 성명·BOJ 결정 판정(60분)
"""

import logging
import statistics
from datetime import date, datetime, timedelta, timezone

from . import calendar_seed, calendar_sources, sources, store
from .view import compute_scenarios

log = logging.getLogger("macro")

INTERVAL_MIN = {"yahoo": 10, "fred": 60, "mof": 60, "bis": 180, "jp_cpi": 180, "dram": 180,
                "eps": 720, "calendar": 720, "fred_calendar": 720, "results": 60, "scenario_log": 60}
BACKFILL_YEARS = 3


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


def seed(db) -> dict:
    """시나리오 기본값만 시드한다 — 캘린더·지표는 전부 수집기가 채운다."""
    return {"scenarios": store.seed_scenarios(db, calendar_seed.SCENARIOS)}


class MacroWorker:
    def __init__(self, db, fetch_fred=sources.fetch_fred, fetch_yahoo=sources.fetch_yahoo, fetch_jgb=sources.fetch_jgb,
                 fetch_release_dates=sources.fetch_fred_release_dates, fetch_bis=sources.fetch_bis_policy,
                 fetch_jp_cpi=sources.fetch_jp_cpi, fetch_dram=sources.fetch_dram, fetch_summary=sources.fetch_quote_summary,
                 fetch_fomc_calendar=calendar_sources.fetch_fomc_calendar, fetch_boj_calendar=calendar_sources.fetch_boj_calendar,
                 fetch_jp_cpi_calendar=calendar_sources.fetch_jp_cpi_calendar, fetch_fomc_result=calendar_sources.fetch_fomc_result,
                 clock=None, fred_enabled: bool = None):
        self.db = db
        self.fetch_fred, self.fetch_yahoo, self.fetch_jgb = fetch_fred, fetch_yahoo, fetch_jgb
        self.fetch_release_dates, self.fetch_bis = fetch_release_dates, fetch_bis
        self.fetch_jp_cpi, self.fetch_dram, self.fetch_summary = fetch_jp_cpi, fetch_dram, fetch_summary
        self.fetch_fomc_calendar, self.fetch_boj_calendar = fetch_fomc_calendar, fetch_boj_calendar
        self.fetch_jp_cpi_calendar, self.fetch_fomc_result = fetch_jp_cpi_calendar, fetch_fomc_result
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.fred_enabled = bool(sources.FRED_API_KEY) if fred_enabled is None else fred_enabled

    def today(self) -> date:
        return self.clock().date()

    # -- 시계열 ----------------------------------------------------------------------
    def job_yahoo(self, range_: str = "1mo") -> int:
        n, errors = 0, []
        for key in sources.YAHOO_SYMBOLS:
            try:
                n += store.upsert_series(self.db, self.fetch_yahoo(key, range_), "yahoo")
            except Exception as e:
                errors.append(f"{key}: {e}")
        if errors:
            raise PartialError(n, errors)
        return n

    def job_fred(self, years: float = None) -> int:
        if not self.fred_enabled:
            raise RuntimeError("ALERT_FRED_API_KEY·FRED_API_KEY 가 없어 건너뜀")
        today = self.today()
        n, errors = 0, []
        for key in sources.FRED_SERIES:
            if years:
                start = today - timedelta(days=int(365 * (years + (1 if key == "us_core_cpi" else 0))))
            else:
                start = today - timedelta(days=430 if key == "us_core_cpi" else 40)
            try:
                n += store.upsert_series(self.db, self.fetch_fred(key, start), "fred")
            except Exception as e:
                errors.append(f"{key}: {e}")
        if errors:
            raise PartialError(n, errors)
        return n

    def job_mof(self, years: float = None) -> int:
        today = self.today()
        since = today - timedelta(days=int(365 * years)) if years else today - timedelta(days=40)
        return store.upsert_series(self.db, self.fetch_jgb(since, history=bool(years)), "mof")

    def job_bis(self, years: float = None) -> int:
        """BOJ 기준금리 — BIS 중앙은행 정책금리(일별). 며칠 늦게 올라오지만 값 자체가 공식이다."""
        start = self.today() - timedelta(days=int(365 * (years or 3)))
        return store.upsert_series(self.db, self.fetch_bis("JP", "jp_policy", start), "bis")

    def job_jp_cpi(self, years: float = None) -> int:
        """일본 전년동월비 CPI — 통계국 요약 페이지(최신 달 하나). 과거는 발표될 때마다 쌓인다."""
        return store.upsert_series(self.db, self.fetch_jp_cpi(), "stat")

    def job_dram(self, years: float = None) -> int:
        """DRAM 현물가와 방향(30일 변화율)."""
        rows = self.fetch_dram(self.today())
        n = store.upsert_series(self.db, rows, "dram")
        hist = [("dram_spot", p["d"], p["v"]) for p in
                store.load_series(self.db, ["dram_spot"], (self.today() - timedelta(days=120)).isoformat())["dram_spot"]]
        pct, dir_ = sources.direction(hist or rows, 30, sources.DRAM_DIR_PCT)
        if dir_ is not None:
            last_day = max(d for _, d, _ in (hist or rows))
            n += store.upsert_series(self.db, [("dram_30d_pct", last_day, round(pct, 2)), ("dram_dir", last_day, dir_)], "dram")
        return n

    def job_eps(self, years: float = None) -> int:
        """반도체 EPS 추정치 방향 + 실적 발표일 — quoteSummary 한 번으로 둘 다."""
        today = self.today().isoformat()
        pcts, events, errors = [], [], []
        for symbol, name in sources.SEMIS.items():
            try:
                payload = self.fetch_summary(symbol)
            except Exception as e:
                errors.append(f"{symbol}: {e}")
                continue
            trend = sources.parse_eps_trend(payload)
            if trend.get("current") and trend.get("30daysAgo"):
                pcts.append((trend["current"] / trend["30daysAgo"] - 1) * 100)
            ev = calendar_sources.earnings_event(symbol, name, payload)
            if ev:
                events.append(ev)
        n = sum(1 for ev in events if store.upsert_event(self.db, ev))
        if pcts:
            pct = statistics.median(pcts)
            dir_ = 1 if pct >= sources.EPS_DIR_PCT else -1 if pct <= -sources.EPS_DIR_PCT else 0
            n += store.upsert_series(self.db, [("eps_rev_30d_pct", today, round(pct, 2)), ("eps_rev_dir", today, dir_)], "yahoo")
        elif not errors:
            errors.append("EPS 추정치가 비어 있다")
        if errors:
            raise PartialError(n, errors)
        return n

    # -- 캘린더 ----------------------------------------------------------------------
    def job_calendar(self) -> int:
        """FOMC·BOJ·일본 CPI 일정. 이미 있는 이벤트는 시각·중요도·메모만 갱신하고 결과는 건드리지 않는다."""
        n, errors = 0, []
        for name, fetch in (("FOMC", self.fetch_fomc_calendar), ("BOJ", self.fetch_boj_calendar),
                            ("일본 CPI", self.fetch_jp_cpi_calendar)):
            try:
                n += sum(1 for ev in fetch() if store.upsert_event(self.db, ev))
            except Exception as e:
                errors.append(f"{name}: {e}")
        store.drop_stale_seed_events(self.db)
        if errors:
            raise PartialError(n, errors)
        return n

    def job_fred_calendar(self) -> int:
        if not self.fred_enabled:
            raise RuntimeError("ALERT_FRED_API_KEY·FRED_API_KEY 가 없어 건너뜀")
        today = self.today()
        n, errors = 0, []
        for release_id, kind, title, importance, t in sources.FRED_RELEASES:
            try:
                for d in self.fetch_release_dates(release_id, today - timedelta(days=60), today + timedelta(days=400)):
                    n += store.upsert_event(self.db, {"event_date": d, "time_local": t, "country": "US", "kind": kind,
                                                      "title": title, "importance": importance, "source": "fred",
                                                      "note": "FRED 발표 일정"})
            except Exception as e:
                errors.append(f"{title}: {e}")
        if errors:
            raise PartialError(n, errors)
        return n

    # -- 결과 판정 --------------------------------------------------------------------
    def job_results(self) -> int:
        """지난 회의의 결과를 채운다 — FOMC 는 성명에서, BOJ 는 BIS 정책금리 변화에서."""
        today = self.today()
        pending = [e for e in store.list_events(self.db, (today - timedelta(days=45)).isoformat(), today.isoformat())
                   if e["kind"] in ("FOMC", "BOJ") and not e["result"]]
        n, errors = 0, []
        for ev in pending:
            try:
                got = self.fetch_fomc_result(ev["event_date"]) if ev["kind"] == "FOMC" else self._boj_result(ev["event_date"])
                if got:
                    store.set_event_result(self.db, ev["id"], got["result"], got["flag"])
                    n += 1
                    log.info("매크로 %s %s 결과: %s", ev["kind"], ev["event_date"], got["result"])
            except Exception as e:
                errors.append(f"{ev['kind']} {ev['event_date']}: {e}")
        if errors:
            raise PartialError(n, errors)
        return n

    def _boj_result(self, event_date: str) -> dict:
        """BIS 정책금리가 회의일 다음 날까지 들어와 있어야 판정한다(인상·인하·동결)."""
        pts = store.load_series(self.db, ["jp_policy"], (date.fromisoformat(event_date) - timedelta(days=120)).isoformat())["jp_policy"]
        after = [p for p in pts if p["d"] > event_date]
        before = [p for p in pts if p["d"] < event_date]
        if not after or not before:
            return {}
        prev, cur = before[-1]["v"], after[-1]["v"]
        if cur == prev:
            return {"result": f"동결 ({cur:.2f}%)", "flag": "hold"}
        diff = (cur - prev) * 100
        return {"result": f"{abs(diff):.0f}bp {'인상' if diff > 0 else '인하'} → {cur:.2f}%", "flag": "hike" if diff > 0 else "cut"}

    def job_scenario_log(self) -> int:
        today = self.today()
        res = compute_scenarios(self.db, today)
        if not res["scenarios"]:
            return 0
        store.log_scenarios(self.db, today.isoformat(), res["result"]["base"], res["result"]["adjusted"])
        return 1

    # -- 실행 ------------------------------------------------------------------------
    def run_job(self, name: str, jobs: dict, now: datetime, **kw):
        fn = getattr(self, f"job_{name}")
        try:
            rows = fn(**kw)
            jobs[name] = {"at": _iso(now), "ok": True, "rows": rows, "error": None}
        except PartialError as e:
            jobs[name] = {"at": _iso(now), "ok": False, "rows": e.rows, "error": "; ".join(e.errors)[:500]}
            log.warning("매크로 %s 일부 실패: %s", name, jobs[name]["error"])
        except Exception as e:
            jobs[name] = {"at": _iso(now), "ok": False, "rows": 0, "error": str(e)[:500]}
            log.warning("매크로 %s 실패: %s", name, e)

    def poll_once(self, force: bool = False) -> dict:
        now = self.clock()
        jobs = store.load_jobs(self.db)
        for name, minutes in INTERVAL_MIN.items():
            last = jobs.get(name, {}).get("at")
            due = force or not last or now - datetime.fromisoformat(last) >= timedelta(minutes=minutes)
            if due:
                self.run_job(name, jobs, now)
        store.save_jobs(self.db, jobs)
        return jobs

    def backfill(self, years: float = BACKFILL_YEARS) -> dict:
        now = self.clock()
        jobs = store.load_jobs(self.db)
        self.run_job("yahoo", jobs, now, range_=f"{int(years)}y")
        self.run_job("fred", jobs, now, years=years)
        self.run_job("mof", jobs, now, years=years)
        for name in ("bis", "jp_cpi", "dram", "eps", "calendar", "fred_calendar", "results", "scenario_log"):
            self.run_job(name, jobs, now)
        store.save_jobs(self.db, jobs)
        return jobs


class PartialError(Exception):
    def __init__(self, rows: int, errors: list):
        super().__init__("; ".join(errors))
        self.rows, self.errors = rows, errors
