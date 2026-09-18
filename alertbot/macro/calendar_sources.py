"""캘린더 자동 수집 — 이벤트는 공식 페이지에서 주기적으로 받아 온다(수동 입력 없음).

- FOMC 일정·점도표 여부: federalreserve.gov/monetarypolicy/fomccalendars.htm
- FOMC 결과: 그 회의의 성명 페이지에서 목표 범위를 읽는다 ("… to 3-3/4 to 4 percent")
- BOJ 일정·전망보고서 여부: boj.or.jp/en/mopo/mpmsche_minu (BOJ 결과는 BIS 정책금리 변화로 판정한다 — sources.fetch_bis_policy)
- 일본 전국 CPI 발표일: 통계국 공표 예정표
- 실적 발표일: Yahoo quoteSummary calendarEvents (sources.SEMIS)

제목은 항상 같은 문구를 쓴다 — 저장 쪽이 (종류·국가·날짜·제목)으로 중복을 거른다.
"""

import logging
import re
from datetime import date

from .sources import SEMIS, SourceError, _get, fetch_quote_summary, parse_earnings_date

log = logging.getLogger("macro")

FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
FOMC_STATEMENT_URL = "https://www.federalreserve.gov/newsevents/pressreleases/monetary{stamp}a.htm"
BOJ_URL = "https://www.boj.or.jp/en/mopo/mpmsche_minu/index.htm"
JP_CPI_SCHEDULE_URL = "https://www.stat.go.jp/english/data/cpi/1582.html"

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"], 1)}
MONTHS.update({m[:3]: i for m, i in list(MONTHS.items())})
MONTHS["sept"] = 9


def _month(name: str):
    return MONTHS.get(re.sub(r"[^a-z]", "", (name or "").lower())[:9]) or MONTHS.get(re.sub(r"[^a-z]", "", (name or "").lower())[:3])


