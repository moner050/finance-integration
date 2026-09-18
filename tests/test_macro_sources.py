"""매크로 소스 파서 — 네트워크 없이 FRED·MOF·Yahoo·BIS·통계국·DRAM 응답 모양만."""
from alertbot.macro import sources as S


def test_fred_observations_skip_missing():
    payload = {"observations": [{"date": "2026-09-14", "value": "4.97"}, {"date": "2026-09-15", "value": "."},
                                {"date": "2026-09-16", "value": "5"}]}
    assert S.parse_fred_observations(payload, "ust_10y") == [("ust_10y", "2026-09-14", 4.97), ("ust_10y", "2026-09-16", 5.0)]


def test_fred_release_dates_sorted_unique():
    payload = {"release_dates": [{"date": "2026-10-14"}, {"date": "2026-09-11"}, {"date": "2026-10-14"}]}
    assert S.parse_fred_release_dates(payload) == ["2026-09-11", "2026-10-14"]


MOF = """Interest Rate (September 2026),,,,,,,,,,,,,,,(Unit : %)
Date,1Y,2Y,3Y,4Y,5Y,6Y,7Y,8Y,9Y,10Y,15Y,20Y,25Y,30Y,40Y
2026/9/1,1.527,1.802,1.952,2.14,2.28,2.411,2.559,2.718,2.848,2.987,3.544,3.859,4.143,4.131,4.145
2026/9/2,1.56,-,2.009,2.199,2.332,2.45,2.585,2.743,2.874,3.006,3.554,3.864,4.141,4.122,4.134
,,,,,,,,,,,,,,,
"""


def test_jgb_csv_finds_header_and_skips_blanks():
    rows = S.parse_jgb_csv(MOF)
    assert ("jgb_2y", "2026-09-01", 1.802) in rows and ("jgb_30y", "2026-09-02", 4.122) in rows
    assert not any(k == "jgb_2y" and d == "2026-09-02" for k, d, _ in rows)          # '-' 는 건너뜀
    assert all(d >= "2026-09-02" for _, d, _ in S.parse_jgb_csv(MOF, since="2026-09-02"))
    assert S.parse_jgb_csv("garbage\nno header") == []


def test_yahoo_chart_uses_exchange_date_and_live_price():
    ts = 1789588800            # 2026-09-16 20:00 UTC = 16:00 NY
    payload = {"chart": {"result": [{"meta": {"gmtoffset": -14400, "regularMarketPrice": 503.5, "regularMarketTime": ts + 86400},
                                     "timestamp": [ts - 86400, ts],
                                     "indicators": {"quote": [{"close": [498.85, None]}]}}]}}
    rows = S.parse_yahoo_chart(payload, "soxx")
    assert rows == [("soxx", "2026-09-15", 498.85), ("soxx", "2026-09-17", 503.5)]
    assert S.parse_yahoo_chart({"chart": {"result": None}}, "soxx") == []


def test_yahoo_quote_fallback():
    payload = {"quoteResponse": {"result": [{"regularMarketPrice": 155.8, "regularMarketTime": 1789632918, "gmtOffSetMilliseconds": 3600000}]}}
    assert S.parse_yahoo_quote(payload, "usdjpy") == [("usdjpy", "2026-09-17", 155.8)]
    assert S.parse_yahoo_quote({}, "usdjpy") == []


BIS = """FREQ,REF_AREA,UNIT_MEASURE,COMPILATION,TITLE,TIME_PERIOD,OBS_VALUE,OBS_STATUS
D,JP,368,"From 17 Jun 2026 onwards: around 1.00 percent",Central bank policy rates - Japan,2026-06-16,0.75,A
D,JP,368,"From 17 Jun 2026 onwards: around 1.00 percent",Central bank policy rates - Japan,2026-06-17,1,A
D,JP,368,"From 17 Jun 2026 onwards: around 1.00 percent",Central bank policy rates - Japan,2026-06-18,,A
"""


def test_bis_policy_rate_skips_blank_days():
    assert S.parse_bis_csv(BIS, "jp_policy") == [("jp_policy", "2026-06-16", 0.75), ("jp_policy", "2026-06-17", 1.0)]
    assert S.parse_bis_csv("FREQ,TIME_PERIOD,OBS_VALUE\nD,,", "jp_policy") == []


