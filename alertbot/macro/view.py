"""매크로 홈 화면 데이터 — DB 에서 읽어 템플릿이 그대로 쓸 dict 로 만든다(계산은 scoring).

날짜 기준은 KST 다. 이벤트는 그 나라 현지 날짜·시각으로 저장하고 여기서 KST 로 바꿔 보여 준다.
"""

import re
from datetime import date, datetime, time, timedelta, timezone

from . import scoring, store

KST = timezone(timedelta(hours=9))
try:
    from zoneinfo import ZoneInfo
    _NY = ZoneInfo("America/New_York")
except Exception:                                   # tzdata 없음 — 서머타임 근사
    _NY = None
COUNTRY_TZ = {"JP": timezone(timedelta(hours=9)), "TW": timezone(timedelta(hours=8)), "KR": KST}
COUNTRY_LABEL = {"US": "미국", "JP": "일본", "TW": "대만", "GLOBAL": "글로벌"}
KIND_LABEL = {"FOMC": "FOMC", "BOJ": "BOJ", "CPI": "CPI", "JOBS": "고용", "PCE": "PCE", "EARNINGS": "실적", "OTHER": "기타"}
KINDS = tuple(KIND_LABEL)
COUNTRIES = tuple(COUNTRY_LABEL)
FLAGS = ("", "hike", "hold", "cut", "hawkish", "dovish")
FLAG_LABEL = {"hike": "인상", "hold": "동결", "cut": "인하", "hawkish": "매파", "dovish": "비둘기"}

SERIES_META = {   # key → (라벨, 종류) 종류: yield(%·bp) | index | fx
    "dxy": ("달러인덱스 DXY", "index"),
    "ust_2y": ("미 국채 2년", "yield"), "ust_10y": ("미 국채 10년", "yield"), "ust_30y": ("미 국채 30년", "yield"),
    "usdjpy": ("USD/JPY", "fx"),
    "jgb_2y": ("일본 국채 2년", "yield"), "jgb_10y": ("일본 국채 10년", "yield"), "jgb_30y": ("일본 국채 30년", "yield"),
}
US_TILES = ("dxy", "ust_2y", "ust_10y", "ust_30y")
JP_TILES = ("usdjpy", "jgb_2y", "jgb_10y", "jgb_30y")
# 수집 지표 → (라벨, 소스, 단위). 수동 입력은 없다 — 전부 macro/worker.py 가 받아 온다.
AUTO_KEYS = {
    "jp_policy": ("BOJ 기준금리", "BIS 정책금리(일별)", "%"),
    "jp_core_cpi_yoy": ("일본 근원 CPI 전년비", "총무성 통계국", "%"),
    "jp_cpi_yoy": ("일본 종합 CPI 전년비", "총무성 통계국", "%"),
    "jp_core_core_cpi_yoy": ("일본 근원근원 CPI 전년비", "총무성 통계국", "%"),
    "us_core_cpi": ("미 근원 CPI 지수", "FRED CPILFESL", ""),
    "eps_rev_30d_pct": ("반도체 EPS 추정치 30일 변화", "Yahoo (NVDA·AVGO·TSM·MU·AMD 중앙값)", "%"),
    "eps_rev_dir": ("반도체 EPS 추정치 방향", "Yahoo earningsTrend", ""),
    "dram_spot": ("DRAM 현물가 (DDR4 1Gx8)", "TrendForce 집계 JSON → DRAMeXchange", "$"),
    "dram_30d_pct": ("DRAM 30일 변화", "위와 같음", "%"),
    "dram_dir": ("DRAM 현물가 방향", "위와 같음", ""),
}
DIR_TEXT = {"eps_rev_dir": {1: "상향", 0: "상향 중단", -1: "하향"}, "dram_dir": {1: "강세", 0: "보합", -1: "하락 전환"}}
STALE_DAYS = 5
WEEKDAYS = ("월", "화", "수", "목", "금", "토", "일")
JOB_LABEL = {"yahoo": "시세(Yahoo)", "fred": "미 금리(FRED)", "mof": "JGB(MOF)", "bis": "BOJ 금리(BIS)", "jp_cpi": "일본 CPI(통계국)",
             "dram": "DRAM 현물가", "eps": "EPS 추정치", "calendar": "FOMC·BOJ 일정", "fred_calendar": "미 발표 일정",
             "results": "결정 결과", "scenario_log": "시나리오 기록"}
RANGE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[~–-]\s*(\d+(?:\.\d+)?)\s*%")


def kst_today(now: datetime = None) -> date:
    return (now or datetime.now(timezone.utc)).astimezone(KST).date()


