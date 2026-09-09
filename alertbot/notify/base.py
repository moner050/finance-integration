"""알림 채널 공통 인터페이스."""

from ..models import SEVERITY_ORDER


class Channel:
    name = "base"

    def __init__(self, min_severity: str = "info"):
        if min_severity not in SEVERITY_ORDER:
            raise ValueError(f"min_severity 는 {sorted(SEVERITY_ORDER)} 중 하나: {min_severity}")
        self.min_severity = min_severity
        self.enabled = True

    def accepts(self, signal) -> bool:
        return self.enabled and SEVERITY_ORDER[signal.severity] >= SEVERITY_ORDER[self.min_severity]

    def send(self, signal) -> str:
        """발송 결과를 짧은 문자열로: 'ok' | 'partial: ...' | 'error: ...'. 예외는 Dispatcher 가 잡는다."""
        raise NotImplementedError
