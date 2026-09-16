"""알림 계층 — 쿨다운, 채널 격리, 등급 필터, 텔레그램 페이로드."""
from datetime import datetime, timedelta, timezone

import pytest

import alertbot.notify.dispatcher as D
import alertbot.notify.telegram as T
from alertbot.models import KINDS, Signal
from alertbot.notify.base import Channel


class Recorder(Channel):
    def __init__(self, name="rec", min_severity="info", fail=False):
        super().__init__(min_severity)
        self.name, self.fail, self.got = name, fail, []

    def send(self, signal):
        if self.fail:
            raise RuntimeError("boom")
        self.got.append(signal)
        return "ok"


def sig(kind="ENTRY", symbol="AAA", title="🔵 매수하세요", body="현재가 100\n거래량 3배"):
    return Signal(kind, title, "테스트", body, symbol)


def test_signal_model():
    s = sig()
    assert (s.severity, s.cooldown, s.key) == ("action", "strong", "ENTRY:AAA")
    assert s.text() == "🔵 매수하세요 | 테스트\n현재가 100\n거래량 3배"
    assert Signal("SUMMARY", "📊 시황", "10:00", "b").key == "SUMMARY:10:00"
    with pytest.raises(ValueError):
        Signal("NOPE", "x", "y", "z")
    assert {k for k, (sev, _) in KINDS.items() if sev == "info"} == {
        "MARKET_OPEN", "MARKET_CLOSE", "DAILY_REPORT", "SUMMARY", "SYSTEM"}


def test_cooldown_by_kind_and_symbol():
    d = D.Dispatcher([Recorder()])
    assert d.send(sig()) == {"rec": "ok"}
    assert d.send(sig()) is None                                 # 15분 안 → 억제 (빈 dict 는 '채널 없음')
    assert d.send(sig(symbol="BBB")) == {"rec": "ok"}            # 다른 종목은 별개
    assert d.send(sig("EXIT_FULL", title="🟢 전량 익절하세요")) == {"rec": "ok"}
    assert d.send(sig("EXIT_FULL", title="🟢 전량 정리하세요")) is None   # 문구가 달라도 같은 청산 신호 (P1-5)
    d.last_sent[sig().key] = datetime.now(timezone.utc) - timedelta(minutes=16)
    assert d.send(sig()) == {"rec": "ok"}
    assert d.send(sig(), force=True) == {"rec": "ok"}           # 테스트 발송은 쿨다운 무시


def test_weak_and_none_cooldown():
    d = D.Dispatcher([Recorder()])
    half = sig("EXIT_HALF", title="🟡 절반 익절 검토")
    assert d.send(half)
    d.last_sent[half.key] = datetime.now(timezone.utc) - timedelta(minutes=30)
    assert d.send(half) is None                                  # 45분 쿨다운
    summary = Signal("SUMMARY", "📊 시황", "10:00", "x")
    assert d.send(summary) and d.send(summary)                   # 정기 발송은 쿨다운 없음


def test_channel_isolation_and_severity_filter():
    bad, tg, wa = Recorder("bad", fail=True), Recorder("telegram"), Recorder("strict", "review")
    recorded = []
    d = D.Dispatcher([bad, tg, wa], record=lambda s, r: recorded.append((s.kind, dict(r))))
    r = d.send(Signal("SUMMARY", "📊 시황", "10:00", "x"))
    assert r["bad"].startswith("error") and r["telegram"] == "ok" and r["strict"] == "skip"
    r = d.send(sig("EXIT_HALF", title="🟡 절반 익절 검토"))
    assert r["strict"] == "ok"
    r = d.send(sig())
    assert r["strict"] == "ok" and len(tg.got) == 3 and len(wa.got) == 2
    assert [k for k, _ in recorded] == ["SUMMARY", "EXIT_HALF", "ENTRY"]
    wa.enabled = False
    assert d.send(sig(symbol="CCC"))["strict"] == "skip"


