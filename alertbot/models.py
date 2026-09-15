"""알림 모델 — 엔진이 만드는 Signal 과 종류별 등급·쿨다운."""

from dataclasses import dataclass

# 등급은 채널이 어디까지 받을지 고르는 기준이다 (TELEGRAM_MIN_SEVERITY).
#   action — 지금 행동해야 한다 (매수·손절·매도·익절·추가매수·마감 정리)
#   review — 살펴볼 일이 생겼다 (일부 익절 검토, 매수 취소, 청산 완료)
#   info   — 정기·시스템 (시황, 장 시작/마감, 성적, 기동)
SEVERITY_ORDER = {"info": 0, "review": 1, "action": 2}

# kind -> (등급, 쿨다운 종류). 쿨다운 분은 config 의 ALERT_COOLDOWN_MIN(strong) /
# WEAK_COOLDOWN_MIN(weak) 이고 none 은 정기 발송이라 쿨다운이 없다.
# 검토 권유가 15분마다 오면 정작 손절 알림이 왔을 때도 흘려보게 되므로 weak 는 더 길다.
KINDS = {
    "ENTRY": ("action", "strong"),          # 매수 신호와 '아직 미진입' 반복은 같은 키를 쓴다
    "ENTRY_CANCEL": ("review", "strong"),
    "CRASH_BUY": ("action", "strong"),      # Binance 5분봉 급락 매수 후보 (run_binance.py). 워커가 60분 쿨다운을 따로 건다
    "SURGE_WATCH": ("review", "strong"),    # Binance 4시간봉 급등 확인 — 추종 관찰 (눌림 대기)
    "SURGE_ENTRY": ("action", "strong"),    # Binance 4시간봉 급등 뒤 눌림 재돌파 — 추종 진입 후보. 워커가 7일 쿨다운을 건다
    "SURGE_WATCH_1D": ("review", "strong"), # 일봉 급등 확인 — 추종 관찰
    "SURGE_ENTRY_1D": ("action", "strong"), # 일봉 급등 뒤 눌림 재돌파 — 추종 진입 후보 (20일 쿨다운)
    "CRASH_WATCH_1D": ("review", "strong"), # 일봉 급락 확인 — 추종 관찰 (약세 국면)
    "CRASH_SHORT_1D": ("action", "strong"), # 일봉 급락 뒤 반등 실패(EMA9 재이탈) — 추종 숏 후보 (20일 쿨다운)
    "STOP": ("action", "strong"),
    "SELL": ("action", "strong"),
    "EXIT_FULL": ("action", "strong"),      # '익절하세요'/'정리하세요' 문구가 달라도 같은 청산 신호다
    "ADDON": ("action", "strong"),
    "CLOSE_WARN": ("action", "strong"),
    "CLOSED": ("review", "strong"),
    "EXIT_HALF": ("review", "weak"),
    "EXIT_THIRD": ("review", "weak"),
    "MARKET_OPEN": ("info", "none"),
    "MARKET_CLOSE": ("info", "none"),
    "DAILY_REPORT": ("info", "none"),
    "SUMMARY": ("info", "none"),
    "SYSTEM": ("info", "none"),
    # 자동매매. 주문 관련은 쿨다운 없이 매번 보낸다 — 같은 종목의 연속 주문도 각각 알아야 한다.
    "ORDER_SENT": ("action", "none"),
    "ORDER_FILLED": ("action", "none"),
    "ORDER_CANCELED": ("review", "none"),
    "ORDER_FAILED": ("action", "none"),
    "AUTOTRADE_DISABLED": ("action", "none"),
}


@dataclass
class Signal:
    kind: str
    title: str              # 알림 제목 (이모지 포함). 예: "🔵 매수하세요"
    label: str              # 종목 표시명, 또는 시장/시각
    body: str
    symbol: str = None      # 종목 코드. 쿨다운 키와 이력 조회에 쓴다

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError(f"unknown signal kind: {self.kind}")

    @property
    def severity(self) -> str:
        return KINDS[self.kind][0]

    @property
    def cooldown(self) -> str:
        return KINDS[self.kind][1]

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.symbol or self.label}"

    def text(self) -> str:
        """텔레그램·로그에 쓰는 원본 형식."""
        return f"{self.title} | {self.label}\n{self.body}"
