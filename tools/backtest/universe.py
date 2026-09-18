"""백테스트 유니버스 — 감시 12종목 + 선행 + KR 대형주 + US 모멘텀 대형주 + 지수 ETF. 데이터 폴더도 여기서 정한다."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("ALERT_BT_DATA") or ROOT / "data" / "backtest")

# 엔진 감시 목록 (2026-09-17 alert_watchlist) — 성적은 확장 유니버스와 분리해 본다
WATCH = {
    "000660": {"market": "KR", "leaders": None, "inverse": False, "pair": None, "name": "SK하이닉스"},
    "005930": {"market": "KR", "leaders": None, "inverse": False, "pair": None, "name": "삼성전자"},
    "009150": {"market": "KR", "leaders": None, "inverse": False, "pair": None, "name": "삼성전기"},
    "114800": {"market": "KR", "leaders": ["069500"], "inverse": True, "pair": None, "name": "KODEX 인버스"},
    "BITX": {"market": "US", "leaders": ["IBIT"], "inverse": False, "pair": None, "name": "비트코인 2배 ETF"},
    "KORU": {"market": "US", "leaders": ["EWY"], "inverse": False, "pair": None, "name": "코스피 3배 ETF"},
    "MU": {"market": "US", "leaders": None, "inverse": False, "pair": None, "name": "마이크론"},
    "OKLO": {"market": "US", "leaders": None, "inverse": False, "pair": None, "name": "오클로"},
    "RKLB": {"market": "US", "leaders": None, "inverse": False, "pair": None, "name": "로켓랩"},
    "SOXL": {"market": "US", "leaders": None, "inverse": False, "pair": "SOXS", "name": "속쓸"},
    "SOXS": {"market": "US", "leaders": None, "inverse": True, "pair": "SOXL", "name": "속쓰"},
    "SOXX": {"market": "US", "leaders": ["NVDA", "AVGO", "TSM", "MU"], "inverse": False, "pair": None, "name": "반도체 ETF"},
}
LEADERS = {"069500": "KR", "IBIT": "US", "EWY": "US", "NVDA": "US", "AVGO": "US", "TSM": "US"}
# 확장 유니버스 (유동성 상위) — 랩 전용. 이름은 보고서 표기용
EXTRA = {
    "005380": ("KR", "현대차"), "000270": ("KR", "기아"), "035420": ("KR", "NAVER"), "068270": ("KR", "셀트리온"),
    "105560": ("KR", "KB금융"), "012450": ("KR", "한화에어로스페이스"), "042660": ("KR", "한화오션"),
    "006400": ("KR", "삼성SDI"), "373220": ("KR", "LG에너지솔루션"), "034020": ("KR", "두산에너빌리티"),
    "TSLA": ("US", "테슬라"), "PLTR": ("US", "팔란티어"), "AMD": ("US", "AMD"), "COIN": ("US", "코인베이스"),
    "HOOD": ("US", "로빈후드"), "SMCI": ("US", "슈퍼마이크로"), "MSTR": ("US", "스트래티지"), "META": ("US", "메타"),
    "AAPL": ("US", "애플"), "AMZN": ("US", "아마존"),
}
# 지수·시장 국면 ETF
INDEX = {"069500": "KR", "229200": "KR", "SPY": "US", "QQQ": "US", "SOXX": "US"}

MARKET = {t: c["market"] for t, c in WATCH.items()} | LEADERS | {t: m for t, (m, _) in EXTRA.items()} | INDEX
NAME = {t: c["name"] for t, c in WATCH.items()} | {t: n for t, (_, n) in EXTRA.items()} | {"229200": "KODEX 코스닥150"}
ALL_SYMBOLS = list(dict.fromkeys(list(WATCH) + list(LEADERS) + list(EXTRA) + list(INDEX)))
TRADABLE = list(dict.fromkeys(list(WATCH) + list(EXTRA)))          # 진입 셋업을 돌릴 종목 (지수·선행 제외)

# 윈도우 예약 장치 이름 — CON·PRN·AUX·NUL·COM1~9·LPT1~9 는 확장자를 붙여도 장치로 해석된다.
# 실제로 NYSE 티커 CON(Concentra) 의 CON.json 을 read_text 하면 콘솔 입력을 기다리며 영원히 멈춘다.
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_name(sym: str) -> str:
    """파일 이름으로 쓸 수 있는 심볼. 예약 이름이면 밑줄을 앞에 붙인다 (CON -> _CON)."""
    return f"_{sym}" if sym.upper() in RESERVED_NAMES else sym


def daily_path(sym: str):
    """전 종목 일봉 파일 경로 (data/backtest/daily/{심볼}.json)."""
    return DATA_DIR / "daily" / f"{safe_name(sym)}.json"
