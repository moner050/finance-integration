"""Binance 시황 요약 — 토스 엔진의 30분 시황처럼, 워커들이 마지막 완성봉에서 계산한 지표를 모아 한 번에 보낸다.

행동 신호는 개별 알림(🔵🔴)뿐이다. 요약은 '조건이 어디까지 찼는지' 와 열린 포지션을 보여 줄 뿐이다.
"""

from datetime import datetime, timezone

from .binance_crash import KST
from .models import Signal


def summary_signal(workers: list, now: datetime = None, paper=None):
    """워커 status_lines + 공용 가상 장부 포지션(paper) → SUMMARY Signal (공용 채널). 보여줄 게 없으면 None.

    계정별 live 포지션은 싣지 않는다 — 계정 일은 그 계정 텔레그램(진입·종료·일일 성적)으로만 간다.
    """
    now = now or datetime.now(timezone.utc)
    lines = []
    for w in workers:
        lines.extend(w.status_lines(now))
    virtual = paper.open_lines() if paper is not None else []
    if not lines and not virtual:
        return None
    if virtual:
        lines += ["", "가상 포지션 (dry)"] + virtual
    lines += ["", "※ 참고용. 진입·청산은 개별 알림(🔵🔴📥📤)이 왔을 때만"]
    return Signal("SUMMARY", "📊 코인 시황", now.astimezone(KST).strftime("%H:%M"), "\n".join(lines))
