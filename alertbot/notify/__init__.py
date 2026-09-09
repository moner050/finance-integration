"""알림 채널 — .env 설정으로 채널 목록을 만든다. 엔진과 백오피스(테스트 발송)가 같이 쓴다."""

import logging

from ..config import (TG_CHATS, TG_MIN_SEVERITY, TG_TOKEN, WA_MIN_SEVERITY, WA_PHONE_ID,
                      WA_TEMPLATE, WA_TEMPLATE_LANG, WA_TO, WA_TOKEN)

log = logging.getLogger("scalper")


def build_channels() -> list:
    from .telegram import TelegramChannel
    from .whatsapp import WhatsAppChannel

    channels = []
    if TG_TOKEN and TG_CHATS:
        channels.append(TelegramChannel(TG_TOKEN, TG_CHATS, TG_MIN_SEVERITY))
        log.info("텔레그램 수신자 %d명 (%s 이상)", len(TG_CHATS), TG_MIN_SEVERITY)
    else:
        log.info("텔레그램 미설정 (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")
    if WA_TOKEN and WA_PHONE_ID and WA_TO:
        channels.append(WhatsAppChannel(WA_TOKEN, WA_PHONE_ID, WA_TO, WA_TEMPLATE, WA_TEMPLATE_LANG,
                                        WA_MIN_SEVERITY))
        log.info("WhatsApp 수신자 %d명 (%s 이상, 템플릿 %s/%s)", len(WA_TO), WA_MIN_SEVERITY,
                 WA_TEMPLATE, WA_TEMPLATE_LANG)
    else:
        log.info("WhatsApp 미설정 (WHATSAPP_ACCESS_TOKEN / WHATSAPP_PHONE_NUMBER_ID / WHATSAPP_TO)")
    if not channels:
        log.info("알림 채널 없음 — 로그 파일에만 기록된다")
    return channels
