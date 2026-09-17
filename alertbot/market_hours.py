"""장 운영 시간 — 개장·프리마켓·마감임박 판정.

세션당 한 번 캘린더 API(/api/v1/market-calendar/{KR|US})를 읽어 휴장일과 조기폐장을
반영한다. 요일만 보면 추석·미국 휴일에도 폴링하고 '장 시작' 알림을 보낸다.
캘린더를 못 읽거나 형식을 모르면 고정 시간으로 판단한다
(KR 09:00~15:30, US 09:30~16:00, 앞뒤 10분 여유, 미국 프리마켓 04:00~).
"""

import logging
from datetime import timedelta

from .config import CLOSE_WARN_MIN
from .timeutil import now_local, parse_ts

log = logging.getLogger("scalper")

DEFAULT_MINUTES = {"KR": (9 * 60, 15 * 60 + 30), "US": (9 * 60 + 30, 16 * 60)}
OPEN_MARGIN_MIN = 10            # 개장 직후 봉도 잡도록 앞뒤 여유
PREMARKET_START_MIN = 4 * 60    # 미국 프리마켓 시작 (04:00 ET = 서머타임 KST 17:00). 캘린더에 preMarket 이 있으면 그것을 쓴다


def _to_minutes(value, market: str):
    """'09:00', '09:00:00', ISO datetime 어느 형식이 와도 '자정 이후 분'으로. 실패 시 None."""
    if value is None:
        return None
    s = str(value)
    if "T" in s:
        dt = parse_ts(s, market)
        return dt.hour * 60 + dt.minute if dt else None
    try:
        h, m = s.split(":")[:2]
        return int(h) * 60 + int(m)
    except ValueError:
        return None


def parse_calendar(data, market: str, date: str):
    """캘린더 응답(result 벗긴 것) → {"closed": bool, "open": 분, "close": 분[, "pre": 분]}. 형식 불명이면 None.

    KR: today.integrated 가 null 이면 휴장, 아니면 regularMarket.startTime/endTime.
    US: today/previousBusinessDay/nextBusinessDay 또는 days/marketDays 목록에서 오늘 항목의
        regularMarket(Session) (startTime/endTime, startDateTime/endDateTime, start/end). 세션이 null 이면 휴장.
    오늘 날짜와 맞지 않는 항목은 쓰지 않는다 — 어제 정보로 오늘을 판단하면 안 된다.
    항목의 date 는 토스가 한국 날짜로 붙인다 (미국 09-15 장이 'date: 09-16, 22:30~05:00 KST'). 그래서
    정규장 시작 시각이 있으면 그것을 시장 현지 날짜로 바꿔 대조하고, date 필드는 시각이 없을 때(휴장)만 쓴다.
    """
    if not isinstance(data, dict):
        return None
    entries = []
    for key in ("today", "previousBusinessDay", "nextBusinessDay"):
        if isinstance(data.get(key), dict):
            entries.append(data[key])
    for key in ("days", "marketDays", "calendar"):
        if isinstance(data.get(key), list):
            entries.extend(d for d in data[key] if isinstance(d, dict))

    for d in entries:
        if "integrated" in d:                       # KR 통합거래소 형식
            integrated = d["integrated"]
            closed = integrated is None
            sess = (integrated or {}).get("regularMarket")
        else:
            closed = (("regularMarketSession" in d and d["regularMarketSession"] is None)
                      or d.get("isHoliday") is True)
            sess = d.get("regularMarketSession") or d.get("regularMarket")
        start = end = None
        if isinstance(sess, dict):
            start = sess.get("startDateTime") or sess.get("start") or sess.get("startTime")
            end = sess.get("endDateTime") or sess.get("end") or sess.get("endTime")
        d_date = ""
        if start and "T" in str(start):
            dt = parse_ts(str(start), market)
            d_date = dt.strftime("%Y-%m-%d") if dt else ""
        if not d_date:
            d_date = str(d.get("date") or "")[:10]
        if d_date != date:
            continue
        if closed:
            return {"closed": True}
        o, c = _to_minutes(start, market), _to_minutes(end, market)
        if o is None or c is None:
            return None
        out = {"closed": False, "open": o, "close": c}
        pre = d.get("preMarket") or d.get("preMarketSession")
        p = _to_minutes(pre.get("startTime") or pre.get("startDateTime") or pre.get("start"), market)             if isinstance(pre, dict) else None
        if p is not None and p < o:
            out["pre"] = p
        return out
    return None


