"""매크로 데이터 소스 — 시계열. 수동 입력은 없다: 네 지표(BOJ 기준금리·일본 근원 CPI·반도체 EPS 추정치·DRAM 현물가)도 여기서 받는다.

- FRED: 미 기준금리·국채·근원 CPI (키 필요)          - MOF(일본 재무성): JGB 2/10/30년
- Yahoo chart: DXY·USD/JPY·SOXX·SPY                  - Yahoo quoteSummary: 반도체 EPS 추정치 추이·실적 발표일 (크럼 필요)
- BIS: 중앙은행 정책금리 일별 (BOJ 기준금리)         - 통계국(stat.go.jp): 일본 전국 CPI 전년비
- capitalandcompute JSON → DRAMeXchange(폴백): DRAM 현물가
캘린더(FOMC·BOJ·일본 CPI 일정·실적일)는 calendar_sources.py.

파서는 네트워크 없이 문자열·dict 만 받는다(테스트). fetch_* 는 재시도·백오프 뒤에도 실패하면 SourceError 를 던지고,
오류 문구에는 URL 쿼리(api_key)를 싣지 않는다.

반환 형식: [(series_key, 'YYYY-MM-DD', value)] — 날짜는 그 시장의 현지 날짜다.
"""

import csv
import logging
import random
import re
import time
from datetime import date, datetime, timedelta, timezone

import requests

from ..config import _CFG

log = logging.getLogger("macro")

# .env 의 ALERT_FRED_API_KEY, 없으면 FRED_API_KEY (같은 .env 를 쓰는 stock-market-monitor 의 키 — FRED 키는 프로젝트마다 다를 이유가 없다).
# 둘 다 없으면 FRED 작업은 건너뛴다
FRED_API_KEY = (_CFG.get("ALERT_FRED_API_KEY") or _CFG.get("FRED_API_KEY") or "").strip()
FRED_BASE = "https://api.stlouisfed.org/fred"
MOF_CURRENT_URL = "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/jgbcme.csv"
MOF_HISTORICAL_URL = "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/historical/jgbcme_all.csv"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
      "Accept": "application/json,text/csv,*/*"}
RETRIES = 3
TIMEOUT = 20

FRED_SERIES = {                 # series_key → FRED series id. 금리는 %, CPI 는 지수(전년비는 화면에서 계산)
    "us_policy_upper": "DFEDTARU",
    "us_policy_lower": "DFEDTARL",
    "ust_2y": "DGS2",
    "ust_10y": "DGS10",
    "ust_30y": "DGS30",
    "us_core_cpi": "CPILFESL",
}
YAHOO_SYMBOLS = {               # series_key → Yahoo 심볼. 일봉 — 오늘 봉은 장중 값이고 장이 끝나면 종가로 덮인다
    "dxy": "DX-Y.NYB",
    "usdjpy": "JPY=X",
    "soxx": "SOXX",
    "spy": "SPY",
}
MOF_COLUMNS = {"2Y": "jgb_2y", "10Y": "jgb_10y", "30Y": "jgb_30y"}
BIS_URL = "https://stats.bis.org/api/v1/data/WS_CBPOL/D.{country}/all"      # 중앙은행 정책금리 일별 (키 불필요)
JP_CPI_URL = "https://www.stat.go.jp/data/cpi/sokuhou/tsuki/index-z.html"   # 총무성 통계국 — 전국 최신 월 요약
DRAM_JSON_URL = "https://capitalandcompute.net/memory-prices.json"          # TrendForce 현물가·BLS PPI 를 모은 공개 JSON
DRAMEXCHANGE_URL = "https://www.dramexchange.com/"                          # 폴백 — 원천(TrendForce) 시세표
YAHOO_SUMMARY_URL = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
YAHOO_CRUMB_URL = "https://query1.finance.yahoo.com/v1/test/getcrumb"
# 반도체 EPS 추정치 바스켓 — 워치리스트 선행 바스켓과 같다. 실적 발표일도 여기서 받는다.
SEMIS = {"NVDA": "엔비디아", "AVGO": "브로드컴", "TSM": "TSMC", "MU": "마이크론", "AMD": "AMD"}
EPS_DIR_PCT = 0.5      # +1년 EPS 추정치 30일 변화율(중앙값)이 이 이상이면 상향, 음수면 하향
DRAM_DIR_PCT = 1.0     # DRAM 현물가 30일 변화율이 이 이상이면 강세, 이 아래면 하락 전환
# FRED 발표 일정 → 캘린더. (release_id, kind, 제목, 중요도, 미 동부 발표 시각)
FRED_RELEASES = (
    (10, "CPI", "미국 CPI", 3, "08:30"),
    (50, "JOBS", "미국 고용보고서", 2, "08:30"),
    (54, "PCE", "미국 PCE 물가", 2, "08:30"),
)


