"""알림 채널 — .env 설정으로 채널 목록을 만든다. 엔진과 백오피스(테스트 발송)가 같이 쓴다."""

import logging

from ..config import TG_CHATS, TG_MIN_SEVERITY, TG_TOKEN

log = logging.getLogger("scalper")


def build_channels() -> list:
    from .telegram import TelegramChannel

    channels = []
    if TG_TOKEN and TG_CHATS:
        channels.append(TelegramChannel(TG_TOKEN, TG_CHATS, TG_MIN_SEVERITY))
        log.info("텔레그램 수신자 %d명 (%s 이상)", len(TG_CHATS), TG_MIN_SEVERITY)
    else:
        log.info("텔레그램 미설정 (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")
    if not channels:
        log.info("알림 채널 없음 — 로그 파일에만 기록된다")
    return channels