def _us_tz(d: date):
    if _NY is not None:
        return _NY
    from ..timeutil import _us_offset
    return timezone(_us_offset(datetime.combine(d, time(12), timezone.utc)))


def event_kst(ev: dict):
    """현지 날짜·시각 → KST datetime. 시각이 없으면 None."""
    if not ev.get("time_local"):
        return None
    d = date.fromisoformat(ev["event_date"])
    hh, mm = (int(x) for x in ev["time_local"].split(":"))
    tz = _us_tz(d) if ev["country"] == "US" else COUNTRY_TZ.get(ev["country"], KST)
    return datetime.combine(d, time(hh, mm), tz).astimezone(KST)


def spark(values: list, w: int = 120, h: int = 34, pad: int = 2) -> str:
    """SVG polyline points. 값이 하나뿐이면 빈 문자열."""
    if len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    step = (w - 2 * pad) / (len(values) - 1)
    return " ".join(f"{pad + i * step:.1f},{pad + (h - 2 * pad) * (1 - (v - lo) / span):.1f}" for i, v in enumerate(values))


def _fmt_change(kind: str, values: list, n: int):
    if len(values) <= n:
        return None
    a, b = values[-1 - n], values[-1]
    if kind == "yield":
        diff = round((b - a) * 100)
        return {"text": f"{abs(diff)}bp", "sign": (diff > 0) - (diff < 0)}
    pct = (b / a - 1) * 100 if a else 0
    return {"text": f"{abs(pct):.2f}%", "sign": (pct > 0.004) - (pct < -0.004)}


def tile(key: str, points: list, today: date) -> dict:
    label, kind = SERIES_META[key]
    values = [p["v"] for p in points]
    if not values:
        return {"key": key, "label": label, "kind": kind, "empty": True}
    g = scoring.grade_series(key, values)
    last = points[-1]
    age = (today - date.fromisoformat(last["d"])).days
    value_text = f"{values[-1]:.3f}" if key.startswith("jgb") else f"{values[-1]:,.2f}"     # MOF 는 소수 셋째 자리까지 준다
    return {"key": key, "label": label, "kind": kind, "empty": False, "value": values[-1], "value_text": value_text,
            "unit": "%" if kind == "yield" else "", "d1": _fmt_change(kind, values, 1), "d5": _fmt_change(kind, values, 5),
            "grade": g["grade"], "grade_label": scoring.GRADE_LABEL[g["grade"]], "reasons": g["reasons"],
            "spark": spark(values[-60:]), "as_of": last["d"], "source": last["source"], "stale": age > STALE_DAYS}


def _last(points: list):
    return points[-1] if points else None


def _spread(a: list, b: list):
    """같은 날짜의 a − b 시계열(%p)."""
    by = {p["d"]: p["v"] for p in b}
    return [p["v"] - by[p["d"]] for p in a if p["d"] in by]


def spread_chip(label: str, series: list, kind: str) -> dict:
    if not series:
        return {"label": label, "text": "-", "tone": "muted", "state": "데이터 없음"}
    v = series[-1]
    if kind == "curve":
        st = scoring.curve_state(v, series[:-1])
        return {"label": label, "text": f"{v * 100:+.0f}bp", "tone": st["tone"], "state": st["label"]}
    g = scoring.carry_grade(v)
    return {"label": label, "text": f"{v:.2f}%p", "tone": ("ok", "info", "warn", "danger")[g], "state": "캐리 여유 " + scoring.GRADE_LABEL[g]}


def policy_block(db_events: list, kind: str, rate_text: str, rate_as_of: str, today: date) -> dict:
    iso = today.isoformat()
    past = [e for e in db_events if e["kind"] == kind and e["event_date"] <= iso]
    decided = [e for e in past if e.get("result")]
    upcoming = [e for e in db_events if e["kind"] == kind and e["event_date"] >= iso]
    last = decided[-1] if decided else None
    nxt = upcoming[0] if upcoming else None
    grade = 2 if last and last.get("flag") in ("hike", "hawkish") else 0
    return {"rate_text": rate_text, "as_of": rate_as_of, "last": last, "last_flag": FLAG_LABEL.get((last or {}).get("flag"), ""),
            "next": nxt, "next_dday": scoring.d_day(nxt["event_date"], today) if nxt else None, "grade": grade,
            "grade_label": scoring.GRADE_LABEL[grade]}


# -- 시나리오 --------------------------------------------------------------------------