JP_CPI = """<p>2025年基準 消費者物価指数 全国 2026年（令和8年）8月分（2026年9月18日公表）</p>
<p>(1) <b>総合指数</b> は2025年を100として102.2 前年同月比は1.9%の上昇</p>
<p>(2) <b>生鮮食品を除く総合指数</b> は102.0 前年同月比は1.7％の上昇</p>
<p>(3) <b>生鮮食品及びエネルギーを除く総合指数</b> は102.5 前年同月比は2.4%の上昇</p>"""


def test_jp_cpi_reads_each_aggregate_separately():
    rows = dict((k, v) for k, _, v in S.parse_jp_cpi(JP_CPI))
    assert rows == {"jp_cpi_yoy": 1.9, "jp_core_cpi_yoy": 1.7, "jp_core_core_cpi_yoy": 2.4}   # '총합'이 '근원'을 잡아채지 않는다
    assert {d for _, d, _ in S.parse_jp_cpi(JP_CPI)} == {"2026-08-01"}
    assert S.parse_jp_cpi("<p>준비 중</p>") == []


def test_jp_cpi_handles_decline():
    html = JP_CPI.replace("前年同月比は1.7％の上昇", "前年同月比は0.3％の下落")
    assert ("jp_core_cpi_yoy", "2026-08-01", -0.3) in S.parse_jp_cpi(html)


def test_dram_json_and_fallback():
    payload = {"series": [{"id": "ppi", "points": [{"date": "2026-08-01", "value": 28.4}]},
                          {"id": "ddr4Spot", "points": [{"date": "2026-08-17", "value": 42.5},
                                                        {"date": "2026-09-16", "value": 45.786}]}]}
    rows = S.parse_dram_json(payload)
    assert rows == [("dram_spot", "2026-08-17", 42.5), ("dram_spot", "2026-09-16", 45.786)]
    pct, dir_ = S.direction(rows, 30, S.DRAM_DIR_PCT)
    assert round(pct, 1) == 7.7 and dir_ == 1
    html = "<td>DDR4 16Gb (2Gx8) 3200</td><td>120.00 45.00 120.00 45.00 86.000</td>" \
           "<td>DDR4 8Gb (1Gx8) 3200</td><td>82.00 24.80 82.00 24.80 46.143</td>"
    assert S.parse_dramexchange(html, "2026-09-18") == [("dram_spot", "2026-09-18", 46.143)]   # JSON 과 같은 품목
    assert S.parse_dramexchange("<p>점검 중</p>", "2026-09-18") == []


def test_direction_needs_two_points():
    assert S.direction([("dram_spot", "2026-09-16", 45.0)]) == (None, None)
    flat = [("dram_spot", "2026-08-16", 45.0), ("dram_spot", "2026-09-16", 45.2)]
    assert S.direction(flat, 30, 1.0)[1] == 0


EPS = {"quoteSummary": {"result": [{"earningsTrend": {"trend": [
    {"period": "0y", "epsTrend": {"current": {"raw": 9.3}, "30daysAgo": {"raw": 8.95}}},
    {"period": "+1y", "epsTrend": {"current": {"raw": 15.68}, "30daysAgo": {"raw": 12.82}, "epsTrendCurrency": "USD"}}]},
    "calendarEvents": {"earnings": {"earningsDate": [{"fmt": "2026-11-17"}], "isEarningsDateEstimate": False}}}]}}


def test_eps_trend_and_earnings_date():
    trend = S.parse_eps_trend(EPS)
    assert trend["current"] == 15.68 and trend["30daysAgo"] == 12.82 and "epsTrendCurrency" not in trend
    assert S.parse_eps_trend(EPS, "0y")["current"] == 9.3
    assert S.parse_eps_trend({}, "+1y") == {}
    assert S.parse_earnings_date(EPS) == ("2026-11-17", True)
    estimate = {"quoteSummary": {"result": [{"calendarEvents": {"earnings": {"earningsDate": [{"fmt": "2026-11-03"}],
                                                                            "isEarningsDateEstimate": True}}}]}}
    assert S.parse_earnings_date(estimate) == ("2026-11-03", False)
    assert S.parse_earnings_date({}) == (None, False)
