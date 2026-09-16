"""장 운영 시간 — 캘린더 파싱과 고정 시간 폴백."""
from datetime import datetime

import pytest

import alertbot.market_hours as mh
from tests.conftest import TZ

KR_OPEN = {"today": {"date": "2026-03-25", "integrated": {
    "preMarket": {"startTime": "08:00", "endTime": "08:50"},
    "regularMarket": {"startTime": "09:00", "singlePriceAuctionStartTime": "15:20", "endTime": "15:30"}}}}
KR_HOLIDAY = {"today": {"date": "2026-03-01", "integrated": None}}
US_LIST = {"days": [{"date": "2026-03-25", "regularMarketSession": {
    "startDateTime": "2026-03-25T13:30:00Z", "endDateTime": "2026-03-25T20:00:00Z"}}]}
US_EARLY = {"today": {"regularMarketSession": {"start": "2026-11-27T14:30:00Z", "end": "2026-11-27T18:00:00Z"}}}
US_HOLIDAY = {"days": [{"date": "2026-07-03", "regularMarketSession": None}]}
# 실제 토스 응답(2026-09-16 조회): 미국 항목인데 date 는 한국 날짜, 시각은 KST. 09-16 항목이 곧 미국 09-15 장이다.
def _us_kst_day(date, next_date):
    return {"date": date,
            "preMarket": {"startTime": f"{date}T17:00:00.000+09:00", "endTime": f"{date}T22:30:00.000+09:00"},
            "regularMarket": {"startTime": f"{date}T22:30:00.000+09:00", "endTime": f"{next_date}T05:00:00.000+09:00"}}
US_TOSS = {"today": _us_kst_day("2026-09-16", "2026-09-17"),
           "previousBusinessDay": _us_kst_day("2026-09-15", "2026-09-16"),
           "nextBusinessDay": _us_kst_day("2026-09-17", "2026-09-18")}


@pytest.mark.parametrize("data, market, date, expect", [
    (KR_OPEN, "KR", "2026-03-25", {"closed": False, "open": 540, "close": 930}),
    (KR_HOLIDAY, "KR", "2026-03-01", {"closed": True}),
    (KR_OPEN, "KR", "2026-03-26", None),                       # 날짜 불일치 → 신뢰하지 않음
    (US_LIST, "US", "2026-03-25", {"closed": False, "open": 570, "close": 960}),   # 09:30~16:00 EDT
    (US_EARLY, "US", "2026-11-27", {"closed": False, "open": 570, "close": 780}),  # 조기폐장 13:00 EST
    (US_HOLIDAY, "US", "2026-07-03", {"closed": True}),
    (US_TOSS, "US", "2026-09-15", {"closed": False, "open": 570, "close": 960}),   # 미국 09-15 저녁(KST 09-16 새벽 전) → 전영업일 항목
    (US_TOSS, "US", "2026-09-16", {"closed": False, "open": 570, "close": 960}),   # 미국 09-16 → today 항목 (KST 09-16 22:30)
    (US_TOSS, "US", "2026-09-18", None),                                          # 목록 밖 날짜
    ({"weird": 1}, "US", "2026-03-25", None),
    (None, "KR", "2026-03-25", None),
])
def test_parse_calendar(data, market, date, expect):
    assert mh.parse_calendar(data, market, date) == expect


class StubClient:
    def __init__(self, payload=None, raise_=False):
        self.payload, self.raise_, self.calls = payload, raise_, 0

    def get_market_calendar(self, market):
        self.calls += 1
        if self.raise_:
            raise RuntimeError("boom")
        return self.payload


def at(monkeypatch, market, y, m, d, hh, mm):
    fixed = datetime(y, m, d, hh, mm, tzinfo=TZ[market])
    monkeypatch.setattr(mh, "now_local", lambda mk: fixed.astimezone(TZ[mk]))


def test_fixed_fallback_kr_boundaries(monkeypatch):
    hours = mh.MarketHours(StubClient(None))
    cases = [(8, 49, False), (8, 50, True), (12, 0, True), (15, 40, True), (15, 41, False)]
    for hh, mm, expect in cases:
        at(monkeypatch, "KR", 2026, 3, 25, hh, mm)          # 수요일
        assert hours.market_open("KR") is expect, (hh, mm)
    at(monkeypatch, "KR", 2026, 3, 28, 10, 0)               # 토요일
    assert hours.market_open("KR") is False
    assert hours.cache["KR"]["source"] == "고정"


def test_near_close_fixed_kr(monkeypatch):
    hours = mh.MarketHours()
    for hh, mm, expect in [(14, 59, False), (15, 0, True), (15, 29, True), (15, 30, False)]:
        at(monkeypatch, "KR", 2026, 3, 25, hh, mm)
        assert hours.near_close("KR") is expect, (hh, mm)


def test_calendar_holiday_and_cache(monkeypatch):
    stub = StubClient(KR_HOLIDAY)
    hours = mh.MarketHours(stub)
    at(monkeypatch, "KR", 2026, 3, 1, 10, 0)
    assert hours.market_open("KR") is False
    assert hours.near_close("KR") is False
    hours.market_open("KR")
    assert stub.calls == 1                                   # 같은 날은 다시 묻지 않는다


def test_calendar_early_close_us(monkeypatch):
    hours = mh.MarketHours(StubClient(US_EARLY))
    at(monkeypatch, "US", 2026, 11, 27, 12, 45)              # 13:00 조기폐장 15분 전
    assert hours.market_open("US") is True
    assert hours.near_close("US") is True
    at(monkeypatch, "US", 2026, 11, 27, 13, 11)
    assert hours.market_open("US") is False
    assert hours.cache["US"]["source"] == "캘린더"


def test_premarket_us_only(monkeypatch):
    hours = mh.MarketHours()
    at(monkeypatch, "US", 2026, 3, 25, 8, 30)
    assert hours.market_premarket("US") is True
    assert hours.market_open("US") is False
    at(monkeypatch, "US", 2026, 3, 25, 9, 20)
    assert hours.market_premarket("US") is False
    assert hours.market_open("US") is True
    at(monkeypatch, "KR", 2026, 3, 25, 8, 30)
    assert hours.market_premarket("KR") is False


def test_client_error_falls_back(monkeypatch):
    hours = mh.MarketHours(StubClient(raise_=True))
    at(monkeypatch, "US", 2026, 3, 25, 10, 0)
    assert hours.market_open("US") is True
    assert hours.cache["US"]["source"] == "고정"