class SourceError(RuntimeError):
    pass


def _get(url: str, params: dict = None, attempts: int = RETRIES, sleep=time.sleep) -> requests.Response:
    last = None
    for i in range(attempts):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=TIMEOUT)
            if r.status_code == 200:
                return r
            last = f"HTTP {r.status_code}"
            if r.status_code in (400, 401, 403, 404):       # 다시 해도 같다
                break
        except requests.RequestException as e:
            last = type(e).__name__
        if i < attempts - 1:
            sleep(min(8.0, 2 ** i) + random.random() * 0.5)
    raise SourceError(f"{url.split('?')[0]} 실패: {last}")


# -- FRED ------------------------------------------------------------------------

def parse_fred_observations(payload: dict, series_key: str) -> list:
    out = []
    for o in payload.get("observations") or []:
        raw = str(o.get("value", "")).strip()
        if raw in ("", ".", "NA", "N/A"):
            continue
        try:
            out.append((series_key, str(o["date"])[:10], float(raw)))
        except (KeyError, ValueError):
            continue
    return out


def fetch_fred(series_key: str, start: date, api_key: str = None) -> list:
    key = api_key or FRED_API_KEY
    if not key:
        raise SourceError("ALERT_FRED_API_KEY·FRED_API_KEY 가 없다")
    r = _get(f"{FRED_BASE}/series/observations", {"series_id": FRED_SERIES[series_key], "api_key": key, "file_type": "json",
                                                  "observation_start": start.isoformat()})
    return parse_fred_observations(r.json(), series_key)


def parse_fred_release_dates(payload: dict) -> list:
    return sorted({str(d["date"])[:10] for d in payload.get("release_dates") or [] if d.get("date")})


def fetch_fred_release_dates(release_id: int, start: date, end: date, api_key: str = None) -> list:
    key = api_key or FRED_API_KEY
    if not key:
        raise SourceError("ALERT_FRED_API_KEY·FRED_API_KEY 가 없다")
    r = _get(f"{FRED_BASE}/release/dates", {"release_id": release_id, "api_key": key, "file_type": "json",
                                            "realtime_start": start.isoformat(), "realtime_end": end.isoformat(),
                                            "include_release_dates_with_no_data": "true", "sort_order": "asc"})
    return [d for d in parse_fred_release_dates(r.json()) if start.isoformat() <= d <= end.isoformat()]


# -- MOF (JGB) -------------------------------------------------------------------

def _mof_date(value: str):
    parts = value.strip().replace("-", "/").split("/")
    if len(parts) != 3:
        return None
    try:
        return date(int(parts[0]), int(parts[1]), int(parts[2])).isoformat()
    except ValueError:
        return None