class MarketHours:
    """시장별 오늘 운영 정보를 캐시하고 개장/프리마켓/마감임박을 판정한다."""

    def __init__(self, client=None):
        self.client = client          # get_market_calendar(market) 를 가진 객체. None 이면 고정 시간
        self.cache = {}               # market -> {"date", "closed", "open", "close", "source"}

    def info(self, market: str) -> dict:
        t = now_local(market)
        date = t.strftime("%Y-%m-%d")
        cached = self.cache.get(market)
        if cached and cached["date"] == date:
            return cached
        o, c = DEFAULT_MINUTES[market]
        info = {"date": date, "closed": t.weekday() >= 5, "open": o, "close": c, "source": "고정"}
        if market == "US":
            info["pre"] = PREMARKET_START_MIN
        if self.client is not None:
            try:
                raw = self.client.get_market_calendar(market)
                parsed = parse_calendar(raw, market, date)
                if parsed:
                    info.update(parsed)
                    info["source"] = "캘린더"
                elif raw is None:
                    log.warning("캘린더 %s 응답 없음 — 고정 시간으로 판단한다", market)
                else:
                    log.warning("캘린더 %s 형식 불명 — 고정 시간으로 판단한다: %s", market, str(raw)[:300])
            except Exception as e:
                log.warning("캘린더 %s 조회 실패 — 고정 시간으로 판단한다: %s", market, e)
        if info["closed"]:
            log.info("장 운영 %s %s: 휴장 (%s)", market, date, info["source"])
        else:
            log.info("장 운영 %s %s: %02d:%02d~%02d:%02d (%s)", market, date,
                     info["open"] // 60, info["open"] % 60, info["close"] // 60, info["close"] % 60,
                     info["source"])
        self.cache[market] = info
        return info

    @staticmethod
    def _hm(market: str) -> int:
        t = now_local(market)
        return t.hour * 60 + t.minute

    def market_open(self, market: str) -> bool:
        """현지시각 기준. 앞뒤 여유를 둬 개장 직후 봉도 잡는다."""
        info = self.info(market)
        if info["closed"]:
            return False
        hm = self._hm(market)
        return info["open"] - OPEN_MARGIN_MIN <= hm <= info["close"] + OPEN_MARGIN_MIN

    def session_open(self, market: str) -> bool:
        """정규장이 실제로 열려 있는지 (여유 없음). '장 시작'/'장 마감' 알림 시각에 쓴다.

        감시(market_open)는 개장봉을 잡으려 앞뒤 10분 여유를 두지만, 알림까지 그 기준을 따르면
        미국장 '장 시작'이 22:20, '장 마감'이 05:10 KST 에 나간다.
        """
        info = self.info(market)
        if info["closed"]:
            return False
        return info["open"] <= self._hm(market) < info["close"]

    def market_premarket(self, market: str) -> bool:
        """프리마켓 시간대인지 (미국 04:00 ET ~ 개장 여유 직전).

        프리마켓은 유동성이 정규장의 수십 분의 일이라 거래 몇 건으로 RVOL 이
        크게 튄다. 그래서 매수·매도 알림 판단에는 쓰지 않고 30분 프리마켓 분석에만 쓴다.
        한국장 장전 동시호가는 체결 구조가 달라 아예 제외한다.
        """
        if market != "US":
            return False
        info = self.info(market)
        if info["closed"]:
            return False
        return info.get("pre", PREMARKET_START_MIN) <= self._hm(market) < info["open"] - OPEN_MARGIN_MIN

    def near_close(self, market: str) -> bool:
        """마감 CLOSE_WARN_MIN 분 전 여부. 조기폐장이면 캘린더의 마감 시각을 따른다."""
        info = self.info(market)
        if info["closed"]:
            return False
        t = now_local(market)
        close = t.replace(hour=info["close"] // 60, minute=info["close"] % 60, second=0, microsecond=0)
        left = close - t
        return timedelta(0) < left <= timedelta(minutes=CLOSE_WARN_MIN)
