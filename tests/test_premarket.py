"""미국 프리마켓 분석 — 프리 VWAP·누적 거래량 배수, 30분 시황 칸 정렬, 한국시각 표기."""
from datetime import datetime
from zoneinfo import ZoneInfo

import alertbot.engine as E
import alertbot.market_hours as MH
from alertbot.indicators import premarket_history, premarket_stats
from tests import scenario as sc
from tests.conftest import make_candles

US_WATCH = {"SOXX": {"market": "US", "leaders": None, "inverse": False, "pair": None, "name": "SOXX"}}
NOW_UTC = datetime(2026, 9, 16, 12, 5, tzinfo=ZoneInfo("UTC"))       # 08:05 EDT = 21:05 KST


def _pre(date, n, volume, close=100.0, start="04:00"):
    return make_candles("US", date, start, n, volume=volume, close=close)


def test_premarket_stats_vwap_and_cumulative_ratio():
    history = premarket_history(_pre("2026-09-14", 300, 10) + _pre("2026-09-15", 300, 20)
                                + make_candles("US", "2026-09-15", "09:30", 30, volume=5000),   # 정규장 봉은 빠진다
                                "US", 240, 570, exclude_session="2026-09-16")
    assert sorted(history) == ["2026-09-14", "2026-09-15"] and len(history["2026-09-15"]) == 300
    today = _pre("2026-09-16", 60, 30, close=102.0)            # 04:00~04:59, 누적 1800
    st = premarket_stats(today, "US", "2026-09-16", 240, 570, history, min_sessions=2)
    # 과거 같은 시각(04:59)까지 누적 600, 1200 → 중앙값 900 → 2배
    assert st["vol_ratio"] == 2.0 and st["sessions"] == 2 and st["vwap"] == 102.0
    assert premarket_stats(today, "US", "2026-09-16", 240, 570, {"2026-09-15": history["2026-09-15"]}, 2)["vol_ratio"] is None
    assert premarket_stats(today, "US", "2026-09-17", 240, 570, history) is None      # 오늘 봉 없음


class PreClient(sc.FakeClient):
    def get_candles_paged(self, symbol, pages):
        return self.history


def _engine(monkeypatch, tmp_path, now_utc=NOW_UTC):
    local = lambda m: now_utc.astimezone(sc.TZ[m])
    monkeypatch.setattr(E, "now_local", local)
    monkeypatch.setattr(MH, "now_local", local)
    monkeypatch.setattr(E, "DATA_DIR", tmp_path)
    client = PreClient([], history=_pre("2026-09-15", 300, 10) + _pre("2026-09-16", 245, 30, close=103.0))
    cap = sc.CaptureNotifier()
    eng = E.SignalEngine(client, cap, US_WATCH)
    eng.pre_history["SOXX"] = premarket_history(_pre("2026-09-14", 300, 10) + _pre("2026-09-15", 300, 10),
                                                "US", 240, 570, "2026-09-16")
    eng.prev_close["SOXX"] = 100.0
    return eng, cap


def test_premarket_summary_line_and_kst_label(monkeypatch, tmp_path):
    eng, cap = _engine(monkeypatch, tmp_path)
    eng.snapshots["SOXX"] = eng._premarket_snapshot("SOXX", {"SOXX": 103.5})
    eng.market_summary([], {}, ["SOXX"])
    sig = cap.signals[-1]
    assert sig.kind == "SUMMARY" and sig.label == "21:05 프리마켓"              # 뉴욕 08:05 가 아니라 한국시각
    line = sig.body.splitlines()[0]
    assert line == "🌙 SOXX  프리마켓 +3.50% (103.5) | 갭상승 유지 — 프리 VWAP 위 · 거래량 평소 3.0배"
    # 같은 30분 칸에선 다시 보내지 않는다
    eng.market_summary([], {}, ["SOXX"])
    assert len(cap.signals) == 1


def test_premarket_30min_change_and_slot_alignment(monkeypatch, tmp_path):
    eng, cap = _engine(monkeypatch, tmp_path)
    eng.snapshots["SOXX"] = eng._premarket_snapshot("SOXX", {"SOXX": 103.5})
    eng.market_summary([], {}, ["SOXX"])
    # 칸 경계는 KST :00/:30 — 사이클이 30초씩 밀려도 21:29:59 와 21:30:00 이 갈린다
    slot = E.SignalEngine._slot
    kst = lambda h, m, sec: datetime(2026, 9, 16, h, m, sec, tzinfo=sc.TZ["KR"])
    assert slot(kst(21, 0, 0)) == slot(kst(21, 29, 59)) == slot(kst(21, 30, 0)) - 1
    eng.summary_slot -= 1                                                    # 다음 칸이 됐다
    eng.snapshots["SOXX"] = eng._premarket_snapshot("SOXX", {"SOXX": 98.0})
    eng.market_summary([], {}, ["SOXX"])
    assert cap.signals[-1].body.splitlines()[0].startswith(
        "🌙 SOXX  프리마켓 -2.00% (98.0), 30분 -5.31% | 갭하락 지속 — 프리 VWAP 아래")