def _text(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


# -- FOMC ---------------------------------------------------------------------------

def parse_fomc_calendar(html: str) -> list:
    """[{event_date, first_day, sep, stamp}] — event_date 는 결정일(마지막 날)."""
    out = []
    for year_block in re.split(r'<div class="panel panel-default">', html)[1:]:
        ym = re.search(r"(20\d\d) FOMC Meetings", year_block)
        if not ym:
            continue
        year = int(ym[1])
        for row in re.split(r'class="[^"]*row fomc-meeting', year_block)[1:]:
            mm = re.search(r'fomc-meeting__month[^>]*>\s*<strong>([^<]+)</strong>', row)
            dd = re.search(r'fomc-meeting__date[^>]*>\s*([^<]+?)\s*</div>', row)
            if not mm or not dd:
                continue
            months = [_month(x) for x in mm[1].split("/")]
            days = re.findall(r"\d{1,2}", dd[1])
            if not days or months[0] is None:
                continue
            start_m, end_m = months[0], months[-1] or months[0]
            end_y = year + 1 if end_m < start_m else year
            try:
                first = date(year, start_m, int(days[0]))
                last = date(end_y, end_m, int(days[-1]))
            except ValueError:
                continue
            stamp = re.search(r"/newsevents/pressreleases/monetary(\d{8})a\.htm", row)
            out.append({"event_date": last.isoformat(), "first_day": first.isoformat(), "sep": "*" in dd[1],
                        "stamp": stamp[1] if stamp else None})
    return out


def fomc_events(html: str) -> list:
    evs = []
    for m in parse_fomc_calendar(html):
        first = date.fromisoformat(m["first_day"])
        evs.append({"event_date": m["event_date"], "time_local": "14:00", "country": "US", "kind": "FOMC",
                    "title": "FOMC 금리 결정" + (" + 점도표" if m["sep"] else ""), "importance": 3, "source": "auto",
                    "note": f"{first:%m-%d} 시작 · 성명 14:00 ET" + (" · SEP(점도표)" if m["sep"] else " · 성명만")})
    return evs


def fetch_fomc_calendar() -> list:
    html = _get(FOMC_URL).text
    evs = fomc_events(html)
    if not evs:
        raise SourceError("FOMC 일정을 읽지 못했다")
    return evs


FRACTIONS = {"": 0.0, "1/4": 0.25, "1/2": 0.5, "3/4": 0.75, "1/8": 0.125, "3/8": 0.375, "5/8": 0.625, "7/8": 0.875}


def _rate(token: str):
    """'3-3/4' → 3.75, '4' → 4.0."""
    m = re.fullmatch(r"(\d+)(?:-(\d/\d))?", token.strip())
    if not m:
        return None
    return int(m[1]) + FRACTIONS.get(m[2] or "", 0.0)


def parse_fomc_statement(html: str) -> dict:
    """성명 본문 → {action: raise|lower|maintain, low, high}. 못 읽으면 빈 dict."""
    text = _text(html)
    m = re.search(r"decided to (raise|lower|maintain) the target range for the federal funds rate"
                  r"(?:[^.]*?)\bto (\d+(?:-\d/\d)?) to (\d+(?:-\d/\d)?) percent", text)
    if not m:
        m2 = re.search(r"target range for the federal funds rate at (\d+(?:-\d/\d)?) to (\d+(?:-\d/\d)?) percent", text)
        if not m2:
            return {}
        return {"action": "maintain", "low": _rate(m2[1]), "high": _rate(m2[2])}
    return {"action": m[1], "low": _rate(m[2]), "high": _rate(m[3])}


FOMC_FLAG = {"raise": "hike", "lower": "cut", "maintain": "hold"}
FOMC_WORD = {"raise": "인상", "lower": "인하", "maintain": "동결"}


def fetch_fomc_result(event_date: str) -> dict:
    """결정일의 성명 → {result, flag}. 성명이 아직 없으면 빈 dict."""
    stamp = event_date.replace("-", "")
    try:
        html = _get(FOMC_STATEMENT_URL.format(stamp=stamp), attempts=1).text
    except SourceError:
        return {}
    st = parse_fomc_statement(html)
    if not st or st["low"] is None:
        return {}
    word = FOMC_WORD[st["action"]]
    size = ""
    if st["action"] != "maintain":
        m = re.search(r"by (\d/\d|\d+(?:-\d/\d)?) percentage point", _text(html))
        size = f"{round(_rate(m[1]) * 100) if m and _rate(m[1]) else 25}bp " if m else "25bp "
    return {"result": f"{size}{word} → {st['low']:.2f}~{st['high']:.2f}%", "flag": FOMC_FLAG[st["action"]]}


# -- BOJ ----------------------------------------------------------------------------

def parse_boj_schedule(html: str) -> list:
    """[{event_date, first_day, outlook}] — 연도는 표 캡션('Table : 2026')에서."""
    out = []
    for table in re.findall(r"<table.*?</table>", html, re.S):
        cap = re.search(r"<caption[^>]*>(.*?)</caption>", table, re.S)
        ym = re.search(r"(20\d\d)", _text(cap[1]) if cap else "")
        if not ym:
            continue
        year = int(ym[1])
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S):
            cells = [_text(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)]
            if len(cells) < 2:
                continue
            m = re.match(r"([A-Za-z]+)\.?\s*(\d{1,2})[^,]*,\s*(?:([A-Za-z]+)\.?\s*)?(\d{1,2})", cells[0])
            if not m or _month(m[1]) is None:
                continue
            start_m = _month(m[1])
            end_m = _month(m[3]) if m[3] else start_m
            end_y = year + 1 if end_m < start_m else year
            try:
                first = date(year, start_m, int(m[2]))
                last = date(end_y, end_m, int(m[4]))
            except ValueError:
                continue
            out.append({"event_date": last.isoformat(), "first_day": first.isoformat(), "outlook": cells[1] not in ("-", "")})
    return out


