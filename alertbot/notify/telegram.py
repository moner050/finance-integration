"""텔레그램 채널 — Bot API sendMessage.

각 수신자는 봇에게 먼저 /start 를 보내야 한다. 텔레그램 봇은 먼저 말을 건
상대에게만 메시지를 보낼 수 있어서, 이 단계를 빼먹으면 chat not found 가 난다.
"""

import logging

import requests

from .base import Channel

log = logging.getLogger("scalper")


class TelegramChannel(Channel):
    """봇 토큰 하나 + 수신 채팅들. account_id 가 None 이면 공용 채널(.env 공개 텔레그램)로 계정 없는 신호를 전부 받고,
    숫자면 그 계정의 채널(DB 에 암호화된 계정 키)로 그 계정의 신호만 받는다 — 계정 알림이 공용 채널로, 공용 알림이 계정 채널로 새지 않는다."""
    name = "telegram"

    def __init__(self, token: str, chat_ids: list, min_severity: str = "info", timeout: int = 5, account_id: int = None):
        super().__init__(min_severity)
        self.token = token
        self.chat_ids = list(chat_ids)
        self.timeout = timeout
        self.account_id = account_id
        self.name = "telegram_public" if account_id is None else "telegram_account"

    def accepts(self, signal) -> bool:
        return super().accepts(signal) and signal.account_id == self.account_id

    def _redact(self, text: str) -> str:
        """요청 URL 에 봇 토큰이 들어가 통신 오류 문구에 그대로 찍힌다. 로그·이력(DB)에 토큰이 남지 않게 가린다."""
        return str(text).replace(self.token, "<bot-token>") if self.token else str(text)

    def send(self, signal) -> str:
        text = signal.text()
        ok, errors = 0, []
        # 한 명에게 실패해도 나머지에게는 보내야 한다.
        for chat in self.chat_ids:
            try:
                resp = requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                                     json={"chat_id": chat, "text": text}, timeout=self.timeout)
                body = resp.json()
                if body.get("ok"):
                    ok += 1
                else:
                    errors.append(f"{chat}: {body.get('description')}")
                    log.warning("텔레그램 전송 실패(%s): %s", chat, body.get("description"))
            except (requests.RequestException, ValueError) as e:
                errors.append(f"{chat}: {self._redact(e)}")
                log.warning("텔레그램 전송 오류(%s): %s", chat, self._redact(e))
        if not errors:
            return "ok"
        return ("partial: " if ok else "error: ") + "; ".join(errors)