def _direction(sig) -> tuple:
    """신호 세기 → (색, 문구). 연속값이라 −0.3~+0.3 은 중립으로 본다."""
    if sig is None:
        return "muted", "미반영"
    if sig >= 0.6:
        return "ok", "S1 방향 (강)"
    if sig >= 0.3:
        return "ok", "S1 방향"
    if sig <= -0.6:
        return "danger", "S3/S4 방향 (강)"
    if sig <= -0.3:
        return "warn", "S3/S4 방향"
    return "neutral", "중립"


def _dir_text(key: str, value, pct_point) -> str:
    """'상향 (30일 +22.3%)' — 방향과 변화율을 같이 보여 준다."""
    if value is None:
        return "수집 대기"
    text = DIR_TEXT[key].get(value, str(value))
    return f"{text} (30일 {pct_point['v']:+.1f}%)" if pct_point else text


def _dir_value(points: list):
    p = _last(points)
    return None if p is None else int(round(p["v"]))


def compute_scenarios(db, today: date) -> dict:
    scenarios = store.list_scenarios(db)
    base_date = max((s["updated_at"][:10] for s in scenarios), default=today.isoformat())
    since = (today - timedelta(days=800)).isoformat()
    s = store.load_series(db, ("us_core_cpi", "jp_core_cpi_yoy", "eps_rev_dir", "eps_rev_30d_pct", "dram_dir", "dram_30d_pct",
                               "soxx", "spy"), since)
    events = store.list_events(db, (today - timedelta(days=400)).isoformat(), today.isoformat())
    iso = today.isoformat()

    us_yoy, us_month = scoring.yoy(s["us_core_cpi"])
    jp = _last(s["jp_core_cpi_yoy"])
    eps, dram = _dir_value(s["eps_rev_dir"]), _dir_value(s["dram_dir"])
    eps_pct, dram_pct = _last(s["eps_rev_30d_pct"]), _last(s["dram_30d_pct"])
    rs = scoring.soxx_spy_state(s["soxx"], s["spy"])
    fomc = scoring.latest_flag(events, "FOMC", base_date, iso)
    boj = scoring.latest_flag(events, "BOJ", base_date, iso)

    signals = {
        "us_core_cpi": scoring.signal_us_cpi(us_yoy),
        "jp_core_cpi": scoring.signal_jp_cpi(jp["v"] if jp else None),
        "eps_rev": scoring.signal_eps(eps_pct["v"] if eps_pct else None),
        "soxx_spy": rs["signal"],
        "dram": scoring.signal_dram(dram_pct["v"] if dram_pct else None),
        "fomc": scoring.FLAG_SIGNAL.get(fomc["flag"]) if fomc else None,
        "boj": scoring.FLAG_SIGNAL.get(boj["flag"]) if boj else None,
    }
    values = {
        "us_core_cpi": (f"{us_yoy:.1f}%" if us_yoy is not None else "-", f"{us_month[:7]}월분" if us_month else ""),
        "jp_core_cpi": (f"{jp['v']:.1f}%" if jp else "수집 대기", jp["d"][:7] if jp else ""),
        "eps_rev": (_dir_text("eps_rev_dir", eps, _last(s["eps_rev_30d_pct"])), _last(s["eps_rev_dir"])["d"] if eps is not None else ""),
        "soxx_spy": (rs["text"], s["soxx"][-1]["d"] if s["soxx"] else ""),
        "dram": (_dir_text("dram_dir", dram, _last(s["dram_30d_pct"])), _last(s["dram_dir"])["d"] if dram is not None else ""),
        "fomc": ((fomc["result"] or FLAG_LABEL.get(fomc["flag"], "")) if fomc else "기준일 이후 없음", fomc["event_date"] if fomc else ""),
        "boj": ((boj["result"] or FLAG_LABEL.get(boj["flag"], "")) if boj else "기준일 이후 없음", boj["event_date"] if boj else ""),
    }
    result = scoring.adjust(scenarios, signals)
    rows = []
    for key, meta in scoring.INDICATORS.items():
        sig = signals[key]
        tone, direction = _direction(sig)
        rows.append({"key": key, "label": meta["label"], "value": values[key][0], "as_of": values[key][1], "bull": meta["bull"],
                     "bear": meta["bear"], "signal": sig, "tone": tone, "direction": direction, "weight": meta["w"]})
    cur = s["soxx"][-1]["v"] if s["soxx"] else None
    for sc in scenarios:
        sc["prob"] = result["adjusted"].get(sc["code"])
        sc["base"] = result["base"].get(sc["code"])
        sc["delta"] = round(sc["prob"] - sc["base"], 1) if sc["prob"] is not None else None
        sc["mid"] = (sc["soxx_low"] + sc["soxx_high"]) / 2
        sc["vs_now"] = (sc["mid"] / cur - 1) * 100 if cur else None
    return {"scenarios": scenarios, "result": result, "indicators": rows, "base_date": base_date, "soxx": cur,
            "missing": [r["label"] for r in rows if r["signal"] is None]}


