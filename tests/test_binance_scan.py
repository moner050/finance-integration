"""Binance 급변 감시 — 유니버스(필터·추가·제외·갱신 주기), 감지(급등·급락·묶음·쿨다운·시간 게이트), 시황 줄."""
import json
from datetime import datetime, timedelta, timezone

import pytest

import alertbot.binance_scan as SC
from alertbot import db as DBM
from alertbot.config import SCAN_ATR_MULT
from tests.test_binance_follow import T0, bar, path, quiet

H1 = 3_600_000
N = 800                                                     # 기준 ATR 30일(720봉) + 여유


class Recorder:
    def __init__(self):
        self.sent = []

    def send(self, signal, force=False):
        self.sent.append(signal)
        return {"test": "ok"}


def hour_bars(n=N, price=10.0):
    return quiet(n, price=price, rng=price * 0.002, step=H1)


def pumped(pct=12.0, price=10.0, n=N):
    """조용한 1시간봉 뒤 마지막 봉 하나가 pct% 오른다 (음수면 급락)."""
    bars = hour_bars(n - 1, price)
    return path(bars, [price * (1 + pct / 100)], step=H1)


def at(bars, sec=10):
    """마지막 완성봉이 끝나고 sec 초 뒤."""
    return datetime.fromtimestamp((bars[-1]["open_time"] + H1) / 1000 + sec, tz=timezone.utc)


class FakeUniverse:
    def __init__(self, symbols, ranks=None):
        self.current = list(symbols)
        self.ranks = ranks if ranks is not None else {s: i + 1 for i, s in enumerate(symbols)}

    def symbols(self, now):
        return self.current


# -- 유니버스 ----------------------------------------------------------------------

def info(*rows):
    return {"symbols": [dict(symbol=s, contractType=ct, status=st, underlyingType=ut, quoteAsset=q) for s, ct, st, ut, q in rows]}


def test_eligible_keeps_only_tradable_usdt_coin_perpetuals():
    got = SC.eligible(info(("BTCUSDT", "PERPETUAL", "TRADING", "COIN", "USDT"),
                           ("XAUUSDT", "TRADIFI_PERPETUAL", "TRADING", "COMMODITY", "USDT"),
                           ("BTCDOMUSDT", "PERPETUAL", "TRADING", "INDEX", "USDT"),
                           ("OLDUSDT", "PERPETUAL", "SETTLING", "COIN", "USDT"),
                           ("BTCUSDC", "PERPETUAL", "TRADING", "COIN", "USDC"),
                           ("USDCUSDT", "PERPETUAL", "TRADING", "COIN", "USDT"),
                           ("PAXGUSDT", "PERPETUAL", "TRADING", "COIN", "USDT")))
    assert got == {"BTCUSDT"}


def test_parse_list_normalizes():
    assert SC.parse_list("lsk, ARBUSDT  zec,lsk\n") == ["LSKUSDT", "ARBUSDT", "ZECUSDT"]
    assert SC.parse_list("") == [] and SC.parse_list(None) == []


def test_pick_excludes_before_ranking_and_appends_includes():
    tickers = [{"symbol": s, "quoteVolume": str(v)} for s, v in (("AUSDT", 300), ("BUSDT", 200), ("CUSDT", 100), ("DUSDT", 50))]
    allowed = {"AUSDT", "BUSDT", "CUSDT", "DUSDT", "EUSDT"}
    symbols, top, invalid = SC.pick(tickers, allowed, ["EUSDT", "ZUSDT", "BUSDT"], ["AUSDT"], 2)
    assert symbols == ["BUSDT", "CUSDT", "EUSDT"] and invalid == ["ZUSDT"]
    assert [(r["symbol"], r["rank"]) for r in top] == [("BUSDT", 1), ("CUSDT", 2)]


def test_universe_refresh_settings_failure_and_snapshot():
    store = DBM.DB.sqlite().init_schema()
    calls = {"info": 0, "tickers": 0}
    fail = {"on": False}

    def fetch_info():
        calls["info"] += 1
        if fail["on"]:
            raise RuntimeError("down")
        return info(*[(s, "PERPETUAL", "TRADING", "COIN", "USDT") for s in ("AUSDT", "BUSDT", "CUSDT", "LSKUSDT")])

    def fetch_tickers():
        calls["tickers"] += 1
        return [{"symbol": s, "quoteVolume": v} for s, v in (("AUSDT", "30"), ("BUSDT", "20"), ("CUSDT", "10"))]

    u = SC.Universe(store, fetch_info, fetch_tickers, n=2, refresh_min=60)
    t = datetime(2026, 9, 17, tzinfo=timezone.utc)
    assert u.symbols(t) == ["AUSDT", "BUSDT"] and calls["info"] == 1
    assert u.symbols(t + timedelta(minutes=10)) == ["AUSDT", "BUSDT"] and calls["info"] == 1          # 주기 전엔 조회 없음
    DBM.set_setting(store, SC.INCLUDE_KEY, "lsk, nope")
    DBM.set_setting(store, SC.EXCLUDE_KEY, "AUSDT")
    assert u.symbols(t + timedelta(minutes=11)) == ["BUSDT", "CUSDT", "LSKUSDT"] and calls["info"] == 1   # 설정 변경은 곧바로
    snap = json.loads(DBM.get_settings(store)[SC.SNAPSHOT_KEY])
    assert snap["invalid"] == ["NOPEUSDT"] and snap["exclude"] == ["AUSDT"] and [r["symbol"] for r in snap["top"]] == ["BUSDT", "CUSDT"]
    fail["on"] = True
    assert u.symbols(t + timedelta(minutes=71)) == ["BUSDT", "CUSDT", "LSKUSDT"] and calls["info"] == 2   # 실패 → 직전 목록
    assert u.symbols(t + timedelta(minutes=71, seconds=30)) == ["BUSDT", "CUSDT", "LSKUSDT"] and calls["info"] == 2
    fail["on"] = False
    u.symbols(t + timedelta(minutes=72))
    assert calls["info"] == 3                                                                          # 1분 뒤 재시도


