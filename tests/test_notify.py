"""알림 계층 — 쿨다운, 채널 격리, 등급 필터, 텔레그램/WhatsApp 페이로드."""
from datetime import datetime, timedelta, timezone

import pytest

import alertbot.notify.dispatcher as D
import alertbot.notify.telegram as T
import alertbot.notify.whatsapp as W
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
    assert d.send(sig()) == {}                                   # 15분 안 → 억제
    assert d.send(sig(symbol="BBB")) == {"rec": "ok"}            # 다른 종목은 별개
    assert d.send(sig("EXIT_FULL", title="🟢 전량 익절하세요")) == {"rec": "ok"}
    assert d.send(sig("EXIT_FULL", title="🟢 전량 정리하세요")) == {}   # 문구가 달라도 같은 청산 신호 (P1-5)
    d.last_sent[sig().key] = datetime.now(timezone.utc) - timedelta(minutes=16)
    assert d.send(sig()) == {"rec": "ok"}
    assert d.send(sig(), force=True) == {"rec": "ok"}           # 테스트 발송은 쿨다운 무시


def test_weak_and_none_cooldown():
    d = D.Dispatcher([Recorder()])
    half = sig("EXIT_HALF", title="🟡 절반 익절 검토")
    assert d.send(half)
    d.last_sent[half.key] = datetime.now(timezone.utc) - timedelta(minutes=30)
    assert d.send(half) == {}                                    # 45분 쿨다운
    summary = Signal("SUMMARY", "📊 시황", "10:00", "x")
    assert d.send(summary) and d.send(summary)                   # 정기 발송은 쿨다운 없음


def test_channel_isolation_and_severity_filter():
    bad, tg, wa = Recorder("bad", fail=True), Recorder("telegram"), Recorder("whatsapp", "review")
    recorded = []
    d = D.Dispatcher([bad, tg, wa], record=lambda s, r: recorded.append((s.kind, dict(r))))
    r = d.send(Signal("SUMMARY", "📊 시황", "10:00", "x"))
    assert r["bad"].startswith("error") and r["telegram"] == "ok" and r["whatsapp"] == "skip"
    r = d.send(sig("EXIT_HALF", title="🟡 절반 익절 검토"))
    assert r["whatsapp"] == "ok"
    r = d.send(sig())
    assert r["whatsapp"] == "ok" and len(tg.got) == 3 and len(wa.got) == 2
    assert [k for k, _ in recorded] == ["SUMMARY", "EXIT_HALF", "ENTRY"]
    wa.enabled = False
    assert d.send(sig(symbol="CCC"))["whatsapp"] == "skip"


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


# --- WhatsApp -------------------------------------------------------------------

def test_template_params_flatten_lines():
    s = Signal("STOP", "🔴 손절하세요", "SK하이닉스", "손익 -5.1%  (평단 100 → 현재 94.9)\n\t손절 한도 -5.0% 도달 — 최후     안전망\n\n", "000660")
    p = W.template_params(s)
    assert p == ["🔴 손절하세요", "SK하이닉스", "손익 -5.1% (평단 100 → 현재 94.9) · 손절 한도 -5.0% 도달 — 최후 안전망"]
    assert all("\n" not in x and "\t" not in x and "    " not in x for x in p)
    assert W.template_params(Signal("SUMMARY", "📊 시황", "10:00", "\n \n"))[2] == "-"
    assert len(W.template_params(sig(body="x" * 3000))[2]) == W.PARAM_MAX_LEN


def test_whatsapp_payload_and_hello_world():
    ch = W.WhatsAppChannel("TOK", "12345", ["+82 10-1234-5678"], template="trade_alert", lang="ko")
    p = ch.payload("+82 10-1234-5678", sig())
    assert p["to"] == "821012345678" and p["type"] == "template"
    assert p["template"]["name"] == "trade_alert" and p["template"]["language"] == {"code": "ko"}
    params = p["template"]["components"][0]["parameters"]
    assert [x["type"] for x in params] == ["text"] * 3 and params[2]["text"] == "현재가 100 · 거래량 3배"
    hello = W.WhatsAppChannel("TOK", "12345", ["1"], template="hello_world", lang="en_US")
    assert "components" not in hello.payload("1", sig())["template"]
    assert ch.url == "https://graph.facebook.com/v22.0/12345/messages"


def test_whatsapp_retry_token_error_and_4xx(monkeypatch):
    calls = []
    queue = [FakeResp(503, text="down"), FakeResp(200, {"messages": [{"id": "wamid.1"}]})]

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append((url, headers["Authorization"]))
        return queue.pop(0)
    monkeypatch.setattr(W.requests, "post", fake_post)
    ch = W.WhatsAppChannel("TOK", "12345", ["1"])
    assert ch.send(sig()) == "ok" and len(calls) == 2 and calls[0][1] == "Bearer TOK"   # 5xx 는 한 번 더

    queue[:] = [FakeResp(400, {"error": {"message": "template missing", "code": 132001}})]
    calls.clear()
    assert ch.send(sig()).startswith("error: 1: error: 132001") and len(calls) == 1      # 4xx 는 재시도 없음

    queue[:] = [FakeResp(401, {"error": {"message": "expired", "code": 190}})]
    ch.to_numbers = ["1", "2"]
    calls.clear()
    assert ch.send(sig()).startswith("error:")
    assert ch.enabled is False and len(calls) == 1                                        # 토큰 무효 → 채널 중단
    assert ch.accepts(sig()) is False
