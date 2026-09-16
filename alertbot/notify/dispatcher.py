"""알림 발송 — 쿨다운 → 로그 → 채널 라우팅 → 이력 기록.

한 채널이 실패해도 다른 채널은 보낸다. 쿨다운 키는 (종류, 종목)이다. 제목 문자열로
키를 잡으면 '전량 익절하세요'/'전량 정리하세요'처럼 손익 부호에 따라 키가 갈려
같은 청산 신호가 쿨다운을 무시하고 다시 나간다.
"""

import logging
from datetime import datetime, timedelta, timezone

from ..config import ALERT_COOLDOWN_MIN, WEAK_COOLDOWN_MIN

log = logging.getLogger("scalper")

COOLDOWN_MIN = {"strong": ALERT_COOLDOWN_MIN, "weak": WEAK_COOLDOWN_MIN, "none": 0}


class Dispatcher:
    def __init__(self, channels=(), record=None):
        self.channels = list(channels)
        self.record = record        # record(signal, results) — signal_log 기록. None 이면 생략
        self.last_sent = {}         # signal.key -> 마지막 발송 시각

    def send(self, signal, force: bool = False):
        """채널별 결과 {"telegram": "ok"}. 쿨다운에 걸려 보내지 않았으면 None (채널이 없어 빈 dict 인 것과 구분)."""
        now = datetime.now(timezone.utc)
        if not force:
            gap = COOLDOWN_MIN[signal.cooldown]
            prev = self.last_sent.get(signal.key)
            if prev and now - prev < timedelta(minutes=gap):
                return None
        self.last_sent[signal.key] = now
        log.info(signal.text())

        results = {}
        for ch in self.channels:
            if not ch.accepts(signal):
                results[ch.name] = "skip"
                continue
            try:
                results[ch.name] = ch.send(signal)
            except Exception as e:           # 채널 하나의 버그가 나머지 발송을 막으면 안 된다
                results[ch.name] = f"error: {e}"
                log.warning("%s 채널 오류: %s", ch.name, e)
        if self.record is not None:
            try:
                self.record(signal, results)
            except Exception as e:
                log.warning("신호 이력 기록 실패: %s", e)
        return results
