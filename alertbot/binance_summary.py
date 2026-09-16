"""Binance 시황 요약 — 토스 엔진의 30분 시황처럼, 워커들이 마지막 완성봉에서 계산한 지표를 모아 한 번에 보낸다.

행동 신호는 개별 알림(🔵🔴)뿐이다. 요약은 '조건이 어디까지 찼는지' 와 열린 포지션을 보여 줄 뿐이다.
"""

from datetime import datetime, timezone

from .binance_crash import KST
from .models import Signal


def summary_signal(workers: list, trader=None, now: datetime = None):
    """워커 status_lines + 트레이더 open_lines → SUMMARY Signal. 보여줄 게 없으면 None."""
    now = now or datetime.now(timezone.utc)
    lines = []
    for w in workers:
        lines.extend(w.status_lines(now))
    mine = trader.open_lines() if trader is not None else []      # 내 포지션 — 계좌 줄, 공개 채널엔 빠진다
    if not lines and not mine:
        return None
    lines += ["", "※ 참고용. 진입·청산은 개별 알림(🔵🔴📥📤)이 왔을 때만"]
    return Signal("SUMMARY", "📊 코인 시황", now.astimezone(KST).strftime("%H:%M"), "\n".join(lines),
                  account="\n".join(["", "내 포지션"] + mine) if mine else None)