def boj_events(html: str) -> list:
    evs = []
    for m in parse_boj_schedule(html):
        first = date.fromisoformat(m["first_day"])
        evs.append({"event_date": m["event_date"], "time_local": None, "country": "JP", "kind": "BOJ",
                    "title": "BOJ 금융정책결정회의" + (" + 전망보고서" if m["outlook"] else ""), "importance": 3, "source": "auto",
                    "note": f"{first:%m-%d} 시작 · 결정 시각 미정(보통 정오 전후 JST) · 총재 회견 15:30"
                            + (" · 전망보고서 당일 공표" if m["outlook"] else "")})
    return evs


def fetch_boj_calendar() -> list:
    evs = boj_events(_get(BOJ_URL).text)
    if not evs:
        raise SourceError("BOJ 일정을 읽지 못했다")
    return evs


# -- 일본 전국 CPI 발표일 ----------------------------------------------------------------

def parse_jp_cpi_schedule(html: str) -> list:
    """[(발표일, '8월분')] — 표의 앞 두 칸(전국 조사월·공표일). 연도는 나오는 곳에서 이어받는다."""
    out, year = [], None
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        cells = [_text(c).replace("\xa0", " ") for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)]
        if len(cells) < 2:
            continue
        survey, release = cells[0], cells[1]
        sm = re.match(r"([A-Za-z]+),?\s*(20\d\d)?", survey)
        rm = re.match(r"([A-Za-z]+)\s+(\d{1,2})(?:,\s*(20\d\d))?", release)
        if not sm or not rm or _month(sm[1]) is None or _month(rm[1]) is None:
            continue
        year = int(rm[3]) if rm[3] else year
        if year is None:
            continue
        try:
            d = date(year, _month(rm[1]), int(rm[2]))
        except ValueError:
            continue
        out.append((d.isoformat(), f"{_month(sm[1])}월분"))
    return out


def jp_cpi_events(html: str) -> list:
    return [{"event_date": d, "time_local": "08:30", "country": "JP", "kind": "CPI", "title": "일본 전국 CPI",
             "importance": 2, "source": "auto", "note": f"{label} · 통계국 공표 예정"} for d, label in parse_jp_cpi_schedule(html)]


def fetch_jp_cpi_calendar() -> list:
    evs = jp_cpi_events(_get(JP_CPI_SCHEDULE_URL).text)
    if not evs:
        raise SourceError("일본 CPI 발표 일정을 읽지 못했다")
    return evs


# -- 실적 발표일 ------------------------------------------------------------------------

EARNINGS_IMPORTANCE = {"NVDA": 3, "MU": 2, "TSM": 2, "AVGO": 2, "AMD": 2}
EARNINGS_TIME = {"US": "16:20", "TW": "14:00"}      # 미국은 장 마감 직후, TSMC 는 타이베이 오후 컨퍼런스


def earnings_event(symbol: str, name: str, payload: dict) -> dict:
    d, confirmed = parse_earnings_date(payload)
    if not d:
        return None
    country = "TW" if symbol == "TSM" else "US"
    return {"event_date": d, "time_local": EARNINGS_TIME[country] if confirmed else None, "country": country,
            "kind": "EARNINGS", "title": f"{name} 실적", "importance": EARNINGS_IMPORTANCE.get(symbol, 2), "source": "auto",
            "note": ("확정" if confirmed else "예상일") + f" · Yahoo Finance ({symbol})"}


def fetch_earnings_events() -> list:
    evs, errors = [], []
    for symbol, name in SEMIS.items():
        try:
            ev = earnings_event(symbol, name, fetch_quote_summary(symbol))
            if ev:
                evs.append(ev)
        except Exception as e:                      # 한 종목이 막혀도 나머지는 받는다
            errors.append(f"{symbol}: {e}")
    if errors and not evs:
        raise SourceError("; ".join(errors))
    if errors:
        log.info("실적 발표일 일부 실패: %s", "; ".join(errors))
    return evs
