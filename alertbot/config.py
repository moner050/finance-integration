"""설정 — .env 로드와 임계값 상수.

.env·로그·CSV·SQLite 는 패키지 폴더가 아니라 프로젝트 루트(BASE_DIR)에 둔다.
상대경로로 두면 실행 방식(더블클릭, 다른 폴더에서 실행)에 따라 작업 디렉토리가
달라져 파일이 엉뚱한 곳에 생긴다.

사전 준비
--------
1. 토스증권 WTS > 설정 > Open API 에서 client_id / client_secret 발급
2. 같은 화면 하단 '허용 IP 관리'에 현재 공인 IP 등록 (미등록 IP 는 403)
3. 프로젝트 루트의 .env 파일 (따옴표 없이, 등호 앞뒤 공백 없이). 여기 키는 공용이다 — 시세·가상매매·공개 채널용.
   계정별 토스·Binance·텔레그램 키(live 매매)는 백오피스 '내 API 키' 에서 넣고 DB 에 암호화해 둔다.
     TOSS_CLIENT_ID=...
     TOSS_CLIENT_SECRET=...
     TELEGRAM_PUBLIC_BOT_TOKEN=...
     TELEGRAM_PUBLIC_CHAT_ID=...
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
        if key.startswith(("TOSS_", "TELEGRAM_", "MYSQL_", "ALERT_", "AUTOTRADE_", "OAUTH_GOOGLE_")) and not cfg.get(key):
            cfg[key] = value
    for key in ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET",
                "MYSQL_HOST", "MYSQL_PORT", "MYSQL_DATABASE", "MYSQL_USER", "MYSQL_PASSWORD"):
        cfg.setdefault(key, "")
    return cfg


_CFG = load_config()

# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------

API_BASE = "https://openapi.tossinvest.com"
# 공용 토스 키 — 시세·캔들·장 캘린더·심볼 검증만. 실계좌 보유는 읽지 않는다 (계정별 live 는 그 계정 키로)
CLIENT_ID = _CFG["TOSS_CLIENT_ID"]
CLIENT_SECRET = _CFG["TOSS_CLIENT_SECRET"]
# 공용 채널(공개 텔레그램) — 계정 없는 신호를 전부 보낸다: 시장 신호·장 시작·마감·시황·시스템·가상매매 주문·체결·청산·성적표.
# 쉼표로 여러 채팅을 넣을 수 있고, 각 수신자는 봇에게 먼저 /start 를 보내야 한다. 비워 두면 만들지 않는다.
TG_PUBLIC_TOKEN = (_CFG.get("TELEGRAM_PUBLIC_BOT_TOKEN") or "").strip()
TG_PUBLIC_CHATS = [c.strip() for c in (_CFG.get("TELEGRAM_PUBLIC_CHAT_ID") or "").split(",") if c.strip()]

# 백오피스. 같은 .env 를 쓰는 다른 프로젝트의 BACKOFFICE_* 키와 겹치지 않게 ALERT_ 접두어를 쓴다.
BACKOFFICE_HOST = _CFG.get("ALERT_BACKOFFICE_HOST") or "127.0.0.1"
BACKOFFICE_PORT = int(_CFG.get("ALERT_BACKOFFICE_PORT") or 8000)
# 로그인: Google OAuth. 클라이언트는 .env 에 이미 있는 OAUTH_GOOGLE_* 를 같이 쓴다.
# 리다이렉트 URL 은 OAUTH_GOOGLE_REDIRECT_URL 그대로 보낸다 — Google 콘솔의 '승인된 리디렉션 URI' 와 글자 하나까지 같아야 한다
# (다르면 400 redirect_uri_mismatch). 콜백은 이 URL 의 경로에 열리고, 이 URL 의 호스트·포트가 브라우저가 접속하는 주소다 (https 면 세션 쿠키에 Secure).
# 들어올 수 있는 계정은 DB alert_accounts 에 있는 활성 이메일뿐이다. 첫 관리자만 ALERT_ADMIN_EMAIL 로 정하고 나머지는 '계정' 화면에서 추가한다.
GOOGLE_CLIENT_ID = (_CFG.get("OAUTH_GOOGLE_CLIENT_ID") or "").strip()
GOOGLE_CLIENT_SECRET = (_CFG.get("OAUTH_GOOGLE_CLIENT_SECRET") or "").strip()
GOOGLE_REDIRECT_URL = (_CFG.get("OAUTH_GOOGLE_REDIRECT_URL") or "").strip()
ADMIN_EMAIL = (_CFG.get("ALERT_ADMIN_EMAIL") or "").strip().lower()
SESSION_HOURS = 12
# 계정별 API 키 암호화 마스터 키 (base64url 32바이트). DB 에는 없다 — python -m alertbot.crypto genkey
MASTER_KEY = (_CFG.get("ALERT_MASTER_KEY") or "").strip()

# 자동매매 — 가상매매는 늘 돈다. 코드 배포만으로는 절대 live 가 되지 않는다.
#   off·dry: 공용 가상 장부만 — 확정 신호마다 가상 체결 (실계좌와 격리, 킬 스위치·한도 없음)
#   live   : 가상 장부 + 계정별 실제 주문. 계정의 토스 live 스위치와 종목별 auto_trade 까지 켜져야 그 계정 계좌로 나간다
AUTOTRADE_MODE = (_CFG.get("AUTOTRADE_MODE") or "off").strip().lower()
if AUTOTRADE_MODE not in ("off", "dry", "live"):
    raise SystemExit(f"AUTOTRADE_MODE 는 off|dry|live 중 하나: {AUTOTRADE_MODE}")
AUTOTRADE_BUY_BUFFER_PCT = float(_CFG.get("AUTOTRADE_BUY_BUFFER_PCT") or 0.3)   # 매수 지정가 = 신호가 × (1 + 이 %)
AUTOTRADE_BUY_TTL_MIN = int(_CFG.get("AUTOTRADE_BUY_TTL_MIN") or 3)            # 이 시간 안에 미체결이면 취소
# 하드캡: 1회 주문 금액 상한. DB 설정(max_order_amount_*)보다 우선하는 최후의 안전망이다.
AUTOTRADE_HARD_MAX_AMOUNT_KRW = float(_CFG.get("AUTOTRADE_HARD_MAX_AMOUNT_KRW") or 2_000_000)
AUTOTRADE_HARD_MAX_AMOUNT_USD = float(_CFG.get("AUTOTRADE_HARD_MAX_AMOUNT_USD") or 2_000)
# 가상매매 1회 매수 금액 — 종목 금액(auto_amount)이 0 일 때. 가상은 최소 1주를 산다 (한 주가 이 금액보다 비싸도 기록이 남게)
VIRTUAL_AMOUNT = {"KRW": 1_000_000, "USD": 1_000}

# 저장소: 이미 쓰고 있는 MySQL (.env 의 MYSQL_*). 테이블은 alert_ 접두어로 만든다.
MYSQL = {
    "host": _CFG["MYSQL_HOST"] or "127.0.0.1",
    "port": int(_CFG["MYSQL_PORT"] or 3306),
    "user": _CFG["MYSQL_USER"],
    "password": _CFG["MYSQL_PASSWORD"],
    "database": _CFG["MYSQL_DATABASE"],
}

ENABLE_EXIT_SIGNAL = True      # 거래량 소진 기반 익절 알림. 끄려면 False

# 감시 종목 — 초기 시딩용. 운영 목록은 MySQL alert_watchlist 이고 백오피스에서 바꾼다.
#   market  : "US" | "KR"  — 장 시간·타임존이 다르다
#   leaders : 선행 바스켓. None 이면 방향 조건을 생략 (개별주)
#   inverse : 인버스면 선행 바스켓 방향을 뒤집는다
#   pair    : 동시 진입을 막을 반대 종목
#   day_trade: 마감 CLOSE_WARN_MIN 분 전 정리 알림(자동매매면 전량 매도) 대상. 오버나잇을 피할 레버리지 상품에만
SEED_WATCHLIST = {
    # 반도체: 신호는 1배 ETF(SOXX)에서 낸다. 3배 상품은 1분봉이 너무 튀어
    # 지표가 지저분하다. SOXX 에서 매수 신호가 뜨면 사람이 SOXL 을 산다.
    # 페어: SOXS(인버스)를 들고 있으면 SOXX 매수 신호를 내지 않는다. 페어 검사는 매수 신호에서만 돌므로
    # 매수 신호가 없는 hold_only 상품(SOXL)에 걸어 두면 아무 일도 안 한다.
    "SOXX":   {"market": "US", "leaders": ["NVDA", "AVGO", "TSM", "MU"],
               "inverse": False, "pair": "SOXS",
               "note": "레버리지 진입 시 SOXL"},
    # 3배 상품은 보유 중일 때만 감시한다 (매수 신호는 안 낸다).
    # 손절·익절은 자기 캔들로 판단해야 3배 변동폭이 반영된다.
    "SOXL":   {"market": "US", "leaders": None, "inverse": False,
               "pair": None, "hold_only": True, "day_trade": True},
    "SOXS":   {"market": "US", "leaders": None, "inverse": True,
               "pair": "SOXX", "hold_only": True, "day_trade": True},
    "KORU":   {"market": "US", "leaders": ["EWY"],
               "inverse": False, "pair": None, "day_trade": True},
    "BITX":   {"market": "US", "leaders": ["IBIT"],
               "inverse": False, "pair": None, "day_trade": True},
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
ATR_BAND_MULT_KR = 1.0         # 한국 종목의 배수. 2026-09-17 3개월 재생에서 한국만 밴드 2배가 표본 안팎 모두 개선 (미국은 악화)
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
FADE_STRONG_RATIO = 0.4        # 세션 정점 대비 이 아래면 연료 소진 — 익절 신호 (수익 중일 때만. 손실 중 '정리' 는 75건 전부 손실이라 뺐다)
FADE_WEAK_RATIO = 0.6          # 이 아래면 둔화 시작 — 일부 익절 검토 (수익 중일 때만)
FADE_MIN_PEAK = 2.5            # 정점이 이 배수는 넘어야 '터졌다'고 본다
OPEN_EXCLUDE_MIN = 10          # 개장 후 이 분 동안의 봉은 정점 계산에서 제외하고 매수 신호도 내지 않는다 (VWAP 이 아직 봉 한두 개)
FADE_BARS = 3                  # 거래량 소진 판정에 쓰는 최근 완성봉 수. 1분봉 하나는 조용한 1분에 '전량 정리' 를 만든다
TRAIL_MIN_PROFIT_PCT = 1.0     # 이 수익률 이상이면 매도선을 기준봉 저점과 밴드 하단 중 높은 쪽으로 (수익 반납 축소)
ENTRY_SKIP_BEAR_EMA = True     # EMA 역배열(9<20<50)에서는 매수 신호를 내지 않는다. 추적 7건 중 역배열·혼조 진입이 모두 음수 — 표본이 쌓이면 재검토
# 신호 확신도. 요건(방향·돌파·기준선 위·강봉)을 다 채운 매수 신호도 확인 항목 — EMA 정배열 · (선행 바스켓이 있으면) 선행 모멘텀이
# 방향과 같음 — 이 ENTRY_CONFIRM_MIN 개(항목 수가 더 적으면 전부) 이상이어야 '매수하세요' 다. 모자라면 '매수 대기하세요'
# (ENTRY_WATCH, 자동매매 대상 아님)로 내고, 대기 중 확인 항목이 채워지면 그때 '매수하세요' 로 승격한다.
# 2026-09-17 3개월 재생: 'RSI 상승' 은 확정 신호의 99% 가 채워 변별력이 없고, '거래량 3배↑' 는 미충족 신호가 오히려 나아 둘 다 뺐다.
ENTRY_CONFIRM_MIN = 3
# 추격 진입 배제 — 같은 재생에서 두 시장·표본 안팎 모두 방향이 같았던 유일한 축. 이미 많이 오른 자리의 돌파는 다음날까지 −1~−8% 였다.
# 오늘 갭(시가/전일 종가), 현재가/전일 종가, 전일 등락(종가/시가), 5세션 수익률, 20세션 평균 대비 — 하나라도 넘으면 매수 신호를 내지 않는다.
# 일봉 이력이 모자라면 그 항목은 건너뛴다 (막지 않는다).
ENTRY_MAX_GAP_PCT = 1.0
ENTRY_MAX_VS_PREV_PCT = 3.0
ENTRY_MAX_PREV_DAY_PCT = 2.0
ENTRY_MAX_RET5_PCT = 8.0
ENTRY_MAX_MA20_PCT = 10.0
DAILY_COUNT = 30               # 일봉 국면용으로 세션당 한 번 받는 일봉 수 (20세션 평균 + 여유)
ENTRY_MAX_MIN_FROM_OPEN = 240  # 개장 뒤 이 분이 지나면 새 매수 신호를 내지 않는다 (오후 진입은 당일·다음날 모두 손실)
# 목표가 익절: 신호가(가상 평단) 대비 이 % 에 닿으면 확정 익절. 당일 최대 이익 중앙값이 +1.4~1.6% 인데 매도선 이탈만으로 청산하면
# 승률 13% 로 털렸다. 종가 기준 매도선 + 목표 +1% 가 재생한 청산 변형 중 최선 (승률 33~39%)
EXIT_TARGET_PCT = 1.0
# 매도 확신도: 종가가 매도선을 밴드 폭보다 깊이 뚫었거나 거래량이 RVOL_TRIGGER 배 이상이면 '매도하세요', 아니면 '매도 대기하세요'
# (EXIT_WATCH). 거래량 소진은 가격이 기준선 아래로 내려서야 '익절하세요', 중립대면 대기. 손절 한도(-5%)는 항상 확정이다.

# 신호 강도에 따른 익절 비중 제안.
# "애매하다"고만 하면 판단이 그대로 남지만, 비중을 제시하면 행동이 가능해진다.
# 확신이 낮을수록 적게 덜어내고 남은 물량으로 추세를 계속 본다.
EXIT_PORTION_STRONG = "전량"    # 연료 완전 소진
EXIT_PORTION_HALF = "절반"      # 둔화 진행
EXIT_PORTION_THIRD = "1/3"      # 근거만 흐려짐
# 토스 미국 1분봉엔 주간거래·프리·애프터 봉이 섞여 16페이지(3200봉)는 약 2세션뿐 — 시각당 표본 2개로
# MIN_PROFILE_SESSIONS 에 못 미쳐 늘 이동평균으로 떨어졌다. 40페이지면 미국 6세션·한국 11세션 (종목당 약 12초, 세션당 한 번)
PROFILE_PAGES = 40             # 거래량 프로파일용 페이지 수 (200봉/페이지)
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
# 매수 대기 신호 뒤 이 시간 안에 확인 항목이 채워지지 않으면 대기를 거둔다 (매수 대기 만료).
# 돌파봉의 근거는 오래가지 않는다 — 한 시간 뒤의 승격은 늦은 진입이다. 확정 신호('매수하세요')는 곧바로 신호 포지션이라 만료가 없다.
# 첫 반복(15분)은 한 번 나가고, 그 다음 반복 전에 만료된다.
ENTRY_PENDING_MAX_MIN = 30
# 청산 신호(손절·매도·익절) 반복 알림의 최대 간격. 15분에서 두 배씩 늘린다 (15→30→60→120→240).
# 같은 사유가 15분마다 종일 오면 진짜 위험 알림도 흘려보게 된다. 장기 보유로 정한 -30% 포지션이 그랬다.
EXIT_REPEAT_MAX_MIN = 240
# 청산대기 해제: 손절 한도에서 이 % 넘게 회복하면 (-5% → -3% 위) 보유로 돌아간다.
# 매도선 이탈은 밴드만큼 되올라오면, 거래량 소진은 가격이 기준선 위로 올라서면 해제한다.
STOP_RECOVER_PCT = 2.0
# 매수 신호는 그날 정점 대비 이 비율 이상이어야 한다.
# 정점 5배였던 종목이 2.1배로 반등한 걸 '돌파'로 보면 안 된다.
ENTRY_MIN_PEAK_RATIO = 0.6
CLOSE_WARN_MIN = 30
STATS_REPORT_MIN = 60
SUMMARY_INTERVAL_MIN = 30      # 전 종목 시황 요약 발송 주기 (주식·코인 공통)
# 미국 프리마켓 분석 (04:00 ET~개장). 시황과 같은 30분 칸(KST :00/:30)에 붙는다. 매수·매도 알림은 내지 않는다
PREMARKET_GAP_PCT = 0.5        # 전일 종가 대비 이 % 이상 벌어져야 갭상승/갭하락으로 부른다
PREMARKET_MIN_SESSIONS = 2     # 거래량 배수를 내려면 과거 프리마켓이 이만큼 있어야 한다
PREMARKET_PAGES = 2            # 오늘 프리마켓 봉 조회 페이지 (200봉/페이지, 프리마켓 330분)

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
# 신호 포지션(확정 매수 신호 → 확정 청산 신호)의 모의 성적. 신호가와 청산 신호 시점 가격 기준이라 체결·수수료는 없다.
# 주식은 장 마감 성적표, 코인은 자정(KST) 성적표의 근거다.
SIGNAL_TRADE_FILE = "signal_trades.csv"
BINANCE_SIGNAL_TRADE_FILE = "binance_signal_trades.csv"

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
CRASH_STOP_PCT = 3.0           # 손절 참고선: 신호봉 종가 -3% 재난 손절. 저가-2ATR(진입 대비 0.7%)은 5분봉 스윕 구간이라 폐기 (2026-09-15 레버리지 분석)
CRASH_HOLD_HOURS = 8           # 보유 한도. 목표 지정가는 없다. 5h → 8h (2026-09-15 표본 확장 257일: 8h 만 두 구간 모두 양수)
CRASH_H4_FILTER = True         # 4시간봉 EMA9 ≤ EMA21(하락 배열)일 때만 알림·진입. 상승 배열 중 급락 -0.41%(t -2.0), 하락 배열 +0.69%(t 3.1)
CRASH_H4_KLINES = 120          # 4시간봉 배열 판정용 봉 수 (20일). 급락 조건이 성립한 때만 조회한다
BINANCE_LOG_PATH = DATA_DIR / "binance_signals.log"

# 상위 봉 추종 알림 (alertbot/binance_follow.py). 같은 워커 프로세스가 사양(FOLLOW_SPECS)마다 워커 하나씩 돌린다.
# 스윙 분석 「스윙의 급등과 급락」의 'M2' 계열: 급변 뒤 첫 눌림(반등)에서 EMA9 를 되찾는(잃는) 봉에 진입 후보.
# 급등 숏은 어느 봉에서도 손실이라 만들지 않는다. 아래 SURGE_* 는 4시간봉 BTC 급등 추종 사양의 값이다.
SURGE_SYMBOLS = [s.strip().upper() for s in (_CFG.get("ALERT_BINANCE_SURGE_SYMBOLS") or "BTCUSDT").split(",") if s.strip()]
FOLLOW_KLINES = 400            # 사양당 받는 봉 수. 기준 ATR 창(4시간봉 30일 = 180봉) + 룩백 + 일봉 EMA200 에 충분
SURGE_LOOKBACK = 30            # 직전 30봉(5일) 저점 대비 상승폭
SURGE_ATR_MULT = 6.0           # 상승폭 ≥ 기준 ATR × 6 (BTC 는 대략 +9% 이상)
SURGE_RSI_MIN = 70.0           # RSI14 과매수 — 스윙에서는 추종 근거
SURGE_BASE_ATR_BARS = 180      # 기준 ATR = 직전 30일 ATR14% 중앙값
SURGE_REENTRY_BARS = 10        # 급등 뒤 이 봉 안의 EMA9 재돌파만 진입 후보
SURGE_RVOL_WINDOW = 60         # RVOL 분모 (참고 표기)
SURGE_STOP_ATR = 2.5           # 손절 참고선: 눌림 저점 - 2.5 기준 ATR (진입 대비 중앙 -6%). ±8ATR 브래킷은 58건 중 4건만 걸려 손절 역할을 못 했다
SURGE_HOLD_BARS = 42           # 보유 한도 7일. 단계별 재알림 간격도 같다
SURGE_REQUIRE_BULL = True      # 일봉 종가가 EMA200 위일 때만 알린다 (약세 국면은 기대값 음수)
SURGE_FUNDING_WARN = 0.0003    # 펀딩 > 3bp/8h(연 30%+)면 과열 표기
# 일봉 사양 심볼: 급등 추종 롱(BTC 19건 승률 84% 순 +7.4%) · 급락 추종 숏(ETC 9건 중 8건 이익 순 +3.5%, 약세 국면만)
SURGE_1D_SYMBOLS = [s.strip().upper() for s in (_CFG.get("ALERT_BINANCE_SURGE_1D_SYMBOLS") or "BTCUSDT").split(",") if s.strip()]
CRASHFOLLOW_1D_SYMBOLS = [s.strip().upper() for s in (_CFG.get("ALERT_BINANCE_CRASHFOLLOW_1D_SYMBOLS") or "ETCUSDT").split(",") if s.strip()]
# 사양: side 롱은 룩백 저점 대비 상승·RSI ≥ rsi, 숏은 룩백 고점 대비 하락·RSI ≤ rsi. regime 은 일봉 EMA200 기준 필요 국면.
# hold_bars 는 보유 한도이자 단계별 재알림 간격, kinds 는 (관찰, 진입) 신호 종류. stop 은 손절 참고선 규칙 — ('pull_atr', m) 은 눌림 저점/반등 고점
# ∓ m 기준 ATR, ('pct', p) 는 진입(신호 종가) ∓ p%. 목표 지정가는 두지 않는다(두면 세 사양 모두 평균 하락 — 2026-09-15 레버리지 분석).
FOLLOW_SPECS = [
    {"name": "급등 추종", "side": "long", "interval": "4h", "label": "4시간봉", "symbols": SURGE_SYMBOLS,
     "lookback": SURGE_LOOKBACK, "atr_mult": SURGE_ATR_MULT, "rsi": SURGE_RSI_MIN, "base_bars": SURGE_BASE_ATR_BARS,
     "reentry_bars": SURGE_REENTRY_BARS, "hold_bars": SURGE_HOLD_BARS, "stop": ("pull_atr", SURGE_STOP_ATR),
     "regime": "bull" if SURGE_REQUIRE_BULL else None, "kinds": ("SURGE_WATCH", "SURGE_ENTRY")},
    # 일봉 급등 추종 롱: 20봉 저점 대비 ≥ 기준ATR(90일 중앙값)×4 (BTC 약 +16%), 재돌파 10봉 안, 보유 20일, 손절 진입 -10% (이긴 거래의 최대 역행 8.2%)
    {"name": "급등 추종", "side": "long", "interval": "1d", "label": "일봉", "symbols": SURGE_1D_SYMBOLS,
     "lookback": 20, "atr_mult": 4.0, "rsi": 70.0, "base_bars": 90, "reentry_bars": 10, "hold_bars": 20, "stop": ("pct", 10.0),
     "regime": "bull", "kinds": ("SURGE_WATCH_1D", "SURGE_ENTRY_1D")},
    # 일봉 급락 추종 숏: 20봉 고점 대비 ≥ 기준ATR×4 (ETC 약 -27%), 첫 반등 뒤 EMA9 재이탈, 약세 국면만, 손절 진입 +25%
    # (반등 고점은 8건 중 6건에서 5~7% 더 뚫린다). 이 값은 공용 기준(격리 3배)이고, 배율이 높은 계정은 청산선 안으로 자동으로 당겨진다 — stop_for_leverage()
    {"name": "급락 추종", "side": "short", "interval": "1d", "label": "일봉", "symbols": CRASHFOLLOW_1D_SYMBOLS,
     "lookback": 20, "atr_mult": 4.0, "rsi": 30.0, "base_bars": 90, "reentry_bars": 10, "hold_bars": 20, "stop": ("pct", 25.0),
     "regime": "bear", "kinds": ("CRASH_WATCH_1D", "CRASH_SHORT_1D")},
]

# 급변 감시 (alertbot/binance_scan.py) — 거래대금 상위 코인의 급등·급락 감지 알림. 급등은 공용 가상 장부의 소진 숏(아래)으로 이어진다. 같은 워커 프로세스가 돌린다.
# 2026-05-17~09-17 4개월 분석(보고서 「최근 4개월 코인 신호 재검증」): 하루 알림 중앙값 10건에 맞춘 설계 가운데
# 큰 움직임(24시간 극값 대비 +15% / −12%)을 가장 많이 잡은 조합이다. 알림이 많으면 SCAN_ATR_MULT 를 올린다 (최근 두 달은 하루 중앙 13건).
SCAN_TOP_N = 30                # 직전 24시간 거래대금 상위 N 개 USDT 무기한 코인 (백오피스 '종목' 에서 추가·제외)
SCAN_EXCLUDE = ("USDCUSDT", "PAXGUSDT", "XAUTUSDT")   # 스테이블·금 연동 토큰 — 거래소 분류는 코인이지만 급변 감시 대상이 아니다
SCAN_REFRESH_MIN = 60          # 유니버스 갱신 주기. 15분 갱신도 포착률이 같았다
SCAN_INTERVAL = "1h"           # 판정 봉. 15분봉은 포착률이 비슷하고 알림이 몰리는 날이 더 많았다 (하루 최대 51건 vs 26건)
SCAN_WINDOW_BARS = 4           # 직전 4봉(4시간) 저점·고점 대비 이동. 한 번 알린 코인은 방향과 무관하게 같은 길이 동안 쉰다
SCAN_BASE_ATR_BARS = 720       # 기준 ATR = 직전 30일 ATR14% 중앙값. 7일은 한 주 내내 들썩인 코인(9-16 LSK)의 급등을 놓쳤다
SCAN_ATR_MULT = 10.3           # 급변 문턱 (기준 ATR 배수) — 4개월 하루 알림 중앙값 10건
SCAN_KLINES = 1000             # 코인당 받는 1시간봉 (기준 ATR 30일 + 여유, weight 5)
SCAN_MAX_LINES = 10            # 한 알림에 싣는 코인 수. 나머지는 '외 N종목'
# 급등 소진 숏 SCAN_FADE — 공용 가상 장부와 계정 live 둘 다 (2026-09-18 live 추가). 대상 코인이 감지 때마다 달라져,
# 진입할 때 그 심볼의 필터·격리·배율을 그 자리에서 건다. 크기가 작아(자본의 1/8) 자본이 작으면 주문 최소 단위에 걸려 보류된다.
# 2026-09-17 4개월 분석(보고서 「급등 코인 소진 숏」): 급등 감지 코인은 72시간 중앙 −11.6% 흘러내렸지만 감지 직후 24시간 안에 중앙 +15% 더 올라
# 곧바로 숏은 손절에 걸렸다. 1시간 종가가 EMA50 아래로 꺾인 뒤의 숏이 설정 주변(EMA20·50, 대기 24·48h, 손절 15~20%, 보유 48~72h)에서 고르게 이익 —
# 최종안 405건 건당 +1.68% (수수료·펀딩 뒤)·승률 59.5%·최대 낙폭 −14.7%. 설정 선택에 전 기간을 봐서, 전진 검증 기대치는 건당 +0.66~1.15% 다.
SCAN_FADE_EMA = 50             # 감지 뒤 1시간봉 종가가 이 EMA 아래로 마감하면 진입. EMA10 은 급등 중의 짧은 눌림에 걸려 손실
SCAN_FADE_WAIT_HOURS = 48      # 감지 뒤 이 시간 안에 안 꺾이면 포기 (12시간은 짧았다)
SCAN_FADE_STOP_PCT = 20.0      # 손절: 진입 기준가 +20% (마크 가격)
SCAN_FADE_TP_PCT = 20.0        # 목표가: 진입 기준가 −20% (마크 가격)
SCAN_FADE_HOLD_HOURS = 48      # 보유 한도
SCAN_FADE_MAX_OPEN = 8         # 이 전략 동시 보유 상한 (같은 코인은 하나). 크기는 BINANCE_TRADE_LEVERAGE 의 1/8


# Binance 자동매매 (alertbot/binance_trade.py) — 공용 가상 장부는 늘 돈다 (공개 시세로 가상 체결, 키 불필요).
#   off·dry: 가상 장부만
#   live   : 가상 장부 + 계정별 실제 주문 (binance_broker). 그 계정 키 + 기동(또는 키 변경) 때 헤지 모드·격리·배율 설정 +
#            계정의 Binance live 스위치가 켜져야 진입한다. 자본은 계정별(백오피스). 공용 Binance 키는 읽지 않는다 — 시세는 공개 API 다
BINANCE_TRADE_MODE = (_CFG.get("ALERT_BINANCE_TRADE_MODE") or "off").strip().lower()
if BINANCE_TRADE_MODE not in ("off", "dry", "live"):
    raise SystemExit(f"ALERT_BINANCE_TRADE_MODE 는 off|dry|live 중 하나: {BINANCE_TRADE_MODE}")
# 공용 가상 장부의 격리 배율 — 공용 채널 알림(손절 참고선)의 기준이다. 3배의 청산 거리는 약 32.8% 라 위 손절(−3/−6/−10/+25%)이 전부 그 안에 든다.
# 계정 live 는 이 값이 아니라 **계정별 배율**(alert_accounts.binance_leverage, 백오피스 자동매매 화면)을 쓴다.
# 어느 쪽이든 진입 크기는 BINANCE_TRADE_LEVERAGE 가 정한다 — 격리 배율은 증거금과 청산 거리만 바꾼다.
BINANCE_TRADE_EXCHANGE_LEV = 3
# 백오피스에서 넣을 수 있는 계정 배율. 상한 5배 — 7배면 손절 상한이 8.8% 로 내려가 일봉 롱(−10%)까지 잘린다.
# 5배(상한 14.5%)는 일봉 롱·숏·급락 매수는 온전하고 급등 소진 숏(+20%)만 잘리는 마지막 지점이다. 소진 숏까지 온전하려면 3배.
BINANCE_LEVERAGE_RANGE = (1, 5)
BINANCE_MAINT_MARGIN_PCT = 0.5           # 유지증거금 가정 (BTC 0.40 · ETC 0.50 — 큰 쪽으로 잡는다). 청산 거리 = 100/배율 − 이 값
# 손절은 청산선에서 이만큼(%p) 안쪽에 둔다. 넘는 손절은 여기까지 당긴다. 손절은 마크 가격 트리거라 보통 먼저 체결되지만,
# 갭·플래시 크래시에서는 트리거와 시장가 체결 사이가 벌어진다 (레버리지 분석: 최종가 꼬리가 마크보다 최대 20~28%).
# 2.0 → 5.0 (2026-09-18): 7배에서 일봉 롱 −10% 가 여유 3.8%p 로 청산선에 너무 붙어 있었다. 5.0 이면 7배 손절 상한이 8.8% —
# 그 전략에서 이긴 거래의 최대 역행 8.2% 보다 아직 밖이라 승자를 털어내지는 않는다 (여유는 0.6%p 뿐이다).
BINANCE_STOP_LIQ_MARGIN_PCT = 5.0


def liq_distance_pct(leverage: float) -> float:
    """격리 마진의 청산 거리(%). 진입가에서 이만큼 역행하면 청산이다."""
    return 100.0 / float(leverage) - BINANCE_MAINT_MARGIN_PCT


def max_stop_pct(leverage: float) -> float:
    """그 배율에서 허용되는 손절 폭(%) — 청산선에서 BINANCE_STOP_LIQ_MARGIN_PCT 안쪽."""
    return max(0.1, liq_distance_pct(leverage) - BINANCE_STOP_LIQ_MARGIN_PCT)
BINANCE_TRADE_CAPITAL = float(_CFG.get("ALERT_BINANCE_TRADE_CAPITAL") or 1000)   # 가상 장부의 전략별 배분 자본 (USDT). live 는 계정별 자본
# 전략(진입 신호 종류)별 유효 배율 = 명목가 ÷ 배분 자본. 레버리지 분석의 시작값 — 최대는 3 / 1.5 / 2 / 1.
# SCAN_FADE 는 포지션당 1/8 (동시 SCAN_FADE_MAX_OPEN 개를 다 열면 자본 1배)
BINANCE_TRADE_LEVERAGE = {"CRASH_BUY": 2.0, "SURGE_ENTRY": 1.0, "SURGE_ENTRY_1D": 1.5, "CRASH_SHORT_1D": 0.5, "SCAN_FADE": 0.125}
# 계정 live 로 실제 주문을 내는 전략. 여기 없는 전략은 공용 가상 장부에서만 돌고 live 한도(자본 합)에도 넣지 않는다.
# SCAN_FADE 는 대상 코인이 그때그때 정해져(거래대금 상위 30) 진입할 때마다 그 심볼의 필터·격리·배율을 건다 (binance_broker.ensure).
BINANCE_LIVE_STRATEGIES = ("CRASH_BUY", "SURGE_ENTRY", "SURGE_ENTRY_1D", "CRASH_SHORT_1D", "SCAN_FADE")
BINANCE_TRADE_MAX_OPEN = {"SCAN_FADE": SCAN_FADE_MAX_OPEN}    # 전략별 동시 보유 상한 (없는 전략은 심볼마다 하나)
BINANCE_TRADE_FEE = 0.0005                                    # 테이커 편도
BINANCE_TRADE_SLIP = {"ETCUSDT": 0.0005, "BTCUSDT": 0.0002}   # dry 체결 슬리피지 편도 (없는 심볼은 0.0005)
BINANCE_TRADE_MAX_TOTAL_LEV = 3.0                             # 합산 명목 ≤ 자본 합 × 3
BINANCE_TRADE_DAILY_LOSS_PCT = 6.0                            # 오늘 실현손실이 자본 합의 이 % 를 넘으면 신규 진입 중단


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