def parse_jgb_csv(text: str, since: str = None) -> list:
    """재무성 국채 금리 CSV. 머리 줄 위치가 파일마다 달라 'Date' 와 만기 열이 있는 줄을 찾는다."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    header_at = next((i for i, ln in enumerate(lines[:10])
                      if "Date" in [c.strip() for c in ln.split(",")] and any(c.strip() in MOF_COLUMNS for c in ln.split(","))), None)
    if header_at is None:
        return []
    reader = csv.reader(lines[header_at:])
    header = [c.strip() for c in next(reader)]
    di = header.index("Date")
    cols = {key: header.index(name) for name, key in MOF_COLUMNS.items() if name in header}
    out = []
    for row in reader:
        if len(row) <= di:
            continue
        d = _mof_date(row[di])
        if d is None or (since and d < since):
            continue
        for key, ci in cols.items():
            raw = row[ci].strip() if ci < len(row) else ""
            try:
                out.append((key, d, float(raw)))
            except ValueError:
                continue
    return out


def fetch_jgb(since: date, history: bool = False) -> list:
    """이번 달 CSV. history 면 1974년부터의 전체 CSV(약 1MB)도 합친다 — 백필용."""
    rows = {}
    if history:
        for k, d, v in parse_jgb_csv(_get(MOF_HISTORICAL_URL).content.decode("latin-1"), since.isoformat()):
            rows[(k, d)] = v
    for k, d, v in parse_jgb_csv(_get(MOF_CURRENT_URL).content.decode("latin-1"), since.isoformat()):
        rows[(k, d)] = v
    return [(k, d, v) for (k, d), v in sorted(rows.items(), key=lambda x: (x[0][1], x[0][0]))]


# -- Yahoo -----------------------------------------------------------------------

def parse_yahoo_chart(payload: dict, series_key: str) -> list:
    """v8 chart 일봉 → 거래소 현지 날짜별 종가. 같은 날짜가 두 번 오면(오늘 봉) 뒤의 값."""
    try:
        res = payload["chart"]["result"][0]
    except (KeyError, IndexError, TypeError):
        return []
    stamps = res.get("timestamp") or []
    closes = ((res.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    offset = timedelta(seconds=int((res.get("meta") or {}).get("gmtoffset") or 0))
    by_date = {}
    for ts, c in zip(stamps, closes):
        if c is None:
            continue
        d = (datetime.fromtimestamp(int(ts), timezone.utc) + offset).date().isoformat()
        by_date[d] = round(float(c), 6)
    meta = res.get("meta") or {}
    price, at = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
    if price is not None and at:                     # 일봉 배열보다 최신인 장중 가격
        d = (datetime.fromtimestamp(int(at), timezone.utc) + offset).date().isoformat()
        if not by_date or d >= max(by_date):
            by_date[d] = round(float(price), 6)
    return [(series_key, d, v) for d, v in sorted(by_date.items())]


def parse_yahoo_quote(payload: dict, series_key: str) -> list:
    try:
        q = payload["quoteResponse"]["result"][0]
        price, at = q["regularMarketPrice"], int(q["regularMarketTime"])
        offset = timedelta(milliseconds=int(q.get("gmtOffSetMilliseconds") or 0))
    except (KeyError, IndexError, TypeError, ValueError):
        return []
    d = (datetime.fromtimestamp(at, timezone.utc) + offset).date().isoformat()
    return [(series_key, d, round(float(price), 6))]


def fetch_yahoo(series_key: str, range_: str = "1mo") -> list:
    symbol = YAHOO_SYMBOLS[series_key]
    try:
        rows = parse_yahoo_chart(_get(YAHOO_CHART_URL.format(symbol=symbol), {"range": range_, "interval": "1d"}).json(), series_key)
        if rows:
            return rows
    except (SourceError, ValueError) as e:
        log.info("Yahoo chart %s 실패 — quote 로 대체: %s", symbol, e)
    rows = parse_yahoo_quote(_get(YAHOO_QUOTE_URL, {"symbols": symbol}).json(), series_key)
    if not rows:
        raise SourceError(f"Yahoo {symbol} 값 없음")
    return rows


# -- BIS 정책금리 ------------------------------------------------------------------

def parse_bis_csv(text: str, series_key: str) -> list:
    """BIS WS_CBPOL CSV → 일별 정책금리. 값이 그대로 %."""
    out = []
    for row in csv.DictReader(text.splitlines()):
        d, v = (row.get("TIME_PERIOD") or "").strip(), (row.get("OBS_VALUE") or "").strip()
        if not d or not v:
            continue
        try:
            out.append((series_key, d, float(v)))
        except ValueError:
            continue
    return sorted(out, key=lambda r: r[1])


def fetch_bis_policy(country: str, series_key: str, start: date) -> list:
    r = _get(BIS_URL.format(country=country), {"format": "csv", "startPeriod": start.isoformat()[:7]})
    rows = parse_bis_csv(r.text, series_key)
    if not rows:
        raise SourceError(f"BIS {country} 정책금리 값 없음")
    return rows


# -- 일본 전국 CPI (총무성 통계국) ------------------------------------------------------

def parse_jp_cpi(html: str) -> list:
    """'2026年（令和8年）8月分' 과 '(2) 生鮮食品を除く総合指数 は102.0 前年同月比は1.7％の上昇' 을 읽는다.

    지수 자체가 아니라 전년동월비를 저장한다 — 기준연도 개편(2025년 기준)이 있어도 이어진다.
    """
    text = " ".join(re.sub(r"<[^>]+>", " ", html).split())
    m = re.search(r"(20\d\d)年(?:（[^）]*）)?\s*(\d{1,2})月分", text)
    if not m:
        return []
    obs_date = f"{int(m[1])}-{int(m[2]):02d}-01"
    prefix_key = {"": "jp_cpi_yoy", "生鮮食品を除く": "jp_core_cpi_yoy", "生鮮食品及びエネルギーを除く": "jp_core_core_cpi_yoy"}
    out = {}
    for hit in re.finditer(r"([぀-ヿ一-鿿]*?)総合指数\s*は[^前]{0,40}前年同月比は\s*([+-]?[\d.]+)\s*[%％]の(上昇|下落)", text):
        key = prefix_key.get(hit[1])
        if key and key not in out:
            out[key] = (key, obs_date, float(hit[2]) * (-1 if hit[3] == "下落" else 1))
    return list(out.values())


def fetch_jp_cpi() -> list:
    r = _get(JP_CPI_URL)
    r.encoding = r.apparent_encoding or "shift_jis"
    rows = parse_jp_cpi(r.text)
    if not rows:
        raise SourceError("통계국 CPI 요약에서 전년동월비를 찾지 못했다")
    return rows


# -- DRAM 현물가 --------------------------------------------------------------------

def parse_dram_json(payload: dict) -> list:
    """capitalandcompute JSON → DDR4 현물가(칩당 USD) 시계열."""
    for s in payload.get("series") or []:
        if s.get("id") in ("ddr4Spot", "ddr5Spot"):
            return [("dram_spot", str(p["date"])[:10], float(p["value"])) for p in s.get("points") or [] if p.get("value") is not None]
    return []


def parse_dramexchange(html: str, today: str) -> list:
    """폴백 — 시세표에서 주력 칩 'DDR4 8Gb (1Gx8) 3200' 의 세션 평균가 (JSON 쪽과 같은 품목)."""
    text = " ".join(re.sub(r"<[^>]+>", " ", html).split())
    m = re.search(r"DDR4 8Gb \(1Gx8\) 3200\s+[\d.]+\s+[\d.]+\s+[\d.]+\s+[\d.]+\s+([\d.]+)", text)
    return [("dram_spot", today, float(m[1]))] if m else []


def fetch_dram(today: date) -> list:
    try:
        rows = parse_dram_json(_get(DRAM_JSON_URL).json())
        if rows:
            return rows
    except (SourceError, ValueError) as e:
        log.info("DRAM JSON 실패 — DRAMeXchange 로 대체: %s", e)
    rows = parse_dramexchange(_get(DRAMEXCHANGE_URL).text, today.isoformat())
    if not rows:
        raise SourceError("DRAM 현물가를 찾지 못했다")
    return rows


def direction(points: list, days: int = 30, threshold: float = 1.0):
    """[(key, date, value)] → (변화율 %, 방향 ±1·0). days 전보다 오래된 값이 없으면 (None, None)."""
    pts = sorted((d, v) for _, d, v in points)
    if len(pts) < 2:
        return None, None
    last_d, last_v = pts[-1]
    cutoff = (date.fromisoformat(last_d) - timedelta(days=days)).isoformat()
    older = [(d, v) for d, v in pts if d <= cutoff] or [pts[0]]
    base = older[-1][1]
    if not base:
        return None, None
    pct = (last_v / base - 1) * 100
    return pct, 1 if pct >= threshold else -1 if pct <= -threshold else 0


# -- Yahoo quoteSummary (EPS 추정치 추이·실적 발표일) --------------------------------------

_crumb = {"value": None, "session": None}


def _yahoo_session():
    """쿠키 + 크럼. 401 이 나면 worker 가 다시 부른다."""
    if _crumb["session"] is None:
        s = requests.Session()
        s.headers.update(UA)
        s.get("https://fc.yahoo.com", timeout=TIMEOUT)
        _crumb["session"] = s
        _crumb["value"] = s.get(YAHOO_CRUMB_URL, timeout=TIMEOUT).text.strip()
    return _crumb["session"], _crumb["value"]


def reset_yahoo_session():
    _crumb["session"], _crumb["value"] = None, None


def fetch_quote_summary(symbol: str) -> dict:
    for attempt in (1, 2):
        s, crumb = _yahoo_session()
        r = s.get(YAHOO_SUMMARY_URL.format(symbol=symbol), params={"modules": "earningsTrend,calendarEvents", "crumb": crumb},
                  timeout=TIMEOUT)
        if r.status_code == 200:
            return r.json()
        reset_yahoo_session()               # 크럼 만료 — 한 번 다시 받는다
        if attempt == 2:
            raise SourceError(f"Yahoo quoteSummary {symbol} 실패: HTTP {r.status_code}")
    return {}


def _raw(node):
    return node.get("raw") if isinstance(node, dict) else node


def parse_eps_trend(payload: dict, period: str = "+1y") -> dict:
    """{'current':…, '30daysAgo':…, '90daysAgo':…} — 없으면 빈 dict."""
    try:
        trend = payload["quoteSummary"]["result"][0]["earningsTrend"]["trend"]
    except (KeyError, IndexError, TypeError):
        return {}
    for t in trend:
        if t.get("period") == period:
            eps = t.get("epsTrend") or {}
            got = {k: _raw(v) for k, v in eps.items() if isinstance(_raw(v), (int, float))}
            return got if got.get("current") else {}
    return {}


def parse_earnings_date(payload: dict) -> tuple:
    """(YYYY-MM-DD, 확정 여부). 없으면 (None, False)."""
    try:
        cal = payload["quoteSummary"]["result"][0]["calendarEvents"]["earnings"]
        d = (cal.get("earningsDate") or [{}])[0]
    except (KeyError, IndexError, TypeError):
        return None, False
    fmt = d.get("fmt") if isinstance(d, dict) else None
    return (fmt[:10] if fmt else None), not cal.get("isEarningsDateEstimate", True)
