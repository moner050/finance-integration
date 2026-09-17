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


class StoreUniverse(FakeUniverse):
    """상태(쿨다운·소진 숏 대기)를 DB 에 두는 워커용."""
    def __init__(self, symbols, store):
        super().__init__(symbols)
        self.store = store


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
    assert sent[0].label == "AAAUSDT" and sent[0].body.startswith("현재가 ") and "거래대금 1위" in sent[0].body
    assert "매매 신호 아님" in sent[0].body                                              # 가상 장부 없이 돌면 관찰 알림
    assert w.poll_once(now + timedelta(seconds=20)) == [] and len(calls) == 2           # 새 봉 전엔 조회하지 않는다
    bars["AAAUSDT"] = path(bars["AAAUSDT"], [bars["AAAUSDT"][-1]["close"] * 1.15], step=H1)     # 다음 봉도 급등 — 4시간 쿨다운
    bars["BBBUSDT"] = path(bars["BBBUSDT"], [bars["BBBUSDT"][-1]["close"]], step=H1)
    assert w.poll_once(now + timedelta(hours=1)) == [] and len(calls) == 4
    bars["AAAUSDT"] = path(bars["AAAUSDT"], [bars["AAAUSDT"][-1]["close"] * 0.8] * 4, step=H1)    # 4시간 뒤엔 방향이 달라도 다시 알린다
    later = w.poll_once(now + timedelta(hours=5))
    assert [(s.kind, s.symbol) for s in later] == [("SCAN_CRASH", "AAAUSDT")]


def test_cooldown_survives_restart():
    """재시작한 워커가 방금 알린 봉·쿨다운 안의 코인을 다시 알리지 않는다 (2026-09-17 재시작 직후 같은 급락을 다시 보낸 일)."""
    store = DBM.DB.sqlite().init_schema()
    bars = {"AAAUSDT": pumped(12)}
    now = at(bars["AAAUSDT"])
    first = SC.ScanWorker(StoreUniverse(["AAAUSDT"], store), Recorder(), fetch_bars=lambda s, i, n: bars[s])
    assert [s.kind for s in first.poll_once(now)] == ["SCAN_SURGE"]
    again = SC.ScanWorker(StoreUniverse(["AAAUSDT"], store), Recorder(), fetch_bars=lambda s, i, n: bars[s])
    assert again.poll_once(now + timedelta(seconds=30)) == [] and again.status_lines(now)[0].endswith("24시간 감지 1종목")


def test_simultaneous_moves_are_grouped_per_direction(monkeypatch):
    monkeypatch.setattr(SC, "SCAN_MAX_LINES", 2)
    bars = {"AAAUSDT": pumped(12), "BBBUSDT": pumped(25), "CCCUSDT": pumped(15), "DDDUSDT": pumped(-14), "EEEUSDT": hour_bars()}
    rec = Recorder()
    w = SC.ScanWorker(FakeUniverse(list(bars), ranks={"AAAUSDT": 3}), rec, fetch_bars=lambda s, i, n: bars[s])
    sent = w.poll_once(at(bars["AAAUSDT"]))
    assert [s.kind for s in sent] == ["SCAN_SURGE", "SCAN_CRASH"]
    surge, crash = sent
    assert surge.symbol is None and surge.label == "코인 3종목"
    lines = surge.body.splitlines()
    assert lines[0].startswith("BBBUSDT") and lines[1].startswith("CCCUSDT") and lines[2] == "외 1종목"
    assert "추가 코인" in lines[0]                                                        # 순위 밖(추가) 코인 표기
    assert crash.symbol == "DDDUSDT" and crash.title == "💥 급락 감지" and "4시간 -" in crash.body


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


# -- 급등 소진 숏 (공용 가상 장부) ----------------------------------------------------------

class TraderSpy:
    def __init__(self):
        self.calls = []

    def on_entry(self, *args, **kw):
        self.calls.append((args, kw))




