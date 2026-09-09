"""WhatsApp 채널 — Meta Cloud API 템플릿 메시지.

제약 (Meta 정책):
- 수신자가 24시간 안에 먼저 보낸 적이 없으면 승인된 템플릿으로만 보낼 수 있다. 알림은
  항상 우리가 먼저 보내는 쪽이므로 템플릿만 쓴다. (자유 형식은 웹훅으로 24시간 창을
  추적해야 해서 넣지 않았다.)
- 템플릿 파라미터 값에는 개행·탭·연속 공백 4개 이상이 올 수 없다. 여러 줄 본문은 ' · ' 로 잇는다.
- Utility 카테고리는 건당 과금이다. 어떤 등급까지 보낼지는 min_severity 로 조절한다.

템플릿 trade_alert (Utility, 한국어) 본문은 파라미터 3개로 만든다:
    {{1}} | {{2}}
    {{3}}
승인 전에는 Meta 가 제공하는 hello_world(en_US, 파라미터 없음)로 연결만 확인한다.

사전 준비(사용자): Meta Business 계정, WhatsApp Business 앱, 개인 WhatsApp 에 안 묶인
전화번호(개발 중엔 테스트 번호 + 수신자 5명), 시스템 사용자 영구 토큰, 템플릿 승인.
"""

import logging

import requests

from .base import Channel

log = logging.getLogger("scalper")

GRAPH_URL = "https://graph.facebook.com/{version}/{phone_number_id}/messages"
PARAM_MAX_LEN = 1024
TOKEN_ERROR_CODE = 190          # 토큰 무효·만료. 재시도해도 소용없고 계속 보내면 로그만 쌓인다


def _clean(value) -> str:
    """개행·탭·연속 공백을 공백 하나로."""
    return " ".join(str(value).replace("\t", " ").split())


def template_params(signal, max_len: int = PARAM_MAX_LEN) -> list:
    """Signal → 템플릿 파라미터 [제목, 라벨, 본문 한 줄]. 빈 값은 '-' (빈 파라미터는 거부된다)."""
    lines = [ln for ln in (_clean(x) for x in signal.body.splitlines()) if ln]
    body = " · ".join(lines) or "-"
    return [_clean(signal.title) or "-", _clean(signal.label) or "-", body[:max_len]]


def _digits(number) -> str:
    """E.164 의 '+' 와 구분 기호를 뺀다. Cloud API 는 국가번호부터 숫자만 받는다."""
    return "".join(ch for ch in str(number) if ch.isdigit())


class WhatsAppChannel(Channel):
    name = "whatsapp"

    def __init__(self, token: str, phone_number_id: str, to_numbers: list,
                 template: str = "trade_alert", lang: str = "ko", min_severity: str = "review",
                 api_version: str = "v22.0", timeout: int = 5):
        super().__init__(min_severity)
        self.token = token
        self.phone_number_id = phone_number_id
        self.to_numbers = list(to_numbers)
        self.template = template
        self.lang = lang
        self.url = GRAPH_URL.format(version=api_version, phone_number_id=phone_number_id)
        self.timeout = timeout

    def payload(self, to: str, signal) -> dict:
        template = {"name": self.template, "language": {"code": self.lang}}
        if self.template != "hello_world":
            template["components"] = [{
                "type": "body",
                "parameters": [{"type": "text", "text": p} for p in template_params(signal)],
            }]
        return {"messaging_product": "whatsapp", "to": _digits(to), "type": "template",
                "template": template}

    def _post(self, payload: dict) -> str:
        """수신자 한 명. 5xx·통신 오류는 한 번 더 시도한다. 4xx 는 원인이 고정이라 바로 돌려준다."""
        headers = {"Authorization": f"Bearer {self.token}"}
        last = "error: no attempt"
        for _ in range(2):
            try:
                resp = requests.post(self.url, json=payload, headers=headers, timeout=self.timeout)
            except requests.RequestException as e:
                last = f"error: {e}"
                continue
            if resp.status_code >= 500:
                last = f"error: HTTP {resp.status_code}"
                continue
            try:
                body = resp.json()
            except ValueError:
                body = {}
            err = body.get("error")
            if resp.status_code < 300 and not err:
                return "ok"
            err = err or {}
            code = err.get("code")
            if code == TOKEN_ERROR_CODE:
                self.enabled = False
                log.warning("WhatsApp 토큰 무효(190) — 채널을 끈다. 토큰을 갱신하고 재시작할 것: %s",
                            err.get("message"))
            return f"error: {code} {err.get('message') or resp.text[:120]}"
        return last

    def send(self, signal) -> str:
        ok, errors = 0, []
        for to in self.to_numbers:
            result = self._post(self.payload(to, signal))
            if result == "ok":
                ok += 1
            else:
                errors.append(f"{to}: {result}")
                log.warning("WhatsApp 전송 실패(%s): %s", to, result)
            if not self.enabled:
                break
        if not errors:
            return "ok"
        return ("partial: " if ok else "error: ") + "; ".join(errors)
