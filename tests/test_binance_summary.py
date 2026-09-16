"""Binance 시황 요약 — 워커 status_lines, 트레이더 open_lines, summary_signal 조립, 시작 알림의 감시 목록."""
from datetime import datetime, timedelta, timezone

import alertbot.binance_trade as BT
from alertbot import db
from alertbot.binance_crash import CrashWorker
from alertbot.binance_follow import FollowWorker
from alertbot.binance_summary import summary_signal
from alertbot.binance_trade import Trader
from alertbot.config import FOLLOW_SPECS
from tests.test_binance_crash import make_bars, make_h4
from tests.test_binance_follow import path, quiet, surge_4h

T = datetime(2026, 9, 16, 3, 0, tzinfo=timezone.utc)      # 12:00 KST


class Recorder:
    def __init__(self):
        self.sent = []

    def send(self, signal, force=False):
        self.sent.append(signal)
        return {"test": "ok"}


def test_crash_status_lines_show_partial_conditions():
    fetched = {"ETCUSDT": make_bars(), "BTCUSDT": make_bars(close=78000)}
    w = CrashWorker(["ETCUSDT"], Recorder(), fetch_bars=lambda s: fetched[s], fetch_fund=lambda s: None, fetch_h4=lambda s: make_h4())
    assert w.status_lines(T) == ["급락 매수 5분봉 ETCUSDT  데이터 부족"]        # 아직 폴링 전
    w.poll_once(T)
    line = w.status_lines(T)[0]
    assert line.startswith("급락 매수 5분봉 ETCUSDT  7.5") and "/10배" in line and "(부족: 낙폭, RSI" in line
    fetched["ETCUSDT"] = make_bars(n=1001, crash_bars=20)                    # 새 봉에서 급락 + 반전봉 → 알림 → 쿨다운
    w.poll_once(T + timedelta(minutes=5))
    line = w.status_lines(T + timedelta(minutes=6))[0]
    assert "3/3 — 조건 충족" in line and "알림 쿨다운 중" in line


def test_follow_status_lines_track_stage_and_regime():
    spec = FOLLOW_SPECS[0]                                                    # 4시간봉 급등 추종 롱
    bars, _ = surge_4h()
    watch_bars = bars[:-4]                                                    # 급등이 처음 성립한 봉까지
    daily = quiet(price=60000, rng=500, step=24 * 3_600_000)                  # 일봉 EMA200 아래 → 강세 국면 (종가 70,000 대)
    q = {"bars": quiet()}
    w = FollowWorker(spec, Recorder(), fetch_bars=lambda s, i, n: daily if i == "1d" else q["bars"], fetch_fund=lambda s: None)
    w.poll_once(T)
    line = w.status_lines(T)[0]
    assert line.startswith("급등 추종 4시간봉 BTCUSDT  70,") and "급등 조건" in line and "(부족:" in line
    q["bars"] = watch_bars
    w.poll_once(T + timedelta(hours=4))                                        # 급등이 이어지는 중
    line = w.status_lines(T + timedelta(hours=5))[0]
    assert "강세 | 급등 성립 — 다음 봉부터 눌림 확인" in line
    w.last_alert[("BTCUSDT", "watch")] = T + timedelta(hours=4)                 # 📈 관찰 알림이 나간 뒤 재돌파 창 안
    assert "관찰 중 — 눌림 뒤 EMA9 재돌파 대기" in w.status_lines(T + timedelta(hours=5))[0]
    # 국면이 맞지 않으면 보류라고 말한다
    bear_daily = path(quiet(price=90000, rng=500, step=24 * 3_600_000), [80000], step=24 * 3_600_000)   # 마지막 일봉이 EMA200 아래
    w2 = FollowWorker(spec, Recorder(), fetch_bars=lambda s, i, n: bear_daily if i == "1d" else watch_bars, fetch_fund=lambda s: None)
    w2.poll_once(T)
    assert "국면 불일치 — 보류 (필요: 강세)" in w2.status_lines(T)[0]


def test_trader_open_lines_and_summary_signal(monkeypatch):
    monkeypatch.setattr(BT, "BINANCE_TRADE_CAPITAL", 1000.0)
    store = db.DB.sqlite().init_schema()
    rec = Recorder()
    prem = {"mark": 98.0, "next_funding": 1_000}
    t = Trader(store, rec, "dry", fetch_price=lambda s: 100.0, fetch_premium=lambda s: prem,
               fetch_settled_funding=lambda s: (1_000, 0.0))
    assert t.open_lines() == []
    t.on_entry("CRASH_BUY", "ETCUSDT", "long", {"stop": 97.0, "open_time": 1, "funding": 0.0}, 8, T)
    line = t.open_lines()[0]
    assert line.startswith("📥 [DRY] ETCUSDT CRASH_BUY 롱") and "마크 98.000 (-2.05%)" in line and "한도 09-16 20:00 KST" in line
    fetched = {"ETCUSDT": make_bars(), "BTCUSDT": make_bars(close=78000)}
    w = CrashWorker(["ETCUSDT"], rec, fetch_bars=lambda s: fetched[s], fetch_fund=lambda s: None, fetch_h4=lambda s: make_h4())
    w.poll_once(T)
    sig = summary_signal([w], t, T)
    assert sig.kind == "SUMMARY" and sig.title == "📊 코인 시황" and sig.label == "12:00"
    assert sig.body.splitlines()[0].startswith("급락 매수 5분봉 ETCUSDT") and "[DRY]" not in sig.body
    assert sig.body.endswith("※ 참고용. 진입·청산은 개별 알림(🔵🔴📥📤)이 왔을 때만")
    assert sig.account.startswith("\n내 포지션\n📥 [DRY] ETCUSDT")                 # 내 포지션은 계좌 줄 — 공개 채널엔 빠진다


def test_start_message_lists_symbols_not_rules():
    import run_binance
    lines = run_binance.watch_list()
    assert lines[0].startswith("급락 매수 5분봉: ") and len(lines) == 1 + len(FOLLOW_SPECS)
    assert all("기준ATR" not in ln and "RSI" not in ln for ln in lines)