# -- 감지 ------------------------------------------------------------------------

def test_move_needs_history_and_measures_both_sides():
    assert SC.move(hour_bars(100)) is None                  # 기준 ATR 표본 부족
    quiet_m = SC.move(hour_bars())
    assert quiet_m["up_mult"] < SCAN_ATR_MULT and quiet_m["down_mult"] < SCAN_ATR_MULT
    up = SC.move(pumped(12))
    assert up["up"] > 11 and up["up_mult"] >= SCAN_ATR_MULT
    dn = SC.move(pumped(-12))
    assert dn["down"] > 11 and dn["down_mult"] >= SCAN_ATR_MULT


def test_single_pump_alerts_once_with_time_gate_and_cooldown():
    bars = {"AAAUSDT": pumped(12), "BBBUSDT": hour_bars(price=2.0)}
    calls = []

    def fetch(symbol, interval, limit):
        calls.append(symbol)
        return bars[symbol]

    w = SC.ScanWorker(FakeUniverse(["AAAUSDT", "BBBUSDT"]), Recorder(), fetch_bars=fetch)
    now = at(bars["AAAUSDT"])
    sent = w.poll_once(now)
    assert [s.kind for s in sent] == ["SCAN_SURGE"] and sent[0].symbol == "AAAUSDT" and sent[0].title == "🚀 급등 감지"
    assert "AAAUSDT" in sent[0].body and "거래대금 1위" in sent[0].body and "매매 신호 아님" in sent[0].body
    assert w.poll_once(now + timedelta(seconds=20)) == [] and len(calls) == 2           # 새 봉 전엔 조회하지 않는다
    bars["AAAUSDT"] = path(bars["AAAUSDT"], [bars["AAAUSDT"][-1]["close"] * 1.15], step=H1)     # 다음 봉도 급등 — 4시간 쿨다운
    bars["BBBUSDT"] = path(bars["BBBUSDT"], [bars["BBBUSDT"][-1]["close"]], step=H1)
    assert w.poll_once(now + timedelta(hours=1)) == [] and len(calls) == 4
    bars["AAAUSDT"] = path(bars["AAAUSDT"], [bars["AAAUSDT"][-1]["close"] * 0.8] * 4, step=H1)    # 4시간 뒤엔 방향이 달라도 다시 알린다
    later = w.poll_once(now + timedelta(hours=5))
    assert [(s.kind, s.symbol) for s in later] == [("SCAN_CRASH", "AAAUSDT")]


def test_simultaneous_moves_are_grouped_per_direction(monkeypatch):
    monkeypatch.setattr(SC, "SCAN_MAX_LINES", 2)
    bars = {"AAAUSDT": pumped(12), "BBBUSDT": pumped(25), "CCCUSDT": pumped(15), "DDDUSDT": pumped(-14), "EEEUSDT": hour_bars()}
    rec = Recorder()
    w = SC.ScanWorker(FakeUniverse(list(bars), ranks={"AAAUSDT": 3}), rec, fetch_bars=lambda s, i, n: bars[s])
    sent = w.poll_once(at(bars["AAAUSDT"]))
    assert [s.kind for s in sent] == ["SCAN_SURGE", "SCAN_CRASH"]
    surge, crash = sent
    assert surge.symbol is None and surge.label == "코인 3종목 1시간봉"
    lines = surge.body.splitlines()
    assert lines[0].startswith("BBBUSDT") and lines[1].startswith("CCCUSDT") and lines[2] == "외 1종목"
    assert "추가 코인" in lines[0]                                                        # 순위 밖(추가) 코인 표기
    assert crash.symbol == "DDDUSDT" and crash.title == "💥 급락 감지" and "고점 대비 -" in crash.body


def test_one_broken_symbol_does_not_block_others():
    bars = {"AAAUSDT": pumped(12)}

    def fetch(symbol, interval, limit):
        if symbol == "BADUSDT":
            raise RuntimeError("timeout")
        return bars[symbol]

    w = SC.ScanWorker(FakeUniverse(["BADUSDT", "AAAUSDT"]), Recorder(), fetch_bars=fetch)
    assert [s.symbol for s in w.poll_once(at(bars["AAAUSDT"]))] == ["AAAUSDT"]


def test_status_line_is_one_summary_row():
    import alertbot.backoffice.app as A
    bars = {"AAAUSDT": pumped(12), "BBBUSDT": pumped(-3)}
    w = SC.ScanWorker(FakeUniverse(list(bars)), Recorder(), fetch_bars=lambda s, i, n: bars[s])
    now = at(bars["AAAUSDT"])
    assert w.status_lines(now)[0].endswith("데이터 부족 (감시 2종목)")
    w.poll_once(now)
    (line,) = w.status_lines(now)
    assert "최대 상승 AAAUSDT +1" in line and "최대 하락 BBBUSDT -" in line and line.endswith("24시간 감지 1종목")
    items = A.parse_summary(line)["items"]
    assert list(items) == ["급변 감시 1시간봉 상위30"] and items["급변 감시 1시간봉 상위30"]["short"] == "24시간 감지 1종목"


def test_start_message_includes_scan_line():
    import run_binance
    from alertbot.config import FOLLOW_SPECS
    lines = run_binance.watch_list()
    assert len(lines) == 2 + len(FOLLOW_SPECS) and lines[-1].startswith("급변 감시 1시간봉: 거래대금 상위 30")
