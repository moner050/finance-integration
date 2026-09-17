"""자동매매 모델 — 주문 의도(intent)와 상태."""

import random
import string
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

# intent 상태. 실제 주문 상태(토스 status)는 여기로 접어 넣는다.
#   proposed  정책 검사 전                 rejected  정책이 막음 (주문 안 나감)
#   sent      브로커에 접수됨 (미체결)      open      접수 확인, 체결 대기 (sent 와 같이 '열림'으로 본다)
#   filled    전량 체결                    partial   일부 체결 후 종료(취소·거절)
#   canceled  취소됨                       failed    브로커 오류 (주문이 나갔는지 불명확하면 reconcile 이 확인)
OPEN_STATUSES = ("sent", "open")
CLOSED_STATUSES = ("rejected", "filled", "partial", "canceled", "failed")

# 토스 주문 status → intent 상태
TOSS_STATUS_MAP = {
    "PENDING": "open", "PENDING_CANCEL": "open", "PENDING_REPLACE": "open", "REPLACED": "open",
    "PARTIAL_FILLED": "open",
    "FILLED": "filled", "CANCELED": "canceled", "REJECTED": "failed",
    "CANCEL_REJECTED": "open", "REPLACE_REJECTED": "open",
}


def new_intent_id(symbol: str) -> str:
    """clientOrderId 로도 쓴다: 36자 이하, 영숫자·-·_ 만."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    salt = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    sym = "".join(ch for ch in symbol if ch.isalnum())[:12]
    return f"{sym}-{stamp}-{salt}"


@dataclass
class OrderIntent:
    intent_id: str
    mode: str                    # dry | live
    symbol: str
    market: str                  # KR | US
    side: str                    # BUY | SELL
    kind: str                    # 신호 종류 (ENTRY, STOP, SELL, EXIT_FULL, CLOSE_WARN)
    order_type: str              # LIMIT | MARKET
    price: float                 # LIMIT 가격. MARKET 이면 참조가(신호 시점 현재가)
    quantity: float
    amount: float                # price × quantity (통화는 market 에 따름)
    bar_key: str = None          # 매수 신호봉 timestamp — 같은 봉으로 두 번 사지 않는다
    ref_avg: float = None        # 매도 시 보유 평단 — 체결 뒤 실현손익 계산용
    status: str = "proposed"
    reason: str = None
    order_id: str = None
    filled_qty: float = 0.0
    avg_price: float = None
    pnl: float = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    updated_at: str = None
    account_id: int = None       # 계정별 live 주문이면 그 계정. None = 공용 가상 장부

    @classmethod
    def create(cls, mode, symbol, market, side, kind, order_type, price, quantity, bar_key=None, ref_avg=None, account_id=None):
        return cls(new_intent_id(symbol), mode, symbol, market, side, kind, order_type,
                   float(price), float(quantity), round(float(price) * float(quantity), 4), bar_key, ref_avg,
                   account_id=account_id)

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @property
    def currency(self) -> str:
        return "KRW" if self.market == "KR" else "USD"

    def to_row(self) -> dict:
        return asdict(self)

    @classmethod
    def from_row(cls, row: dict) -> "OrderIntent":
        cols = {k: row.get(k) for k in cls.__dataclass_fields__}
        for k in ("price", "quantity", "amount", "filled_qty"):
            cols[k] = float(cols[k] or 0)
        for k in ("ref_avg", "avg_price", "pnl"):
            cols[k] = float(cols[k]) if cols[k] is not None else None
        return cls(**cols)