def test_public_channel_gets_market_signals_without_account_lines(monkeypatch):
    """공개 채널은 PUBLIC_KINDS 만 받고 본문의 계좌 줄(account)을 뺀다. 내 채널은 전부, 계좌 줄까지 받는다."""
    calls = []

    def fake_post(url, json=None, timeout=None, **kw):
        calls.append((url, json["text"]))
        return FakeResp(body={"ok": True})
    monkeypatch.setattr(T.requests, "post", fake_post)
    mine, pub = T.TelegramChannel("MINE", ["1"]), T.TelegramChannel("PUB", ["9"], public=True)
    assert (mine.name, pub.name) == ("telegram", "telegram_public")
    d = D.Dispatcher([mine, pub])
    sell = Signal("SELL", "🔴 매도하세요", "테스트", "종가 100.2가 매수 신호봉 저점 100.3 아래로 내려감", "AAA",
                  account="손익 -0.6%  (평단 100.8 → 현재 100.2)")
    assert d.send(sell) == {"telegram": "ok", "telegram_public": "ok"}
    assert calls[0][0].startswith("https://api.telegram.org/botMINE/") and "손익 -0.6%" in calls[0][1]
    assert calls[1][0].startswith("https://api.telegram.org/botPUB/") and "손익" not in calls[1][1] and "저점 100.3" in calls[1][1]
    assert sell.full_body().endswith("손익 -0.6%  (평단 100.8 → 현재 100.2)")
    # 계좌 정보만 담는 종류는 공개 채널이 받지 않는다
    for kind, title in (("STOP", "🔴 손절하세요"), ("CLOSED", "✅ 손절 완료"), ("SYSTEM", "⚪ 시스템"),
                        ("ORDER_SENT", "📤 주문 접수"), ("BN_ENTRY", "📥 진입"), ("DAILY_REPORT", "📈 오늘 성적")):
        r = d.send(Signal(kind, title, "x", "b", "BBB"))
        assert r["telegram"] == "ok" and r["telegram_public"] == "skip", kind
    assert d.send(Signal("CRASH_BUY", "🔵 급락 매수 후보", "ETCUSDT 5분봉", "b", "ETCUSDT"))["telegram_public"] == "ok"
    # 장 시작·시황은 공개로 가지만 시황의 보유 현황(account)은 빠진다
    assert d.send(Signal("MARKET_OPEN", "🔔 장 시작", "한국", "감시 시작"))["telegram_public"] == "ok"
    r = d.send(Signal("SUMMARY", "📊 시황", "10:00", "▲ 삼성전자  기준선 위 | 2/3", account="\n내 보유\n🔴 속쓰  보유 380주 -31%"))
    assert r["telegram_public"] == "ok" and "보유 380주" in calls[-2][1] and "보유" not in calls[-1][1]


def test_record_failure_does_not_break_send():
    def boom(s, r):
        raise RuntimeError("db down")
    d = D.Dispatcher([Recorder()], record=boom)
    assert d.send(sig()) == {"rec": "ok"}


# --- 텔레그램 ------------------------------------------------------------------

class FakeResp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code, self._body, self.text = status, body, text

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def test_telegram_payload_and_partial_failure(monkeypatch):
    calls = []

    def fake_post(url, json=None, timeout=None, **kw):
        calls.append((url, json))
        return FakeResp(body={"ok": True} if json["chat_id"] == "1" else {"ok": False, "description": "chat not found"})
    monkeypatch.setattr(T.requests, "post", fake_post)
    ch = T.TelegramChannel("TOKEN", ["1", "2"])
    result = ch.send(sig())
    assert result.startswith("partial:") and "chat not found" in result
    assert calls[0][0] == "https://api.telegram.org/botTOKEN/sendMessage"
    assert calls[0][1] == {"chat_id": "1", "text": "🔵 매수하세요 | 테스트\n현재가 100\n거래량 3배"}
    assert ch.send(Signal("SUMMARY", "📊 시황", "10:00", "x")) .startswith("partial")
    ch.chat_ids = ["1"]
    assert ch.send(sig()) == "ok"
