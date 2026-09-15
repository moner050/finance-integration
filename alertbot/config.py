"""설정 — .env 로드와 임계값 상수.

.env·로그·CSV·SQLite 는 패키지 폴더가 아니라 프로젝트 루트(BASE_DIR)에 둔다.
상대경로로 두면 실행 방식(더블클릭, 다른 폴더에서 실행)에 따라 작업 디렉토리가
달라져 파일이 엉뚱한 곳에 생긴다.

사전 준비
--------
1. 토스증권 WTS > 설정 > Open API 에서 client_id / client_secret 발급
2. 같은 화면 하단 '허용 IP 관리'에 현재 공인 IP 등록 (미등록 IP 는 403)
3. 프로젝트 루트의 .env 파일 (따옴표 없이, 등호 앞뒤 공백 없이):
     TOSS_CLIENT_ID=...
     TOSS_CLIENT_SECRET=...
     TELEGRAM_BOT_TOKEN=...
     TELEGRAM_CHAT_ID=...
4. Windows 는 IANA 타임존 DB 가 없어 zoneinfo 가 실패할 수 있다.
     pip install tzdata
   설치하지 않으면 고정 오프셋으로 대체하되, 미국 서머타임 전환 주간에 1시간 오차가 날 수 있다.
"""

import logging
import os
import sys
from pathlib import Path

# 프로젝트 루트. .env 는 여기서 읽는다.
BASE_DIR = Path(__file__).resolve().parent.parent


def load_config() -> dict:
    """스크립트와 같은 폴더의 .env 를 읽고, 없는 항목은 환경변수로 채운다."""
    cfg = {}
    env_path = BASE_DIR / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip("'\"")
    # 환경변수 폴백: Docker(env_file) 처럼 .env 파일이 없이 환경변수로만 줄 때. 이 접두어의 키는 전부 받는다.
    for key, value in os.environ.items():
        if key.startswith(("TOSS_", "TELEGRAM_", "MYSQL_", "ALERT_", "AUTOTRADE_")) and not cfg.get(key):
            cfg[key] = value
    for key in ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                "MYSQL_HOST", "MYSQL_PORT", "MYSQL_DATABASE", "MYSQL_USER", "MYSQL_PASSWORD"):
        cfg.setdefault(key, "")
    return cfg


_CFG = load_config()

# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------

API_BASE = "https://openapi.tossinvest.com"
CLIENT_ID = _CFG["TOSS_CLIENT_ID"]
CLIENT_SECRET = _CFG["TOSS_CLIENT_SECRET"]
TG_TOKEN = _CFG["TELEGRAM_BOT_TOKEN"]
# 쉼표로 여러 명을 넣을 수 있다: TELEGRAM_CHAT_ID=111111,222222
# 각 수신자는 봇에게 먼저 /start 를 보내야 한다. 텔레그램 봇은 먼저 말을 건
# 상대에게만 메시지를 보낼 수 있어서, 이 단계를 빼먹으면 chat not found 가 난다.
TG_CHATS = [c.strip() for c in _CFG["TELEGRAM_CHAT_ID"].split(",") if c.strip()]
# 받을 최소 등급 (info | review | action). info 면 시황 요약까지 전부 받는다.
TG_MIN_SEVERITY = _CFG.get("TELEGRAM_MIN_SEVERITY") or "info"

# 백오피스. 같은 .env 를 쓰는 다른 프로젝트의 BACKOFFICE_* 키와 겹치지 않게 ALERT_ 접두어를 쓴다.
BACKOFFICE_HOST = _CFG.get("ALERT_BACKOFFICE_HOST") or "127.0.0.1"
BACKOFFICE_PORT = int(_CFG.get("ALERT_BACKOFFICE_PORT") or 8000)

# 자동매매 — 기본 off. 코드 배포만으로는 절대 live 가 되지 않는다.
#   off : 실행기를 만들지 않는다 (알림만)
#   dry : 정책 검사·주문 의도 기록까지 실제와 같고, 브로커만 가짜(참조가로 가상 체결)
#   live: 실제 주문. DB 킬 스위치(autotrade_enabled)와 종목별 auto_trade 까지 켜져야 나간다
AUTOTRADE_MODE = (_CFG.get("AUTOTRADE_MODE") or "off").strip().lower()
if AUTOTRADE_MODE not in ("off", "dry", "live"):
    raise SystemExit(f"AUTOTRADE_MODE 는 off|dry|live 중 하나: {AUTOTRADE_MODE}")
