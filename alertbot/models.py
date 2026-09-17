"""알림 모델 — 엔진이 만드는 Signal 과 종류별 등급·쿨다운."""

from dataclasses import dataclass

# 등급은 채널이 어디까지 받을지 고르는 기준이다 (Channel.min_severity).
#   action — 지금 행동해야 한다 (매수·손절·매도·익절·추가매수·마감 정리)
#   review — 살펴볼 일이 생겼다 (일부 익절 검토, 매수 취소, 청산 완료)
#   info   — 정기·시스템 (시황, 장 시작/마감, 성적, 기동)
SEVERITY_ORDER = {"info": 0, "review": 1, "action": 2}

# kind -> (등급, 쿨다운 종류). 쿨다운 분은 config 의 ALERT_COOLDOWN_MIN(strong) /
# WEAK_COOLDOWN_MIN(weak) 이고 none 은 정기 발송이라 쿨다운이 없다.
# 검토 권유가 15분마다 오면 정작 손절 알림이 왔을 때도 흘려보게 되므로 weak 는 더 길다.
KINDS = {
    "ENTRY": ("action", "strong"),          # 확정 매수 신호 — 곧바로 신호 포지션(보유)이라 반복이 없다. 대기 신호의 승격도 여기
    "ENTRY_CANCEL": ("review", "strong"),   # 매수 대기 취소·만료 (확정 신호에는 없다)
    "ENTRY_WATCH": ("review", "strong"),    # 매수 대기 — 요건은 찼지만 확인 항목이 모자란 신호, 그리고 그 반복. 자동매매 대상 아님
    "EXIT_CANCEL": ("review", "strong"),    # 청산 신호 해제 — 근거가 사라져 보유로 복귀
    "EXIT_WATCH": ("review", "strong"),     # 매도·익절 대기 — 이탈이 얕거나 소진이 아직 확실하지 않은 신호. 자동매매 대상 아님
    "CRASH_BUY": ("action", "strong"),      # Binance 5분봉 급락 매수 후보 (run_binance.py). 워커가 60분 쿨다운을 따로 건다
    "SURGE_WATCH": ("review", "strong"),    # Binance 4시간봉 급등 확인 — 추종 관찰 (눌림 대기)
    "SURGE_ENTRY": ("action", "strong"),    # Binance 4시간봉 급등 뒤 눌림 재돌파 — 추종 진입 후보. 워커가 7일 쿨다운을 건다
    "SURGE_WATCH_1D": ("review", "strong"), # 일봉 급등 확인 — 추종 관찰
    "SURGE_ENTRY_1D": ("action", "strong"), # 일봉 급등 뒤 눌림 재돌파 — 추종 진입 후보 (20일 쿨다운)
    "CRASH_WATCH_1D": ("review", "strong"), # 일봉 급락 확인 — 추종 관찰 (약세 국면)
    "CRASH_SHORT_1D": ("action", "strong"), # 일봉 급락 뒤 반등 실패(EMA9 재이탈) — 추종 숏 후보 (20일 쿨다운)
    "SCAN_SURGE": ("review", "none"),       # 급변 감시 — 거래대금 상위 코인 급등 감지 (관찰, 매매 없음). 쿨다운은 워커가 코인마다 건다
    "SCAN_CRASH": ("review", "none"),       # 급변 감시 — 급락 감지
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
    "SIGNAL_REPORT": ("info", "none"),    # 신호 포지션 모의 성적표 (장 마감 · 코인은 자정 KST)
    "SUMMARY": ("info", "none"),
    "SYSTEM": ("info", "none"),
    # 자동매매(공용 가상 장부 · 계정별 live). 주문 관련은 쿨다운 없이 매번 보낸다 — 같은 종목의 연속 주문도 각각 알아야 한다.
    "ORDER_SENT": ("action", "none"),
    "ORDER_FILLED": ("action", "none"),
    "ORDER_CANCELED": ("review", "none"),
    "ORDER_FAILED": ("action", "none"),
    "AUTOTRADE_DISABLED": ("action", "none"),
    # Binance 자동매매 (공용 가상 장부 · 계정별 live, run_binance.py). 포지션 사건은 쿨다운 없이 매번 보낸다
    "BN_ENTRY": ("action", "none"),
    "BN_EXIT": ("action", "none"),
    "BN_SKIP": ("review", "none"),
    "BN_FAIL": ("action", "none"),        # live 주문 실패·손절 주문 실패·자동 차단
}


def price_text(value, market: str) -> str:
    """알림에 싣는 가격. 한국 주식은 원 단위라 소수점 없이 천 단위 쉼표(1,766,000), 그 밖의 시장은 값 그대로."""
    if market == "KR" and value is not None:
        return f"{float(value):,.0f}"
    return f"{value}"


@dataclass
class Signal:
    kind: str
    title: str              # 알림 제목 (이모지 포함). 예: "🔵 매수하세요"
    label: str              # 종목 표시명, 또는 시장/시각
    body: str               # 시장 근거
    symbol: str = None      # 종목 코드. 쿨다운 키와 이력 조회에 쓴다
    account: str = None     # 장부 줄(가상 보유 수량·평단·손익). 본문 뒤에 붙는다
    # 알림 경로. None 이면 공용 채널(.env 공개 텔레그램 — 시장 신호·시황·시스템·가상매매·성적표), 숫자면 그 계정의 텔레그램으로만 간다
    # (계정별 live 주문·체결·실패·성적). 채널은 자기 account_id 와 같은 신호만 받는다 (notify/telegram.py).
    account_id: int = None

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
        """쿨다운 키. 계정 알림은 계정마다 따로 센다."""
        return f"{self.kind}:{self.symbol or self.label}" + (f"@{self.account_id}" if self.account_id is not None else "")

    def full_body(self) -> str:
        """장부 줄까지 붙인 본문 (채널·이력용)."""
        return f"{self.body}\n{self.account}" if self.account else self.body

    def text(self) -> str:
        """텔레그램·로그에 쓰는 원본 형식."""
        return f"{self.title} | {self.label}\n{self.full_body()}"
