"""텔레그램 채널 — Bot API sendMessage.

각 수신자는 봇에게 먼저 /start 를 보내야 한다. 텔레그램 봇은 먼저 말을 건
상대에게만 메시지를 보낼 수 있어서, 이 단계를 빼먹으면 chat not found 가 난다.
"""

import logging

import requests

from .base import Channel

log = logging.getLogger("scalper")


class TelegramChannel(Channel):
    name = "telegram"

    def __init__(self, token: str, chat_ids: list, min_severity: str = "info", timeout: int = 5):
        super().__init__(min_severity)
        self.token = token
        self.chat_ids = list(chat_ids)
        self.timeout = timeout

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
                errors.append(f"{chat}: {e}")
                log.warning("텔레그램 전송 오류(%s): %s", chat, e)
        if not errors:
            return "ok"
        return ("partial: " if ok else "error: ") + "; ".join(errors)