AUTOTRADE_BUY_BUFFER_PCT = float(_CFG.get("AUTOTRADE_BUY_BUFFER_PCT") or 0.3)   # 매수 지정가 = 신호가 × (1 + 이 %)
AUTOTRADE_BUY_TTL_MIN = int(_CFG.get("AUTOTRADE_BUY_TTL_MIN") or 3)            # 이 시간 안에 미체결이면 취소
# 하드캡: 1회 주문 금액 상한. DB 설정(max_order_amount_*)보다 우선하는 최후의 안전망이다.
AUTOTRADE_HARD_MAX_AMOUNT_KRW = float(_CFG.get("AUTOTRADE_HARD_MAX_AMOUNT_KRW") or 2_000_000)
AUTOTRADE_HARD_MAX_AMOUNT_USD = float(_CFG.get("AUTOTRADE_HARD_MAX_AMOUNT_USD") or 2_000)

# 저장소: 이미 쓰고 있는 MySQL (.env 의 MYSQL_*). 테이블은 alert_ 접두어로 만든다.
MYSQL = {
    "host": _CFG["MYSQL_HOST"] or "127.0.0.1",
    "port": int(_CFG["MYSQL_PORT"] or 3306),
    "user": _CFG["MYSQL_USER"],
    "password": _CFG["MYSQL_PASSWORD"],
    "database": _CFG["MYSQL_DATABASE"],
}

WATCH_HOLDINGS = True          # 보유 조회(읽기 전용). False 면 ENTRY 알림만
ENABLE_EXIT_SIGNAL = True      # 거래량 소진 기반 익절 알림. 끄려면 False

# 감시 종목 — 초기 시딩용. 운영 목록은 MySQL alert_watchlist 이고 백오피스에서 바꾼다.
#   market  : "US" | "KR"  — 장 시간·타임존이 다르다
#   leaders : 선행 바스켓. None 이면 방향 조건을 생략 (개별주)
#   inverse : 인버스면 선행 바스켓 방향을 뒤집는다
#   pair    : 동시 진입을 막을 반대 종목
SEED_WATCHLIST = {
    # 반도체: 신호는 1배 ETF(SOXX)에서 낸다. 3배 상품은 1분봉이 너무 튀어
    # 지표가 지저분하다. SOXX 에서 매수 신호가 뜨면 사람이 SOXL 을 산다.
    "SOXX":   {"market": "US", "leaders": ["NVDA", "AVGO", "TSM", "MU"],
               "inverse": False, "pair": None,
               "note": "레버리지 진입 시 SOXL"},
    # 3배 상품은 보유 중일 때만 감시한다 (매수 신호는 안 낸다).
    # 손절·익절은 자기 캔들로 판단해야 3배 변동폭이 반영된다.
    "SOXL":   {"market": "US", "leaders": None, "inverse": False,
               "pair": "SOXS", "hold_only": True},
    "SOXS":   {"market": "US", "leaders": None, "inverse": True,
               "pair": "SOXL", "hold_only": True},
    "KORU":   {"market": "US", "leaders": ["EWY"],
               "inverse": False, "pair": None},
    "BITX":   {"market": "US", "leaders": ["IBIT"],
               "inverse": False, "pair": None},
    # 개별주. 선행 바스켓 없이 VWAP + RVOL 두 조건으로 판단한다.
    "OKLO":   {"market": "US", "leaders": None,
               "inverse": False, "pair": None},
    "000660": {"market": "KR", "leaders": None,
               "inverse": False, "pair": None, "name": "SK하이닉스"},
    "005930": {"market": "KR", "leaders": None,
               "inverse": False, "pair": None, "name": "삼성전자"},
    "114800": {"market": "KR", "leaders": ["069500"],
               "inverse": True,  "pair": None, "name": "KODEX 인버스"},
}

POLL_INTERVAL_SEC = 30         # 위험 알림(손절·매도) 지연을 줄이려 30초.
                               # ENTRY 는 완성봉 기준이라 이 값과 무관하게 봉당 1회다.