def test_surge_queues_fade_and_shorts_on_first_close_below_ema():
    store = DBM.DB.sqlite().init_schema()
    bars = {"AAAUSDT": pumped(12)}
    spy, rec = TraderSpy(), Recorder()
    w = SC.ScanWorker(StoreUniverse(["AAAUSDT"], store), rec, fetch_bars=lambda s, i, n: bars[s], trader=spy)
    sent = w.poll_once(at(bars["AAAUSDT"]))
    pump = bars["AAAUSDT"][-1]["open_time"]
    wait = {"AAAUSDT": {"deadline": pump + H1 + 48 * H1, "after": pump}}
    assert [s.kind for s in sent] == ["SCAN_SURGE"] and "48시간 안에 1시간 종가가 EMA50 아래면 가상 숏" in sent[0].body
    assert w.pending == wait and json.loads(DBM.get_settings(store)[SC.FADE_KEY]) == wait
    assert SC.ScanWorker(StoreUniverse(["AAAUSDT"], store), rec, trader=spy).pending == wait           # 재시작해도 이어진다
    bars["AAAUSDT"] = path(bars["AAAUSDT"], [11.0], step=H1)                  # EMA50(약 10) 위 — 계속 기다린다
    w.poll_once(at(bars["AAAUSDT"]))
    assert spy.calls == [] and "AAAUSDT" in w.pending
    bars["AAAUSDT"] = path(bars["AAAUSDT"], [9.8], step=H1)                   # EMA50 아래로 마감 → 가상 숏
    w.poll_once(at(bars["AAAUSDT"]))
    (args, kw), = spy.calls
    assert args[:3] == ("SCAN_FADE", "AAAUSDT", "short") and args[4] == 48 and kw == {"notify_skip": False}
    assert args[3]["stop"] == pytest.approx(9.8 * 1.2) and args[3]["take_profit"] == pytest.approx(9.8 * 0.8)
    assert args[3]["open_time"] == bars["AAAUSDT"][-1]["open_time"]
    assert w.pending == {} and json.loads(DBM.get_settings(store)[SC.FADE_KEY]) == {}


def test_fade_wait_expires_and_crash_is_not_queued(monkeypatch):
    monkeypatch.setattr(SC, "SCAN_FADE_WAIT_HOURS", 2)
    bars = {"AAAUSDT": pumped(12), "BBBUSDT": pumped(-14)}
    spy = TraderSpy()
    w = SC.ScanWorker(FakeUniverse(list(bars)), Recorder(), fetch_bars=lambda s, i, n: bars[s], trader=spy)
    now = at(bars["AAAUSDT"])
    assert [s.kind for s in w.poll_once(now)] == ["SCAN_SURGE", "SCAN_CRASH"] and list(w.pending) == ["AAAUSDT"]  # 급락은 매매하지 않는다
    for i in range(1, 4):                                                     # EMA 위에서 3봉 — 2시간 대기가 끝난다
        bars = {s: path(b, [b[-1]["close"]], step=H1) for s, b in bars.items()}
        w.poll_once(now + timedelta(hours=i))
        assert ("AAAUSDT" in w.pending) == (i < 3)
    assert spy.calls == []


def test_pending_coin_is_watched_after_leaving_universe():
    bars = {"AAAUSDT": pumped(12)}
    fetched, spy, u = [], TraderSpy(), FakeUniverse(["AAAUSDT"])

    def fetch(symbol, interval, limit):
        fetched.append(symbol)
        return bars[symbol]

    w = SC.ScanWorker(u, Recorder(), fetch_bars=fetch, trader=spy)
    now = at(bars["AAAUSDT"])
    w.poll_once(now)
    u.current = []                                                            # 상위 30 에서 빠졌다
    bars["AAAUSDT"] = path(bars["AAAUSDT"], [9.5], step=H1)
    assert w.poll_once(now + timedelta(hours=1)) == [] and fetched == ["AAAUSDT", "AAAUSDT"]
    assert [c[0][:3] for c in spy.calls] == [("SCAN_FADE", "AAAUSDT", "short")] and "AAAUSDT" not in w.status


def test_status_line_counts_fade_waits():
    bars = {"AAAUSDT": pumped(12)}
    w = SC.ScanWorker(FakeUniverse(list(bars)), Recorder(), fetch_bars=lambda s, i, n: bars[s], trader=TraderSpy())
    now = at(bars["AAAUSDT"])
    w.poll_once(now)
    assert w.status_lines(now)[0].endswith("24시간 감지 1종목 · 숏 대기 1종목")
