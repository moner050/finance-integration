"""알림 발송 — 쿨다운 후 텔레그램으로 보낸다."""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests

from ..config import ALERT_COOLDOWN_MIN, TG_CHATS, TG_TOKEN, WEAK_COOLDOWN_MIN

log = logging.getLogger("scalper")


@dataclass
class Notifier:
    last_sent: dict = None

    def __post_init__(self):
        self.last_sent = {}

    def send(self, level: str, ticker: str, msg: str):
        key = f"{level}:{ticker}"
        now = datetime.now(timezone.utc)
        # 강도별로 재발송 간격을 다르게 둔다. 검토 권유가 15분마다 오면
        # 정작 손절 알림이 왔을 때도 흘려보게 된다.
        if any(x in level for x in ("📊", "🔔", "🔕", "📈")):
            gap = 0        # 시황 요약은 정기 발송이라 쿨다운을 두지 않는다
        else:
            gap = WEAK_COOLDOWN_MIN if "🟡" in level else ALERT_COOLDOWN_MIN
        prev = self.last_sent.get(key)
        if prev and now - prev < timedelta(minutes=gap):
            return
        self.last_sent[key] = now
        line = f"{level} | {ticker}\n{msg}"
        log.info(line)
        if not (TG_TOKEN and TG_CHATS):
            return
        # 한 명에게 실패해도 나머지에게는 보내야 한다.
        for chat in TG_CHATS:
            try:
                resp = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                                     json={"chat_id": chat, "text": line}, timeout=5)
                body = resp.json()
                if not body.get("ok"):
                    log.warning("텔레그램 전송 실패(%s): %s", chat, body.get("description"))
            except (requests.RequestException, ValueError) as e:
                log.warning("텔레그램 전송 오류(%s): %s", chat, e)