MIN_CALL_GAP_SEC = 0.3         # API 호출 최소 간격 (레이트리밋 회피)
CANDLE_COUNT = 120             # EMA50 계산에 최소 50봉 필요
RVOL_WINDOW = 20               # 프로파일이 없을 때 쓰는 이동평균 구간
RVOL_TRIGGER = 2.0
VWAP_BAND_PCT = 0.15           # VWAP 밴드 하한 (%). 실제는 변동성에 맞춰 커진다
ATR_BAND_MULT = 0.5            # 밴드 = 최근 20봉 평균진폭 × 이 배수 (하한 이상)
STRONG_BAR_MIN = 0.5           # 매수 신호봉 종가가 봉 범위의 이 비율 이상 위치해야 함
LEADER_GAP_PCT = 1.0           # 선행 바스켓 평균 등락률 트리거 (%)
# 선행 바스켓의 최근 N분 변화율. 전일 종가 대비 등락률만 보면 갭업 뒤 흘러내리는
# 중에도 '방향 OK' 가 된다. 우선 추적 CSV(leader_mom)에 기록만 하고, 효과가
# 확인되면 GATE 를 켜서 부호가 방향과 맞을 때만 방향 조건을 인정한다.
LEADER_MOMENTUM_MIN = 5
LEADER_MOMENTUM_GATE = False
STOP_LOSS_PCT = -5.0           # 고정 손절 한도
# 익절은 평단이 아니라 거래량 소진으로 판단한다.
# 평단은 진입가가 아닐 수 있고(물타기·장기분 혼합), 시장은 내 평단을 모른다.
# 거래량이 정점 대비 얼마나 줄었는지가 추세의 실제 연료 상태를 보여준다.
FADE_STRONG_RATIO = 0.4        # 세션 정점 대비 이 아래면 연료 소진 — 익절 신호
FADE_WEAK_RATIO = 0.6          # 이 아래면 둔화 시작 — 일부 익절 검토
FADE_MIN_PEAK = 2.5            # 정점이 이 배수는 넘어야 '터졌다'고 본다
OPEN_EXCLUDE_MIN = 10          # 개장 후 이 분 동안의 봉은 정점 계산에서 제외

# 신호 강도에 따른 익절 비중 제안.
# "애매하다"고만 하면 판단이 그대로 남지만, 비중을 제시하면 행동이 가능해진다.
# 확신이 낮을수록 적게 덜어내고 남은 물량으로 추세를 계속 본다.
EXIT_PORTION_STRONG = "전량"    # 연료 완전 소진
EXIT_PORTION_HALF = "절반"      # 둔화 진행
EXIT_PORTION_THIRD = "1/3"      # 근거만 흐려짐
PROFILE_PAGES = 16             # 거래량 프로파일용 페이지 수 (200봉/페이지, 약 8세션)
MIN_PROFILE_SESSIONS = 3       # 같은 시각 표본 최소 개수
ALERT_COOLDOWN_MIN = 15        # 강한 알림(손절·익절·매도) 재발송 간격
WEAK_COOLDOWN_MIN = 45         # 약한 알림(검토 권유) 재발송 간격. 자주 오면 무시하게 된다

# 매도 계열 알림이 나간 뒤 같은 종목의 매수 알림을 막는 시간.
# 익절 직후 거래량이 잠깐 반등하면 2.0 을 다시 넘길 수 있는데,
# 그건 새로운 추세가 아니라 소진된 추세의 잔진동이다.
REENTRY_BLOCK_MIN = 60
# 매수 신호 직후 익절 알림을 막는 시간.
# 거래량이 한 봉만 튀고 식으면 정점 대비 비율이 곧바로 무너져,
# 산 지 1~2분 만에 '정리하세요'가 나오는 모순이 생긴다.
# 손절·매도(위험 알림)에는 적용하지 않는다.
EXIT_GRACE_MIN = 20
# 매수 신호는 그날 정점 대비 이 비율 이상이어야 한다.
# 정점 5배였던 종목이 2.1배로 반등한 걸 '돌파'로 보면 안 된다.
ENTRY_MIN_PEAK_RATIO = 0.6
CLOSE_WARN_MIN = 30
STATS_REPORT_MIN = 60
SUMMARY_INTERVAL_MIN = 30      # 전 종목 시황 요약 발송 주기

