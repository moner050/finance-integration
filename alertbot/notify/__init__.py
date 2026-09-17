"""알림 채널 — 공용 채널은 .env, 계정 채널은 DB 의 계정 키로 만든다. 엔진·워커·백오피스(테스트 발송)가 같이 쓴다."""

import logging

from ..config import TG_PUBLIC_CHATS, TG_PUBLIC_TOKEN

log = logging.getLogger("scalper")


def build_channels() -> list:
    """공용 채널 — .env 공개 텔레그램(TELEGRAM_PUBLIC_*). 계정 없는 신호(시장 신호·시황·시스템·가상매매·성적표)를 전부 받는다."""
    from .telegram import TelegramChannel

    if TG_PUBLIC_TOKEN and TG_PUBLIC_CHATS:
        log.info("공개 텔레그램 수신자 %d명", len(TG_PUBLIC_CHATS))
        return [TelegramChannel(TG_PUBLIC_TOKEN, TG_PUBLIC_CHATS)]
    log.info("공개 텔레그램 미설정 (TELEGRAM_PUBLIC_BOT_TOKEN / TELEGRAM_PUBLIC_CHAT_ID) — 로그 파일에만 기록된다")
    return []


def account_channels(account: dict) -> list:
    """계정 채널 — accounts.live_accounts 항목의 텔레그램 키. 그 계정의 live 매매 알림(Signal.account_id)만 받는다. 키가 없으면 빈 목록."""
    from .telegram import TelegramChannel

    tg = account.get("telegram") or {}
    if not tg.get("bot_token") or not tg.get("chat_id"):
        return []
    return [TelegramChannel(tg["bot_token"], [tg["chat_id"]], account_id=account["id"])]