def price_ruler(scenarios: list, cur: float, ev: float) -> dict:
    """가격 눈금자 — 범위 막대·현재가·기대값의 가로 위치(%)."""
    if not scenarios:
        return {}
    lo = min([s["soxx_low"] for s in scenarios] + ([cur] if cur else []))
    hi = max([s["soxx_high"] for s in scenarios] + ([cur] if cur else []))
    lo, hi = lo - (hi - lo) * 0.04, hi + (hi - lo) * 0.04
    pos = lambda v: round((v - lo) / (hi - lo) * 100, 2)
    return {"bands": [{"code": s["code"], "left": pos(s["soxx_low"]), "width": pos(s["soxx_high"]) - pos(s["soxx_low"])} for s in scenarios],
            "cur": pos(cur) if cur else None, "ev": pos(ev) if ev else None,
            "ticks": [{"v": v, "left": pos(v)} for v in range(int(lo // 50 + 1) * 50, int(hi) + 1, 50)]}


# -- 캘린더 ----------------------------------------------------------------------------

def decorate_event(ev: dict, today: date) -> dict:
    at = event_kst(ev)
    d = date.fromisoformat(ev["event_date"])
    dd = (d - today).days
    return {**ev, "kst": at, "kst_text": at.strftime("%m/%d %H:%M") if at else None, "dday": dd,
            "day": d.day, "md": f"{d.month}/{d.day}", "wd": WEEKDAYS[d.weekday()], "importance": int(ev["importance"]),
            "country_label": COUNTRY_LABEL.get(ev["country"], ev["country"]), "kind_label": KIND_LABEL.get(ev["kind"], ev["kind"]),
            "flag_label": FLAG_LABEL.get(ev.get("flag") or "", ""), "past": dd < 0, "cluster": None}


def find_clusters(events: list, hours: int = 72) -> list:
    """★3 이벤트끼리 결정일 간격이 hours 이내로 이어지면 한 묶음. 반환 [{start, end, titles, ids}] (2개 이상만)."""
    major = [e for e in events if int(e["importance"]) >= 3]
    groups, cur = [], []
    for e in major:
        if cur and (date.fromisoformat(e["event_date"]) - date.fromisoformat(cur[-1]["event_date"])).days * 24 > hours - 24:
            groups.append(cur)
            cur = []
        cur.append(e)
    if cur:
        groups.append(cur)
    out = []
    for g in groups:
        kinds = {e["kind"] for e in g}
        if len(g) >= 2 and len(kinds) >= 2:
            out.append({"start": g[0]["event_date"], "end": g[-1]["event_date"], "titles": [e["title"] for e in g],
                        "ids": [e["id"] for e in g]})
    return out


def calendar_upcoming(db, today: date, days: int = 105, back: int = 7) -> dict:
    rows = [decorate_event(e, today) for e in
            store.list_events(db, (today - timedelta(days=back)).isoformat(), (today + timedelta(days=days)).isoformat())]
    clusters = find_clusters(rows)
    for i, c in enumerate(clusters):
        for e in rows:
            if e["id"] in c["ids"]:
                e["cluster"] = i
    months = []
    for e in rows:
        ym = e["event_date"][:7]
        if not months or months[-1]["ym"] != ym:
            months.append({"ym": ym, "label": f"{int(ym[:4])}년 {int(ym[5:])}월", "events": []})
        months[-1]["events"].append(e)
    return {"months": months, "clusters": clusters, "count": len(rows)}


def calendar_month(db, ym: str, today: date) -> dict:
    y, m = int(ym[:4]), int(ym[5:7])
    first = date(y, m, 1)
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    start = first - timedelta(days=(first.weekday() + 1) % 7)       # 일요일 시작
    events = [decorate_event(e, today) for e in store.list_events(db, start.isoformat(), (nxt + timedelta(days=7)).isoformat())]
    by_date = {}
    for e in events:
        by_date.setdefault(e["event_date"], []).append(e)
    weeks, d = [], start
    while d < nxt or d.weekday() != 6:
        if d.weekday() == 6:
            weeks.append([])
        weeks[-1].append({"date": d, "in_month": d.month == m, "today": d == today, "events": by_date.get(d.isoformat(), [])})
        d += timedelta(days=1)
    prev_m = date(y - (m == 1), (m - 2) % 12 + 1, 1)
    return {"ym": ym, "label": f"{y}년 {m}월", "weeks": weeks, "prev": prev_m.strftime("%Y-%m"), "next": nxt.strftime("%Y-%m")}


# -- 홈 --------------------------------------------------------------------------------

def market_context(db, today: date) -> dict:
    since = (today - timedelta(days=365 * 3 + 10)).isoformat()
    keys = list(US_TILES + JP_TILES) + ["us_policy_upper", "us_policy_lower", "jp_policy"]
    s = store.load_series(db, keys, since)
    events = store.list_events(db, (today - timedelta(days=200)).isoformat(), (today + timedelta(days=200)).isoformat())

    up, lo = _last(s["us_policy_upper"]), _last(s["us_policy_lower"])
    us_rate = f"{lo['v']:.2f}–{up['v']:.2f}%" if up and lo else "-"
    jp = _last(s["jp_policy"])
    us = policy_block(events, "FOMC", us_rate, up["d"] if up else None, today)
    jpb = policy_block(events, "BOJ", f"{jp['v']:.2f}%" if jp else "입력 필요", jp["d"] if jp else None, today)
    # FRED 는 결정 다음 날부터 새 범위를 싣는다 — 결정 결과 문구('→ 3.75~4.00%')가 더 최신이면 그 값을 쓴다
    last = us["last"]
    m = RANGE_RE.search((last or {}).get("result") or "")
    if m and (not up or last["event_date"] >= up["d"]):
        us["rate_text"], us["as_of"], us["pending_fred"] = f"{float(m[1]):.2f}–{float(m[2]):.2f}%", last["event_date"], True
    return {
        "us": {"policy": us, "tiles": [tile(k, s[k], today) for k in US_TILES]},
        "jp": {"policy": jpb, "tiles": [tile(k, s[k], today) for k in JP_TILES]},
        "spreads": [
            spread_chip("미 2s10s", _spread(s["ust_10y"], s["ust_2y"]), "curve"),
            spread_chip("미 10s30s", _spread(s["ust_30y"], s["ust_10y"]), "curve"),
            spread_chip("일 2s10s", _spread(s["jgb_10y"], s["jgb_2y"]), "curve"),
            spread_chip("미·일 10년 금리차", _latest_diff(s["ust_10y"], s["jgb_10y"]), "carry"),
        ],
    }


def _latest_diff(a: list, b: list) -> list:
    """나라가 달라 휴일이 어긋난다 — 각자 최신값끼리 뺀다."""
    return [a[-1]["v"] - b[-1]["v"]] if a and b else []


def jobs_summary(jobs: dict, now: datetime) -> list:
    out = []
    for name, label in JOB_LABEL.items():
        j = jobs.get(name)
        if not j:
            out.append({"name": name, "label": label, "ok": None, "ago": "기록 없음", "error": None})
            continue
        mins = int((now - datetime.fromisoformat(j["at"])).total_seconds() // 60)
        ago = "방금" if mins < 1 else f"{mins}분 전" if mins < 90 else f"{mins // 60}시간 전" if mins < 2880 else f"{mins // 1440}일 전"
        out.append({"name": name, "label": label, "ok": j.get("ok"), "ago": ago, "error": j.get("error")})
    return out


def home_context(db, now: datetime = None) -> dict:
    now = now or datetime.now(timezone.utc)
    today = kst_today(now)
    sc = compute_scenarios(db, today)
    soxx = store.load_series(db, ("soxx",), (today - timedelta(days=380)).isoformat())["soxx"]
    cur = soxx[-1]["v"] if soxx else None
    high = max(p["v"] for p in soxx[-252:]) if soxx else None
    scenarios = sc["scenarios"]
    top = next((x for x in scenarios if x["code"] == sc["result"]["top"]), None)
    ev = sc["result"]["ev"]
    return {
        "today": today, "now_kst": now.astimezone(KST),
        "soxx": {"price": cur, "as_of": soxx[-1]["d"] if soxx else None, "high": high,
                 "dd": (cur / high - 1) * 100 if cur and high else None, "spark": spark([p["v"] for p in soxx[-120:]], 160, 36)},
        "scen": sc, "top": top, "ev": ev, "ev_vs": (ev / cur - 1) * 100 if ev and cur else None,
        "ruler": price_ruler(scenarios, cur, ev),
        "market": market_context(db, today),
        "calendar": calendar_upcoming(db, today),
        "jobs": jobs_summary(store.load_jobs(db), now),
    }