# 불타기(추세 지속 확인 후 추가 매수) 알림
ENABLE_ADD_ON = True
ADDON_MIN_PROFIT_PCT = 2.0     # 이 수익률 이상일 때만. 손실 중 추가매수는 다루지 않는다
ADDON_MAX_COUNT = 1            # 포지션당 최대 알림 횟수. 3배 상품에서 포지션이 눈덩이가 되는 걸 막는다

# 판단 애매 알림: 수익 중인데 추세 근거가 흐려질 때.
# 단타는 근거가 흐려지면 정리하는 게 원칙이지만, 장기 포지션에 같은 기준을
# 적용하면 '승자 조기 청산'이 된다. 이 알림은 단타 종목에만 의미가 있다.
ENABLE_AMBIGUOUS = True        # 근거가 흐려졌을 때 검토 알림. 수익 여부와 무관하게 판단한다

# 신호 추적: ENTRY 발생 후 일정 시간 뒤 가격을 CSV 에 기록한다.
# 임계값(RVOL 2.0, 밴드 0.15%, 선행 1%)이 맞는지는 이 데이터로만 판단할 수 있다.
ENABLE_TRACKING = True
TRACK_MINUTES = [15, 30, 60]   # ENTRY 후 몇 분 뒤를 기록할지
TRACK_FILE = "signal_tracking.csv"
TRADE_FILE = "trade_log.csv"    # 청산된 거래 기록 (일일 성적 집계용)

# 런타임 산출물(로그·추적 CSV·거래 CSV) 위치. 기본은 프로젝트 루트, Docker 에선 볼륨(ALERT_DATA_DIR=/data).
DATA_DIR = Path(_CFG.get("ALERT_DATA_DIR") or os.getenv("ALERT_DATA_DIR") or BASE_DIR)
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = DATA_DIR / "scalping_signals.log"

# ---------------------------------------------------------------------------
# Binance 무기한 선물 5분봉 급락 매수 알림 (run_binance.py, alertbot/binance_crash.py)
# 공개 REST 라 API 키가 필요 없다. 임계값은 2026-09-15 분석(1분·5분·1시간봉, ETC·BTC)에서
# 5분봉 ETC 에 수수료 뒤에도 우위가 확인된 'B +반전봉' 트리거 그대로다.
# ---------------------------------------------------------------------------
BINANCE_FAPI = "https://fapi.binance.com"
BINANCE_SYMBOLS = [s.strip().upper() for s in (_CFG.get("ALERT_BINANCE_SYMBOLS") or "ETCUSDT").split(",") if s.strip()]
BINANCE_INTERVAL = "5m"
BINANCE_KLINES = 1000          # 한 번에 받는 봉 수. 기준 ATR(3일 = 864봉) 계산에 필요
BINANCE_POLL_SEC = 20          # 5분봉이 완성되고 이 초 안에 판정한다
CRASH_LOOKBACK = 48            # 직전 48봉(4시간) 고점 대비 하락폭
CRASH_ATR_MULT = 10.0          # 하락폭 ≥ 기준 ATR × 10 (ETC 는 대략 -2% 이상)
CRASH_RSI_MAX = 30.0           # RSI14 과매도
CRASH_CLOSE_POS_MIN = 0.6      # 신호봉 종가가 봉 범위의 이 위치 이상 (반전봉)
CRASH_BASE_ATR_BARS = 864      # 기준 ATR = 직전 3일 ATR14% 중앙값
CRASH_RVOL_WINDOW = 60         # RVOL 분모: 직전 60봉 거래량 중앙값 (참고 표기)
CRASH_BETA_BTC = 1.35          # BTC 동반 판정용 베타 (ETC 이동 ≈ BTC 이동 × 1.35)
CRASH_COOLDOWN_MIN = 60        # 같은 심볼 재알림 간격
BINANCE_LOG_PATH = DATA_DIR / "binance_signals.log"


def setup_logging(path: Path = None):
    """엔진은 파일+콘솔, 백오피스는 콘솔만. 형식은 원본과 같다.

    Windows 콘솔(cp949)은 이모지를 못 찍어 로그마다 'Logging error' 가 붙는다.
    콘솔 쪽은 못 찍는 글자를 '?' 로 대체하고, 파일은 UTF-8 로 온전히 남긴다.
    """
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    handlers = [logging.StreamHandler(sys.stdout)]
    if path is not None:
        handlers.insert(0, logging.FileHandler(path, encoding="utf-8-sig"))
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        handlers=handlers)
