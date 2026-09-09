"""
단타 알림 시스템 — 토스증권 Open API (알림 전용, 주문 없음)
==========================================================

이 스크립트는 절대 주문을 내지 않는다.
주문 API(POST /api/v1/orders)는 코드에 포함돼 있지 않다.
호출하는 API 는 전부 읽기 전용이다: 토큰, 캔들, 현재가, 계좌 목록, 보유 주식, 장 캘린더.

사전 준비
--------
1. 토스증권 WTS > 설정 > Open API 에서 client_id / client_secret 발급
2. 같은 화면 하단 '허용 IP 관리'에 현재 공인 IP 등록 (미등록 IP 는 403)
3. 이 파일과 같은 폴더에 .env 파일 (따옴표 없이, 등호 앞뒤 공백 없이):
     TOSS_CLIENT_ID=...
     TOSS_CLIENT_SECRET=...
     TELEGRAM_BOT_TOKEN=...
     TELEGRAM_CHAT_ID=...
4. Windows 는 IANA 타임존 DB 가 없어 zoneinfo 가 실패할 수 있다.
     pip install tzdata
   설치하지 않으면 고정 오프셋으로 대체하되, 미국 서머타임 전환 주간에 1시간 오차가 날 수 있다.

알림 종류
--------
  ENTRY      매수 타점 — 선행 바스켓 방향 + RVOL 2.0 돌파 + VWAP 위 지지
  STOP       평단 대비 -5% (고정 한도)
  VWAP_EXIT  VWAP 밴드 하단 이탈 (보유 중). 거래량 동반 여부를 함께 표기
  익절하세요      거래량이 세션 정점 대비 40% 아래로 — 상승 연료 소진
  일부 익절 검토   거래량이 정점 대비 60% 아래로 — 둔화 시작
  CLOSE      장 마감 30분 전 미청산 (한국·미국 각각)

주요 설계 결정
------------
- 지표는 '완성된 봉'으로만 계산한다. 마지막 봉은 진행 중이라 거래량이 부분값이다.
- 시각 비교는 전부 거래소 현지시각으로 한다. UTC 문자열을 그대로 자르면
  서머타임 전환 시 같은 09:31 이 다른 시각의 봉과 짝지어진다.
- 보유 조회가 실패하면 그 사이클은 판단을 보류한다. 보유 여부를 모르면
  ENTRY(미보유 전제)와 STOP(보유 전제) 어느 쪽도 신뢰할 수 없다.
"""

import os
import time
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# 설정 로드
# ---------------------------------------------------------------------------


def load_config() -> dict:
    """스크립트와 같은 폴더의 .env 를 읽고, 없는 항목은 환경변수로 채운다."""
    cfg = {}
    env_path = Path(__file__).with_name(".env")
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip("'\"")
    for key in ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET",
                "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        if not cfg.get(key):
            cfg[key] = os.getenv(key, "")
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

WATCH_HOLDINGS = True          # 보유 조회(읽기 전용). False 면 ENTRY 알림만
ENABLE_EXIT_SIGNAL = True      # 거래량 소진 기반 익절 알림. 끄려면 False

# 감시 종목
#   market  : "US" | "KR"  — 장 시간·타임존이 다르다
#   leaders : 선행 바스켓. None 이면 방향 조건을 생략 (개별주)
#   inverse : 인버스면 선행 바스켓 방향을 뒤집는다
#   pair    : 동시 진입을 막을 반대 종목
WATCHLIST = {
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
TICKERS = list(WATCHLIST.keys())

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

# 로그는 스크립트와 같은 폴더에 쓴다.
# 상대경로로 두면 실행 방식(더블클릭, 다른 폴더에서 실행)에 따라
# 작업 디렉토리가 달라져 로그가 엉뚱한 곳에 생긴다.
LOG_PATH = Path(__file__).with_name("scalping_signals.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8-sig"),
              logging.StreamHandler()],
)
log = logging.getLogger("scalper")

# ---------------------------------------------------------------------------
# 타임존
# ---------------------------------------------------------------------------

_TZ = {}
try:
    from zoneinfo import ZoneInfo
    _TZ = {"US": ZoneInfo("America/New_York"), "KR": ZoneInfo("Asia/Seoul")}
except Exception:
    _TZ = {}
    log.warning("zoneinfo 사용 불가 — 고정 오프셋으로 대체한다. "
                "정확한 처리를 위해 `pip install tzdata` 를 권장한다.")


def _us_offset(dt_utc: datetime) -> timedelta:
    """zoneinfo 가 없을 때의 미국 동부 오프셋 근사. 3월 둘째 일요일~11월 첫째 일요일 EDT."""
    y = dt_utc.year
    march = datetime(y, 3, 1, tzinfo=timezone.utc)
    second_sun_mar = march + timedelta(days=(6 - march.weekday()) % 7 + 7)
    nov = datetime(y, 11, 1, tzinfo=timezone.utc)
    first_sun_nov = nov + timedelta(days=(6 - nov.weekday()) % 7)
    return timedelta(hours=-4) if second_sun_mar <= dt_utc < first_sun_nov else timedelta(hours=-5)


def to_local(dt: datetime, market: str) -> datetime:
    """UTC-aware datetime 을 거래소 현지시각으로."""
    if market in _TZ:
        return dt.astimezone(_TZ[market])
    off = timedelta(hours=9) if market == "KR" else _us_offset(dt)
    return dt.astimezone(timezone(off))


def now_local(market: str) -> datetime:
    return to_local(datetime.now(timezone.utc), market)


_naive_warned = False


def parse_ts(ts: str, market: str):
    """API 타임스탬프 -> 거래소 현지시각 datetime. 실패 시 None.

    타임존 정보가 없는 문자열이 오면 UTC 로 간주한다. 이 가정이 틀리면
    프로파일 시각이 통째로 어긋나므로, 기동 시 원본 타임스탬프를 로그로 찍어
    사람이 확인하도록 한다.
    """
    global _naive_warned
    if not ts:
        return None
    s = str(ts).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        if not _naive_warned:
            log.warning("타임스탬프에 타임존 정보가 없다 — UTC 로 간주한다: %s", ts)
            _naive_warned = True
        dt = dt.replace(tzinfo=timezone.utc)
    return to_local(dt, market)


# ---------------------------------------------------------------------------
# 토스증권 API (읽기 전용)
# ---------------------------------------------------------------------------


class TossReadOnlyClient:
    """읽기 전용. 주문 관련 메서드는 의도적으로 없다."""

    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self._token = None
        self._expires_at = datetime.min.replace(tzinfo=timezone.utc)
        self.account_seq = None
        self._last_call = 0.0
        self.session = requests.Session()

    def _ensure_token(self):
        now = datetime.now(timezone.utc)
        if self._token and now < self._expires_at - timedelta(minutes=5):
            return
        resp = self.session.post(
            f"{API_BASE}/oauth2/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials",
                  "client_id": self.client_id, "client_secret": self.client_secret},
            timeout=10)
        if resp.status_code == 403:
            raise SystemExit("403 — 허용 IP 미등록. WTS > 설정 > Open API > 허용 IP 관리.")
        resp.raise_for_status()
        data = resp.json()
        if "access_token" not in data:
            raise SystemExit(f"토큰 발급 실패: {data}\n.env 의 client_id/secret 을 확인할 것.")
        self._token = data["access_token"]
        self._expires_at = now + timedelta(seconds=int(data.get("expires_in", 3600)))
        log.info("토큰 발급 완료")

    def _get(self, path: str, params: dict = None, with_account: bool = False):
        """GET 전용. 실패 시 None (빈 결과 {} 와 구분해야 보유 조회 실패를 감지할 수 있다)."""
        gap = time.monotonic() - self._last_call
        if gap < MIN_CALL_GAP_SEC:
            time.sleep(MIN_CALL_GAP_SEC - gap)
        self._last_call = time.monotonic()

        for attempt in range(3):
            try:
                self._ensure_token()
                headers = {"Authorization": f"Bearer {self._token}"}
                if with_account:
                    headers["X-Tossinvest-Account"] = str(self.account_seq)
                resp = self.session.get(f"{API_BASE}{path}", headers=headers,
                                        params=params, timeout=10)
                if resp.status_code == 429 or (resp.status_code >= 400 and "rate-limit" in resp.text):
                    wait = float(resp.headers.get("Retry-After", 2 ** attempt))
                    log.warning("레이트리밋 — %.1f초 대기", wait)
                    time.sleep(wait)
                    continue
                if resp.status_code == 401:
                    self._token = None
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.HTTPError as e:
                body = e.response.text[:200] if e.response is not None else ""
                # 어느 종목에서 실패했는지 남긴다. 심볼 없이는 원인 추적이 불가능하다.
                log.error("GET %s %s 실패: %s", path, params or "", body)
                return None
            except requests.RequestException as e:
                log.error("GET %s 통신 오류: %s", path, e)
                time.sleep(2 ** attempt)
        return None

    @staticmethod
    def _unwrap(data):
        """토스 응답은 `result` 로 감싼다. 벗겨서 돌려준다."""
        if not isinstance(data, dict):
            return data
        for w in ("result", "data"):
            if data.get(w) is not None:
                return data[w]
        return data

    @classmethod
    def _items(cls, data, *keys) -> list:
        payload = cls._unwrap(data)
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for k in keys:
                if isinstance(payload.get(k), list):
                    return payload[k]
            lists = [v for v in payload.values() if isinstance(v, list)]
            if len(lists) == 1:
                return lists[0]
        return []

    # -- 계좌 (읽기 전용) -----------------------------------------------------
    def load_account(self) -> bool:
        data = self._get("/api/v1/accounts")
        for acc in self._items(data, "accounts"):
            if acc.get("accountType") == "BROKERAGE":
                self.account_seq = str(acc["accountSeq"])
                log.info("계좌 연결(조회 전용) 완료")
                return True
        log.warning("BROKERAGE 계좌를 찾지 못했다 — 손절 알림 비활성")
        return False

    def get_holdings(self):
        """symbol -> {qty, avg}. 조회 실패 시 None (빈 보유 {} 와 구분)."""
        data = self._get("/api/v1/holdings", with_account=True)
        if data is None:
            return None
        out = {}
        for it in self._items(data, "holdings", "items"):
            try:
                out[it["symbol"]] = {"qty": float(it["quantity"]),
                                     "avg": float(it["averagePurchasePrice"])}
            except (KeyError, TypeError, ValueError):
                continue
        return out

    # -- 시세 ---------------------------------------------------------------
    def get_candles(self, symbol: str, interval: str = "1m", count: int = CANDLE_COUNT) -> list:
        data = self._get("/api/v1/candles",
                         {"symbol": symbol, "interval": interval, "count": count})
        return self._sorted(self._items(data, "candles"))

    def get_candles_paged(self, symbol: str, pages: int) -> list:
        """nextBefore 로 과거를 거슬러 여러 페이지를 받는다 (프로파일 구축용)."""
        out, before = [], None
        for _ in range(pages):
            params = {"symbol": symbol, "interval": "1m", "count": 200}
            if before:
                params["before"] = before
            data = self._get("/api/v1/candles", params)
            batch = self._items(data, "candles")
            if not batch:
                break
            out.extend(batch)
            inner = self._unwrap(data)
            before = inner.get("nextBefore") if isinstance(inner, dict) else None
            if not before:
                break
        return self._sorted(out)

    @staticmethod
    def _sorted(candles: list) -> list:
        try:
            return sorted(candles, key=lambda c: c["timestamp"])
        except (KeyError, TypeError):
            return candles

    def get_prices(self, symbols: list):
        """symbol -> lastPrice. 조회 실패 시 None (빈 결과 {} 와 구분)."""
        data = self._get("/api/v1/prices", {"symbols": ",".join(symbols)})
        if data is None:
            return None
        out = {}
        for it in self._items(data, "prices"):
            try:
                out[it["symbol"]] = float(it["lastPrice"])
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def us_regular_close(self):
        """미국 정규장 마감 시각(aware datetime). 실패 시 None."""
        data = self._get("/api/v1/market-calendar/US")
        for d in self._items(data, "days", "marketDays"):
            sess = d.get("regularMarketSession") or {}
            end = sess.get("endDateTime") or sess.get("end")
            if end:
                try:
                    dt = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
                    # 타임존 없는 문자열이면 UTC 로 간주. naive 를 astimezone 하면
                    # 시스템 로컬(한국) 기준으로 해석돼 9시간 어긋난다.
                    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
        return None


# ---------------------------------------------------------------------------
# 지표
# ---------------------------------------------------------------------------


def _bucket(c: dict, market: str):
    """캔들 -> (세션 날짜 'YYYY-MM-DD', 시각 'HH:MM') 현지 기준. 실패 시 None."""
    dt = parse_ts(c.get("timestamp"), market)
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")


def _regular_minutes(market: str) -> tuple:
    """정규장 시작/종료를 '자정 이후 분'으로. (US 09:30~16:00, KR 09:00~15:30)"""
    return (9 * 60 + 30, 16 * 60) if market == "US" else (9 * 60, 15 * 60 + 30)


def _is_regular(hhmm: str, market: str) -> bool:
    """HH:MM 이 정규장 안인지."""
    try:
        h, m = hhmm.split(":")
        t = int(h) * 60 + int(m)
    except ValueError:
        return False
    s, e = _regular_minutes(market)
    return s <= t < e


def _minutes_from_open(hhmm: str, market: str) -> int:
    """정규장 개장 후 몇 분째인지. 개장 전이면 음수."""
    try:
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m) - _regular_minutes(market)[0]
    except ValueError:
        return -1


def build_volume_profile(candles: list, market: str, exclude_session: str = None) -> dict:
    """현지 시각(HH:MM) -> 과거 그 시각의 거래량 목록.

    장중 거래량은 U자 형태라 시간대별로 비교해야 '평소 대비'가 성립한다.
    오늘 세션(exclude_session)은 뺀다. 안 빼면 자기 자신과 비교하는 꼴이 되어
    급등한 날일수록 기준선이 함께 올라가 RVOL 이 과소평가된다.
    """
    profile = {}
    for c in candles:
        b = _bucket(c, market)
        if b is None or (exclude_session and b[0] == exclude_session):
            continue
        # 정규장 봉만 넣는다. 프리마켓·애프터 봉이 섞이면 버킷이 하루 960개로
        # 흩어져 버킷당 표본이 2개 남짓이 되고, 대부분 이동평균으로 떨어진다.
        # 정규장만이면 390개 버킷에 표본이 집중된다.
        if not _is_regular(b[1], market):
            continue
        try:
            profile.setdefault(b[1], []).append(float(c["volume"]))
        except (KeyError, TypeError, ValueError):
            continue
    return profile


def _median(nums: list) -> float:
    if not nums:
        return 0.0
    s = sorted(nums)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def compute_rvol(candles: list, market: str, profile: dict = None) -> tuple:
    """(직전봉 RVOL, 현재봉 RVOL, 계산방식).

    프로파일이 있으면 같은 현지 시각의 과거 중앙값과 비교, 없으면 직전 20봉 평균.
    두 기준선은 스케일이 달라 방식을 함께 돌려준다. 직전봉과 현재봉의 방식이
    다르면 '혼합'으로 표시하고, 호출부는 그 경우 돌파 판정을 보류한다.
    """
    if len(candles) < RVOL_WINDOW + 2:
        return 0.0, 0.0, "부족"
    vols = [float(c["volume"]) for c in candles]
    methods = []

    def at(i: int) -> float:
        if profile:
            b = _bucket(candles[i], market)
            hist = profile.get(b[1], []) if b else []
            if len(hist) >= MIN_PROFILE_SESSIONS:
                base = _median(hist)
                if base > 0:
                    methods.append("프로파일")
                    return round(vols[i] / base, 2)
        methods.append("이동평균")
        past = vols[i - RVOL_WINDOW:i]
        avg = sum(past) / len(past) if past else 0
        return round(vols[i] / avg, 2) if avg > 0 else 0.0

    prev, cur = at(len(vols) - 2), at(len(vols) - 1)
    method = methods[-1] if len(set(methods)) == 1 else "혼합"
    return prev, cur, method


def rvol_at(candles: list, i: int, market: str, profile: dict = None) -> float:
    """i번째 봉의 RVOL. 프로파일이 있으면 같은 시각 과거 중앙값, 없으면 직전 20봉 평균."""
    if i < RVOL_WINDOW:
        return 0.0
    try:
        cur = float(candles[i]["volume"])
    except (KeyError, TypeError, ValueError):
        return 0.0
    if profile:
        b = _bucket(candles[i], market)
        hist = profile.get(b[1], []) if b else []
        if len(hist) >= MIN_PROFILE_SESSIONS:
            base = _median(hist)
            if base > 0:
                return round(cur / base, 2)
    past = [float(c["volume"]) for c in candles[i - RVOL_WINDOW:i]]
    avg = sum(past) / len(past) if past else 0
    return round(cur / avg, 2) if avg > 0 else 0.0


def session_peak_rvol(candles: list, market: str, profile: dict = None) -> float:
    """오늘 세션 중 최고 RVOL.

    실행 중 관측한 값만 쌓으면 장중에 스크립트를 켰을 때 이전 정점을 모른다.
    캔들 이력에서 직접 계산하면 언제 켜도 그날의 정점이 잡힌다.
    """
    if len(candles) < RVOL_WINDOW + 1:
        return 0.0
    last = _bucket(candles[-1], market)
    if last is None:
        return 0.0
    session = last[0]
    peak = 0.0
    for i in range(RVOL_WINDOW, len(candles)):
        b = _bucket(candles[i], market)
        if b is None or b[0] != session:
            continue
        # 개장 직후 봉은 정점에서 뺀다. 개장봉은 구조적으로 거래량이 몰려
        # 70배 같은 값이 나오는데, 그걸 정점으로 잡으면 3분 뒤 정상화를
        # '연료 소진'으로 오독해 팔라고 하게 된다.
        if 0 <= _minutes_from_open(b[1], market) < OPEN_EXCLUDE_MIN:
            continue
        peak = max(peak, rvol_at(candles, i, market, profile))
    return round(peak, 2)


def compute_vwap(candles: list, market: str) -> float:
    """당일(현지 세션 기준) VWAP. typical price = (고+저+종)/3."""
    if not candles:
        return 0.0
    last = _bucket(candles[-1], market)
    if last is None:
        return 0.0
    session = last[0]
    pv = vol = 0.0
    for c in candles:
        b = _bucket(c, market)
        if b is None or b[0] != session:
            continue
        try:
            tp = (float(c["highPrice"]) + float(c["lowPrice"]) + float(c["closePrice"])) / 3
            v = float(c["volume"])
        except (KeyError, TypeError, ValueError):
            continue
        pv += tp * v
        vol += v
    return round(pv / vol, 4) if vol > 0 else 0.0


def compute_ema(values: list, period: int) -> float:
    if len(values) < period:
        return 0.0
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return round(ema, 4)


def ema_alignment(candles: list) -> str:
    closes = [float(c["closePrice"]) for c in candles]
    if len(closes) < 50:
        return "unknown"
    e9, e20, e50 = compute_ema(closes, 9), compute_ema(closes, 20), compute_ema(closes, 50)
    if e9 > e20 > e50:
        return "정배열"
    if e9 < e20 < e50:
        return "역배열"
    return "혼조"


def compute_rsi(candles: list, period: int = 14) -> tuple:
    """(직전 RSI, 현재 RSI). 기울기를 보기 위해 둘 다."""
    closes = [float(c["closePrice"]) for c in candles]
    if len(closes) < period + 2:
        return 0.0, 0.0

    def at(end: int) -> float:
        g = l = 0.0
        for i in range(end - period + 1, end + 1):
            d = closes[i] - closes[i - 1]
            g += max(d, 0)
            l += max(-d, 0)
        if l == 0:
            return 100.0
        return round(100 - 100 / (1 + (g / period) / (l / period)), 1)

    return at(len(closes) - 2), at(len(closes) - 1)


def compute_atr_pct(candles: list, n: int = 20) -> float:
    """최근 n봉의 평균 진폭(고가-저가)을 가격 대비 %로.

    고정 밴드(0.15%)는 종목마다 의미가 다르다. SOXL 은 1분에 0.5% 가 보통이라
    0.15% 는 노이즈 안이고, 조용한 종목은 0.15% 도 큰 움직임이다.
    변동성에 맞춰 밴드를 늘리면 '사자마자 팔라'는 진동이 줄어든다.
    """
    if len(candles) < n:
        return 0.0
    ranges = []
    for c in candles[-n:]:
        try:
            hi, lo, cl = float(c["highPrice"]), float(c["lowPrice"]), float(c["closePrice"])
            if cl > 0:
                ranges.append((hi - lo) / cl * 100)
        except (KeyError, TypeError, ValueError):
            continue
    return round(sum(ranges) / len(ranges), 3) if ranges else 0.0


def effective_band(candles: list) -> float:
    """실제 적용할 밴드 %. 고정값과 변동성 기반값 중 큰 쪽."""
    atr = compute_atr_pct(candles)
    return max(VWAP_BAND_PCT, round(atr * ATR_BAND_MULT, 3))


def strong_bar(candle: dict) -> bool:
    """종가가 봉 범위의 상단 절반에 있는가.

    거래량이 터졌어도 긴 윗꼬리에 종가가 아래라면 매수세가 밀린 봉이다.
    그런 봉에서 진입하면 바로 눌린다.
    """
    try:
        hi, lo, cl = float(candle["highPrice"]), float(candle["lowPrice"]), float(candle["closePrice"])
    except (KeyError, TypeError, ValueError):
        return False
    rng = hi - lo
    if rng <= 0:
        return True
    return (cl - lo) / rng >= STRONG_BAR_MIN


def vwap_position(price: float, vwap: float, band: float = None) -> str:
    """밴드는 호출부가 종목 변동성에 맞춰 넘긴다. 없으면 고정값."""
    if vwap <= 0 or price <= 0:
        return "neutral"
    b = band if band is not None else VWAP_BAND_PCT
    diff = (price - vwap) / vwap * 100
    if diff > b:
        return "above"
    if diff < -b:
        return "below"
    return "neutral"


# ---------------------------------------------------------------------------
# 알림
# ---------------------------------------------------------------------------


@dataclass
class Notifier:
    last_sent: dict = None

    def __post_init__(self):
        self.last_sent = {}

    def send(self, level: str, ticker: str, msg: str):
        key = f"{level}:{ticker}"
        now = datetime.now(timezone.utc)
        # 강도별로 재발송 간격을 다르게 둔다. 검토 권유가 15분마다 오면
        # 정작 손절 알림이 왔을 때도 흘려보게 된다.
        if any(x in level for x in ("📊", "🔔", "🔕", "📈")):
            gap = 0        # 시황 요약은 정기 발송이라 쿨다운을 두지 않는다
        else:
            gap = WEAK_COOLDOWN_MIN if "🟡" in level else ALERT_COOLDOWN_MIN
        prev = self.last_sent.get(key)
        if prev and now - prev < timedelta(minutes=gap):
            return
        self.last_sent[key] = now
        line = f"{level} | {ticker}\n{msg}"
        log.info(line)
        if not (TG_TOKEN and TG_CHATS):
            return
        # 한 명에게 실패해도 나머지에게는 보내야 한다.
        for chat in TG_CHATS:
            try:
                resp = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                                     json={"chat_id": chat, "text": line}, timeout=5)
                body = resp.json()
                if not body.get("ok"):
                    log.warning("텔레그램 전송 실패(%s): %s", chat, body.get("description"))
            except (requests.RequestException, ValueError) as e:
                log.warning("텔레그램 전송 오류(%s): %s", chat, e)


class SignalTracker:
    """ENTRY 발생 후 정해진 시간이 지난 시점의 가격을 CSV 에 기록한다.

    이 파일이 임계값 조정의 유일한 근거다. 신호가 좋았는지 나빴는지는
    "그때 기분"이 아니라 신호 후 실제 가격 변화로만 판단할 수 있다.
    수동 매매라 실제 진입 여부와 무관하게 모든 신호를 기록한다.
    """

    HEADER = ("signal_time,ticker,entry_price,vwap,rvol_prev,rvol,rvol_method,"
              "leader_pct,ema,rsi,horizon_min,later_price,change_pct\n")

    def __init__(self, path: Path):
        self.path = path
        self.pending = []           # 아직 관측 시점이 안 된 신호들
        if not self.path.exists():
            self.path.write_text(self.HEADER, encoding="utf-8-sig")

    def add(self, ticker: str, price: float, meta: dict):
        """ENTRY 시점 정보를 등록. 각 관측 시점마다 하나씩 대기열에 넣는다."""
        now = datetime.now(timezone.utc)
        for minutes in TRACK_MINUTES:
            self.pending.append({
                "due": now + timedelta(minutes=minutes),
                "signal_time": now, "ticker": ticker, "price": price,
                "horizon": minutes, "meta": meta,
            })

    def flush(self, prices: dict):
        """관측 시점이 지난 항목을 기록한다. 현재가가 없으면 다음 사이클로 미룬다."""
        if not self.pending:
            return
        now = datetime.now(timezone.utc)
        remain = []
        for item in self.pending:
            if now < item["due"]:
                remain.append(item)
                continue
            later = prices.get(item["ticker"])
            if later is None or later <= 0:
                # 가격을 못 받았으면 버리지 말고 다음 사이클에 재시도.
                # 단 관측 시점에서 5분 넘게 지나면 의미가 없어 폐기한다.
                if now - item["due"] < timedelta(minutes=5):
                    remain.append(item)
                continue
            base = item["price"]
            change = round((later - base) / base * 100, 2) if base > 0 else 0.0
            m = item["meta"]
            row = (f'{item["signal_time"].isoformat()},{item["ticker"]},{base},'
                   f'{m.get("vwap", "")},{m.get("rvol_prev", "")},{m.get("rvol", "")},'
                   f'{m.get("rvol_method", "")},{m.get("leader_pct", "")},'
                   f'{m.get("ema", "")},{m.get("rsi", "")},'
                   f'{item["horizon"]},{later},{change}\n')
            try:
                with self.path.open("a", encoding="utf-8-sig") as f:
                    f.write(row)
            except OSError as e:
                log.warning("추적 기록 실패: %s", e)
        self.pending = remain


class TradeLog:
    """청산된 거래를 CSV 에 남기고 일일 성적을 집계한다.

    수익률 단순 합산은 의미가 없다. 10주짜리 +5% 와 100주짜리 -2% 를 더하면
    +3% 가 나오지만 실제로는 손실이다. 투자금(평단×수량) 가중으로 계산한다.
    체결가를 모르므로 모든 값은 추정치다.
    """

    HEADER = "closed_at,ticker,label,qty,avg_price,exit_price,pnl_pct,cost,profit\n"

    def __init__(self, path: Path):
        self.path = path
        if not self.path.exists():
            self.path.write_text(self.HEADER, encoding="utf-8-sig")

    def add(self, ticker, label, qty, avg, price, pnl):
        cost = avg * qty
        profit = (price - avg) * qty
        row = (f"{datetime.now(timezone.utc).isoformat()},{ticker},{label},"
               f"{qty:g},{avg},{price},{pnl},{round(cost, 2)},{round(profit, 2)}\n")
        try:
            with self.path.open("a", encoding="utf-8-sig") as f:
                f.write(row)
        except OSError as e:
            log.warning("거래 기록 실패: %s", e)

    def today_rows(self, market: str) -> list:
        """오늘(거래소 현지 기준) 청산된 거래들."""
        today = now_local(market).strftime("%Y-%m-%d")
        out = []
        try:
            lines = self.path.read_text(encoding="utf-8-sig").splitlines()[1:]
        except OSError:
            return out
        for ln in lines:
            parts = ln.split(",")
            if len(parts) < 9:
                continue
            try:
                closed = datetime.fromisoformat(parts[0])
            except ValueError:
                continue
            if to_local(closed, market).strftime("%Y-%m-%d") != today:
                continue
            try:
                out.append({"label": parts[2], "pnl": float(parts[6]),
                            "cost": float(parts[7]), "profit": float(parts[8])})
            except ValueError:
                continue
        return out

    def daily_summary(self, market: str) -> str:
        """장 마감 시 보낼 성적표. 거래가 없으면 빈 문자열."""
        rows = self.today_rows(market)
        if not rows:
            return ""
        wins = [r for r in rows if r["pnl"] > 0]
        losses = [r for r in rows if r["pnl"] < 0]
        cost = sum(r["cost"] for r in rows)
        profit = sum(r["profit"] for r in rows)
        total_pct = round(profit / cost * 100, 2) if cost > 0 else 0.0
        rate = round(len(wins) / len(rows) * 100) if rows else 0

        best = max(rows, key=lambda r: r["pnl"])
        worst = min(rows, key=lambda r: r["pnl"])

        parts = [
            f"{len(wins)}익절 {len(losses)}손절 (승률 {rate}%)",
            f"투자금 대비 {total_pct:+.2f}%  (손익 {profit:+,.0f})",
            f"최고 {best['label']} {best['pnl']:+.2f}% / 최저 {worst['label']} {worst['pnl']:+.2f}%",
        ]
        if len(wins) and len(losses):
            aw = sum(r["pnl"] for r in wins) / len(wins)
            al = abs(sum(r["pnl"] for r in losses) / len(losses))
            if al > 0:
                parts.append(f"손익비 1:{round(aw / al, 2)}  (평균 익절 {aw:+.2f}% / 평균 손절 -{al:.2f}%)")
        parts.append("")
        parts.append("※ 체결가 미확인 — 모두 추정치")
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# 신호 엔진
# ---------------------------------------------------------------------------


class SignalEngine:
    def __init__(self, client: TossReadOnlyClient, notifier: Notifier, watch_holdings: bool):
        self.client = client
        self.notify = notifier
        self.watch_holdings = watch_holdings
        self.last_bar = {}              # ticker -> 마지막으로 평가한 완성봉 timestamp
        self.prev_close = {}
        self.prev_close_date = None
        self.volume_profile = {}        # ticker -> {HH:MM: [거래량...]}
        self.profile_date = {}          # ticker -> 구축한 세션 날짜. 시장별로 세션이 달라 종목별로 관리
        self.us_close = None
        self.us_close_date = None
        self.stats = {}
        self.last_report = datetime.now(timezone.utc)
        self.tracker = (SignalTracker(Path(__file__).with_name(TRACK_FILE))
                        if ENABLE_TRACKING else None)
        self.trades = TradeLog(Path(__file__).with_name(TRADE_FILE))
        # 불타기 알림을 포지션당 몇 번 보냈는지. 청산되면 초기화한다.
        self.addon_count = {}
        # 매도 계열 알림을 보낸 시각. 이후 일정 시간 매수 알림을 막는다.
        self.exit_at = {}
        # 매수 알림을 보낸 시각. 이후 일정 시간 익절 알림을 막는다.
        self.entry_at = {}
        # 종목별 상태: 관망 / 진입대기 / 보유 / 청산대기
        self.state = {}
        self.pending = {}           # 이행 대기 중인 신호 정보
        self._last_holdings = {}    # 직전 사이클 보유. hold_only 종목의 감시 여부 결정
        # 청산 직전 마지막으로 관측한 평단·시세. 마감 메시지의 손익 추정에 쓴다.
        self.last_seen = {}
        # 매수 신호봉의 저점. 이게 구조적 손절선이다.
        # 기준선 밴드 대신 이 선을 쓰면 '사자마자 살짝 눌림'에 안 흔들린다.
        self.stop_ref = {}
        self.snapshots = {}         # 최근 지표. 시황 요약이 재계산 없이 쓴다
        # 기동 직후 첫 시황이 바로 나가도록 과거 시각으로 초기화한다.
        # 30분을 기다리면 '돌고 있는 건지' 확인이 늦어진다.
        self.last_summary = datetime.now(timezone.utc) - timedelta(minutes=SUMMARY_INTERVAL_MIN)

    # -- 집계 ---------------------------------------------------------------
    def bump(self, ticker: str, key: str):
        self.stats.setdefault(ticker, {}).setdefault(key, 0)
        self.stats[ticker][key] += 1

    def report_stats(self):
        now = datetime.now(timezone.utc)
        if now - self.last_report < timedelta(minutes=STATS_REPORT_MIN) or not self.stats:
            return
        self.last_report = now
        for t, s in self.stats.items():
            log.info("[집계] %s 완성봉 %d | 방향OK %d | RVOL돌파 %d | VWAP위 %d | 3조건동시 %d",
                     WATCHLIST[t].get("name") or t, s.get("tick", 0), s.get("direction", 0),
                     s.get("rvol", 0), s.get("vwap", 0), s.get("all", 0))

    # -- 일 1회 갱신 ---------------------------------------------------------
    def refresh_volume_profile(self, tickers: list):
        """종목별로 세션당 한 번 프로파일을 만든다.

        캐시를 시장 공용으로 두면 한국장에서 만든 날 미국장이 열릴 때
        '오늘 이미 만들었다'로 판단해 미국 종목이 프로파일 없이 돌아간다.
        """
        for t in tickers:
            market = WATCHLIST[t]["market"]
            session = now_local(market).strftime("%Y-%m-%d")
            if self.profile_date.get(t) == session and t in self.volume_profile:
                continue
            try:
                candles = self.client.get_candles_paged(t, PROFILE_PAGES)
                if candles:
                    self.volume_profile[t] = build_volume_profile(candles, market, session)
                    log.info("프로파일 %s: %d봉 / %d개 시간대", t, len(candles),
                             len(self.volume_profile[t]))
            except Exception as e:
                log.warning("프로파일 %s 실패(이동평균 대체): %s", t, e)
            self.profile_date[t] = session

    def refresh_prev_closes(self, symbols: list):
        today = datetime.now(timezone.utc).date()
        if self.prev_close_date == today and all(s in self.prev_close for s in symbols):
            return
        for s in symbols:
            daily = self.client.get_candles(s, interval="1d", count=2)
            if len(daily) >= 2:
                try:
                    self.prev_close[s] = float(daily[-2]["closePrice"])
                except (KeyError, TypeError, ValueError):
                    pass
        self.prev_close_date = today

    def leader_strength(self, leaders: list, prices: dict) -> float:
        ch = []
        for s in leaders:
            cur, prev = prices.get(s), self.prev_close.get(s)
            if cur and prev and prev > 0:
                ch.append((cur - prev) / prev * 100)
        return round(sum(ch) / len(ch), 2) if ch else 0.0

    # -- 시장 시간 -------------------------------------------------------------
    def market_open(self, market: str) -> bool:
        """현지시각 기준. 앞뒤 여유를 둬 개장 직후 봉도 잡는다."""
        t = now_local(market)
        if t.weekday() >= 5:
            return False
        hm = t.hour * 60 + t.minute
        if market == "KR":
            return 8 * 60 + 50 <= hm <= 15 * 60 + 40
        return 9 * 60 + 20 <= hm <= 16 * 60 + 10

    def market_premarket(self, market: str) -> bool:
        """프리마켓 시간대인지.

        프리마켓은 유동성이 정규장의 수십 분의 일이라 거래 몇 건으로 RVOL 이
        크게 튄다. 그래서 알림 판단에는 쓰지 않고 시황 표시에만 쓴다.
        한국장 장전 동시호가는 체결 구조가 달라 아예 제외한다.
        """
        if market != "US":
            return False
        t = now_local(market)
        if t.weekday() >= 5:
            return False
        hm = t.hour * 60 + t.minute
        return 8 * 60 <= hm < 9 * 60 + 20      # 08:00~09:20 ET

    def near_close(self, market: str) -> bool:
        """마감 30분 전 여부. 한국은 15:30 고정(서머타임 없음), 미국은 캘린더 API."""
        t = now_local(market)
        if market == "KR":
            close = t.replace(hour=15, minute=30, second=0, microsecond=0)
        else:
            today = datetime.now(timezone.utc).date()
            if self.us_close_date != today:
                self.us_close = self.client.us_regular_close()
                self.us_close_date = today
            close = (to_local(self.us_close, "US") if self.us_close
                     else t.replace(hour=16, minute=0, second=0, microsecond=0))
        left = close - t
        return timedelta(0) < left <= timedelta(minutes=CLOSE_WARN_MIN)

    # -- 종목별 판단 -----------------------------------------------------------
    def _snapshot(self, ticker, prices):
        """지표를 한 번에 계산해 돌려준다. 판단과 시황 요약이 같은 값을 쓰도록."""
        cfg = WATCHLIST[ticker]
        market = cfg["market"]
        candles = self.client.get_candles(ticker)
        if not candles:
            return None

        # 진행 중인 마지막 봉은 제외한다. 거래량이 부분값이라 RVOL 이 낮게 나오고,
        # 같은 봉이 폴링마다 다른 값으로 재평가된다.
        last_dt = parse_ts(candles[-1].get("timestamp"), market)
        cur_min = now_local(market).replace(second=0, microsecond=0)
        if last_dt is not None and last_dt >= cur_min:
            candles = candles[:-1]
        if len(candles) < RVOL_WINDOW + 2:
            return None

        price = prices.get(ticker)
        if price is None or price <= 0:
            try:
                price = float(candles[-1]["closePrice"])
            except (KeyError, TypeError, ValueError):
                return None

        prof = self.volume_profile.get(ticker)
        prev_rvol, rvol, rvol_method = compute_rvol(candles, market, prof)
        vwap = compute_vwap(candles, market)
        band = effective_band(candles)

        leaders = cfg.get("leaders") or []
        if leaders:
            strength = self.leader_strength(leaders, prices)
            direction_ok = (strength <= -LEADER_GAP_PCT if cfg["inverse"]
                            else strength >= LEADER_GAP_PCT)
        else:
            strength, direction_ok = None, True

        return {
            "cfg": cfg, "market": market, "label": cfg.get("name") or ticker,
            "candles": candles, "bar_key": candles[-1].get("timestamp"),
            "price": price, "vwap": vwap, "band": band,
            "pos": vwap_position(price, vwap, band),
            "last_low": float(candles[-1].get("lowPrice") or 0),
            "strong": strong_bar(candles[-1]),
            "prev_rvol": prev_rvol, "rvol": rvol, "rvol_method": rvol_method,
            "peak": session_peak_rvol(candles, market, prof),
            "strength": strength, "direction_ok": direction_ok,
        }

    def _sync_state(self, ticker: str, has_pos: bool) -> str:
        """실제 보유 여부에 맞춰 상태를 맞춘다.

        상태 기계를 두는 이유: 매 사이클 처음부터 판단하면 방금 낸 신호를
        기억하지 못해 '사라 → 팔아라 → 사라'가 반복된다. 상태가 있으면
        보유 중엔 매수 알림이, 청산 대기 중엔 매수 알림이 아예 나오지 않는다.

        상태 전이는 시간이 아니라 실제 보유 변화가 결정한다.
        진규가 알림대로 움직였는지를 API 가 알려주므로, 안 움직였으면
        같은 방향의 알림이 계속 반복된다.
        """
        st = self.state.get(ticker, "관망")
        if has_pos and st in ("관망", "진입대기"):
            st = "보유"                     # 매수 실행됨
            self.entry_at[ticker] = datetime.now(timezone.utc)
        elif not has_pos and st in ("보유", "청산대기"):
            st = "관망"                     # 청산 실행됨
            self.exit_at[ticker] = datetime.now(timezone.utc)
            self.addon_count.pop(ticker, None)
            self.pending.pop(ticker, None)
            self.stop_ref.pop(ticker, None)
            self._closing_note(ticker)
        self.state[ticker] = st
        return st

    def evaluate(self, ticker: str, prices: dict, holdings: dict):
        snap = self._snapshot(ticker, prices)
        if snap is None:
            return
        self.snapshots[ticker] = snap

        held = holdings.get(ticker)
        has_pos = bool(held and held["qty"] > 0)
        st = self._sync_state(ticker, has_pos)

        label, market = snap["label"], snap["market"]
        price, vwap, pos = snap["price"], snap["vwap"], snap["pos"]
        rvol, prev_rvol, peak = snap["rvol"], snap["prev_rvol"], snap["peak"]

        # ---- 보유 중 / 청산 대기 ----
        if st in ("보유", "청산대기"):
            # 청산 뒤에는 보유 정보가 사라지므로, 매 사이클 마지막 값을 남겨둔다
            self.last_seen[ticker] = {"avg": held["avg"], "price": price, "qty": held["qty"]}
            ctx = {"ema": ema_alignment(snap["candles"]), "rsi": compute_rsi(snap["candles"])}
            rvol_breakout = snap["rvol_method"] != "혼합" and prev_rvol < RVOL_TRIGGER <= rvol
            self._check_holding(ticker, label, market, price, vwap, pos,
                                rvol, prev_rvol, rvol_breakout, held, ctx, peak)
            return

        # ---- 진입 대기: 아직 안 샀다. 조건이 살아있으면 다시 알린다 ----
        if st == "진입대기":
            if pos == "below":
                # 근거가 무너졌으면 기다릴 이유가 없다
                self.state[ticker] = "관망"
                self.pending.pop(ticker, None)
                self.notify.send("⚪ 매수 취소", label,
                                 f"현재가 {price}가 기준선 {vwap} 아래로 내려감\n"
                                 f"진입 근거 소멸 — 관망으로 전환")
            else:
                sig = self.pending.get(ticker, {})
                self.notify.send("🔵 매수하세요", label,
                                 f"현재가 {price}  (신호가 {sig.get('price', price)})\n"
                                 f"거래량 {rvol}배, 기준선 {vwap} 위 유지\n"
                                 f"아직 미진입 — 조건 유지 중")
            return

        # ---- 관망: 매수 판단 ----
        if snap["cfg"].get("hold_only"):
            return          # 3배 상품은 매수 신호를 내지 않는다
        exited = self.exit_at.get(ticker)
        if exited and datetime.now(timezone.utc) - exited < timedelta(minutes=REENTRY_BLOCK_MIN):
            return

        # 매수는 완성봉 기준이라 같은 봉을 두 번 판단하지 않는다
        if self.last_bar.get(ticker) == snap["bar_key"]:
            return
        self.last_bar[ticker] = snap["bar_key"]

        rvol_breakout = snap["rvol_method"] != "혼합" and prev_rvol < RVOL_TRIGGER <= rvol
        # 오늘 이미 큰 거래량이 있었다면, 지금이 그 정점에 근접해야 '새 추세'다.
        if rvol_breakout and peak >= FADE_MIN_PEAK and rvol < peak * ENTRY_MIN_PEAK_RATIO:
            rvol_breakout = False

        pair = snap["cfg"].get("pair")
        if pair and holdings.get(pair, {}).get("qty", 0) > 0:
            return

        direction_ok = snap["direction_ok"]
        strength = snap["strength"]
        direction_txt = "선행지표 없음" if strength is None else f"선행 {strength}%"

        self.bump(ticker, "tick")
        if direction_ok:
            self.bump(ticker, "direction")
        if rvol_breakout:
            self.bump(ticker, "rvol")
        if pos == "above":
            self.bump(ticker, "vwap")

        # 거래량이 터져도 긴 윗꼬리에 종가가 아래면 매수세가 밀린 봉이다
        if rvol_breakout and not snap["strong"]:
            log.debug("%s 돌파했으나 신호봉이 약함(윗꼬리) — 보류", ticker)
            rvol_breakout = False

        if direction_ok and rvol_breakout and pos == "above":
            self.bump(ticker, "all")
            self.stop_ref[ticker] = snap["last_low"]
            align = ema_alignment(snap["candles"])
            rsi_prev, rsi_now = compute_rsi(snap["candles"])
            slope = "상승" if rsi_now > rsi_prev else "하락"
            self.state[ticker] = "진입대기"
            self.pending[ticker] = {"price": price, "at": datetime.now(timezone.utc)}
            note = snap["cfg"].get("note")
            self.notify.send("🔵 매수하세요", label,
                             f"현재가 {price}\n"
                             f"거래량 {prev_rvol}→{rvol}배 돌파, 기준선 {vwap} 위\n"
                             f"{direction_txt} | EMA {align} | RSI {rsi_now}({slope})"
                             + (f"\n→ {note}" if note else ""))
            if self.tracker:
                self.tracker.add(ticker, price, {
                    "vwap": vwap, "rvol_prev": prev_rvol, "rvol": rvol,
                    "rvol_method": snap["rvol_method"],
                    "leader_pct": strength if strength is not None else "",
                    "ema": align, "rsi": rsi_now,
                })


    @staticmethod
    def _ambiguous_flags(pos_now: str, ctx: dict) -> list:
        """흐려진 근거들을 나열한다.

        진입 근거는 셋이었다: 기준선 위, 추세 정배열, 모멘텀 상승.
        그중 무너진 것을 찾는다. 기준선 중립대는 above 도 below 도 아니라
        다른 알림이 하나도 안 걸리는 사각지대다.
        """
        flags = []
        if pos_now == "neutral":
            flags.append("기준선 중립대(위/아래 판정 불가)")
        align = ctx.get("ema")
        if align in ("역배열", "혼조"):
            flags.append(f"EMA {align}")
        rsi_prev, rsi_now = ctx.get("rsi", (0.0, 0.0))
        if rsi_now and rsi_now < rsi_prev:
            flags.append(f"RSI 하락 {rsi_prev}→{rsi_now}")
        return flags

    def _ambiguous(self, pos_now: str, ctx: dict) -> bool:
        """근거가 두 개 이상 흐려졌을 때만 애매로 본다.

        하나만으로 판정하면 RSI 가 한 틱 내려간 것만으로도 알림이 나가
        조기 청산을 부추기게 된다.
        """
        return len(self._ambiguous_flags(pos_now, ctx)) >= 2

    def _ambiguous_reason(self, pos_now: str, ctx: dict) -> str:
        return " + ".join(self._ambiguous_flags(pos_now, ctx))

    def _closing_note(self, ticker: str):
        """포지션이 사라졌을 때 결과를 정리해 보낸다.

        실제 체결가는 알 수 없다. 스크립트가 아는 건 보유가 사라졌다는 사실과
        직전에 관측한 시세뿐이라, 손익은 추정치로 표시한다.
        """
        seen = self.last_seen.pop(ticker, None)
        if not seen:
            return
        avg, price, qty = seen["avg"], seen["price"], seen["qty"]
        if avg <= 0:
            return
        pnl = round((price - avg) / avg * 100, 2)
        label = WATCHLIST[ticker].get("name") or ticker

        if pnl > 0:
            head = "🎉 익절 완료"
            body = (f"약 {pnl:+.2f}% 수익  (평단 {avg} → 청산 무렵 {price})\n"
                    f"{qty:g}주 정리 완료\n"
                    f"계획대로 나온 거래야. 다음 신호까지 기다리면 돼")
        elif pnl < 0:
            head = "✅ 손절 완료"
            body = (f"약 {pnl:+.2f}% 손실  (평단 {avg} → 청산 무렵 {price})\n"
                    f"{qty:g}주 정리 완료\n"
                    f"규칙대로 끊은 게 잘한 거야. 다음 기회는 또 와")
        else:
            head = "✅ 청산 완료"
            body = (f"손익 없음  (평단 {avg})\n"
                    f"{qty:g}주 정리 완료\n"
                    f"다음 신호를 기다리면 돼")
        body += f"\n\n※ 실제 체결가는 다를 수 있음 (추정치)"
        self.notify.send(head, label, body)
        self.trades.add(ticker, label, qty, avg, price, pnl)

    def _check_holding(self, ticker, label, market, price, vwap, pos_now,
                       rvol, prev_rvol, rvol_breakout, held, ctx, rvol_peak):
        """보유 중 알림.

        우선순위: 손절 > 매도 > 익절 > 추가매수 > 일부익절 > 판단애매 > 마감임박

        판단 기준은 전부 시장 데이터다. 평단은 손익 표시에만 쓰고,
        예외적으로 손절 한도(-5%)만 최후의 안전망으로 남긴다.
        VWAP 이 급하게 따라 내려오면 밴드 이탈 신호가 늦을 수 있고,
        3배 상품에서는 그 사이 손실이 두 자릿수로 벌어질 수 있어서다.
        """
        avg = held["avg"]
        pnl = round((price - avg) / avg * 100, 2) if avg > 0 else 0.0

        # 이미 청산 신호를 낸 상태면 사유를 바꾸지 않는다.
        # 매 사이클 조건을 다시 평가하면 '매도하세요 → 익절하세요 → 1/3 익절'처럼
        # 같은 행동(팔아라)에 다른 이름표가 붙어 헷갈린다. 최초 사유를 고정하고
        # 손익만 갱신해 반복한다. 단, 손절 한도(-5%)는 더 급한 상황이라 갈아탄다.
        if self.state.get(ticker) == "청산대기":
            first = self.pending.get(ticker, {})
            if pnl <= STOP_LOSS_PCT and first.get("level") != "🔴 손절하세요":
                self.pending[ticker] = {"level": "🔴 손절하세요", "why": f"손절 한도 {STOP_LOSS_PCT}% 도달"}
                first = self.pending[ticker]
            level = first.get("level", "🔴 매도하세요")
            self.notify.send(level, label,
                             f"손익 {pnl}%  (평단 {avg} → 현재 {price})\n"
                             f"아직 미청산 — {first.get('why', '청산 신호 유지')}\n"
                             f"{held['qty']:g}주 보유 중")
            return
        # 매도 기준선. 우리 신호로 산 포지션이면 신호봉 저점, 아니면 기준선 밴드.
        # 신호봉 저점은 '이 봉이 무너지면 진입 근거가 깨진 것'이라는 뜻이라
        # 밴드보다 종목 상황에 밀착돼 있다. 사자마자 살짝 눌리는 정도로는 안 걸린다.
        stop_ref = self.stop_ref.get(ticker, 0)
        band = self.snapshots.get(ticker, {}).get("band", VWAP_BAND_PCT)
        band_low = round(vwap * (1 - band / 100), 4) if vwap > 0 else 0
        if stop_ref > 0:
            sell_line, sell_why = stop_ref, "매수 신호봉 저점"
        else:
            sell_line, sell_why = band_low, f"기준선 밴드 하단 (VWAP -{band}%)"
        broke = sell_line > 0 and price < sell_line

        # 거래량 소진도: 세션 정점 대비 현재 비율.
        # 정점이 충분히 높아야(FADE_MIN_PEAK) '한 번 터진 추세'로 인정한다.
        # 애초에 거래가 안 붙었던 종목에는 소진 개념이 성립하지 않는다.
        faded = rvol / rvol_peak if rvol_peak >= FADE_MIN_PEAK and rvol_peak > 0 else None

        # 매수 직후에는 익절 알림을 막는다.
        # 거래량이 한 봉만 튀고 식으면 정점 대비 비율이 곧바로 무너져
        # 산 지 1~2분 만에 '정리하세요'가 나온다. 손절·매도는 막지 않는다.
        entered = self.entry_at.get(ticker)
        in_grace = bool(entered and
                        datetime.now(timezone.utc) - entered < timedelta(minutes=EXIT_GRACE_MIN))
        if in_grace:
            faded = None

        # 가격 확인 없이 거래량만으로 청산하면 안 된다.
        #
        # 거래량 감소에는 두 가지가 있다.
        #   (1) 눌림목 소화 — 가격이 기준선 위를 지키는 중. 매도세가 마른 것이라
        #       오히려 상승이 이어질 자리다.
        #   (2) 진짜 소진 — 가격도 기준선을 잃음. 매수세가 마른 것.
        # 둘을 구분하지 않으면 (1)에서 팔고 곧바로 급등을 놓친다.
        # 그래서 가격이 기준선 위를 지키는 동안에는 거래량 기반 청산을 보류한다.
        if faded is not None and pos_now == "above":
            holding_up = True
        else:
            holding_up = False

        # 매도 계열이 나가면 재진입 차단 타이머를 건다.
        # 판 직후 매수 알림이 오는 건 시스템이 자기 신호를 뒤집는 꼴이다.
        if pnl <= STOP_LOSS_PCT:
            self.exit_at[ticker] = datetime.now(timezone.utc)
            self.entry_at.pop(ticker, None)
            self.state[ticker] = "청산대기"
            self.pending[ticker] = {"level": "🔴 손절하세요", "why": f"손절 한도 {STOP_LOSS_PCT}% 도달"}
            self.notify.send("🔴 손절하세요", label,
                             f"손익 {pnl}%  (평단 {avg} → 현재 {price})\n"
                             f"손절 한도 {STOP_LOSS_PCT}% 도달 — 최후 안전망")
        elif broke:
            # 위치 기반(크로스 아님)이라 선 아래 머무는 동안 반복되지만,
            # 손절 성격의 알림은 반복돼야 한다. 쿨다운이 빈도를 제한한다.
            flavor = (f"매도 물량 쏟아지는 중 (거래량 {rvol}배)" if rvol >= RVOL_TRIGGER
                      else f"조용히 빠지는 중 (거래량 {rvol}배)")
            self.exit_at[ticker] = datetime.now(timezone.utc)
            self.entry_at.pop(ticker, None)
            self.state[ticker] = "청산대기"
            self.pending[ticker] = {"level": "🔴 매도하세요", "why": f"{sell_why} {sell_line} 이탈"}
            self.notify.send("🔴 매도하세요", label,
                             f"손익 {pnl}%  (평단 {avg} → 현재 {price})\n"
                             f"{flavor}\n"
                             f"{sell_why} {sell_line} 아래로 내려감")
        elif (ENABLE_EXIT_SIGNAL and faded is not None and not holding_up
              and faded <= FADE_STRONG_RATIO):
            # 발동 조건은 순수 시장 기준(거래량 소진)이다. 손익은 표시용이고
            # 판단에 쓰지 않는다. 다만 손실 중인데 '익절'이라 부르면 어색하므로
            # 문구만 상황에 맞춘다.
            verb = "익절하세요" if pnl > 0 else "정리하세요"
            self.exit_at[ticker] = datetime.now(timezone.utc)
            self.entry_at.pop(ticker, None)
            qty = held["qty"]
            self.state[ticker] = "청산대기"
            self.pending[ticker] = {"level": f"🟢 {EXIT_PORTION_STRONG} {verb}",
                                    "why": f"거래량 정점 {rvol_peak}배 대비 {round(faded * 100)}% 로 소진"}
            self.notify.send(f"🟢 {EXIT_PORTION_STRONG} {verb}", label,
                             f"손익 {pnl}%  (평단 {avg} → 현재 {price})\n"
                             f"보유 {qty:g}주 → {EXIT_PORTION_STRONG} 정리 권장\n"
                             f"거래량이 오늘 정점 {rvol_peak}배 → 현재 {rvol}배 "
                             f"({round(faded * 100)}% 수준)\n"
                             f"상승 연료 소진 — 더 오를 힘이 남지 않음")
        elif (ENABLE_ADD_ON and pos_now == "above" and rvol_breakout
              and pnl >= ADDON_MIN_PROFIT_PCT
              and self.addon_count.get(ticker, 0) < ADDON_MAX_COUNT):
            # 불타기: 진입 근거(VWAP 위)가 유지되고 새 거래량이 붙었으며 이미 수익 중.
            # 손실 중에는 절대 발동하지 않는다 — 물타기는 이 시스템이 다루지 않는다.
            self.addon_count[ticker] = self.addon_count.get(ticker, 0) + 1
            self.notify.send("🔵 추가매수 검토", label,
                             f"손익 {pnl}%  (평단 {avg} → 현재 {price})\n"
                             f"거래량 {prev_rvol}→{rvol}배 재돌파, 추세 살아있음\n"
                             f"⚠ 물량 늘리면 손절 시 손실도 같은 배로 커짐")
        elif (ENABLE_EXIT_SIGNAL and faded is not None and not holding_up
              and faded <= FADE_WEAK_RATIO):
            qty = held["qty"]
            self.notify.send(f"🟡 {EXIT_PORTION_HALF} 익절 검토", label,
                             f"손익 {pnl}%  (평단 {avg} → 현재 {price})\n"
                             f"보유 {qty:g}주 → {qty / 2:g}주 정리, {qty / 2:g}주 유지\n"
                             f"거래량이 오늘 정점 {rvol_peak}배 → 현재 {rvol}배 "
                             f"({round(faded * 100)}% 수준)\n"
                             f"둔화 시작. 절반 덜어내고 나머지로 추세 확인")
        elif ENABLE_AMBIGUOUS and not in_grace and self._ambiguous(pos_now, ctx):
            # 판단 애매: 근거가 흐려졌지만 아직 이탈은 아닌 구간.
            # 다른 알림이 하나도 안 걸려 방치되기 쉬운 사각지대다.
            qty = held["qty"]
            part = round(qty / 3, 1)
            self.notify.send(f"🟡 {EXIT_PORTION_THIRD} 익절 검토", label,
                             f"손익 {pnl}%  (평단 {avg} → 현재 {price})\n"
                             f"보유 {qty:g}주 → {part:g}주 정리, {qty - part:g}주 유지\n"
                             f"흐려진 근거: {self._ambiguous_reason(pos_now, ctx)}\n"
                             f"급하지 않음. 조금 덜어내고 지켜봐도 되는 구간")
        elif self.near_close(market):
            self.notify.send("🟠 마감 전 정리", label,
                             f"손익 {pnl}%  ({held['qty']:g}주 보유)\n"
                             f"마감 {CLOSE_WARN_MIN}분 전 — 3배 상품은 오버나잇 시 가치 감소")

    def market_summary(self, active: list, holdings: dict, pre: list = None):
        """30분마다 전 종목 상태를 한 번에 보낸다.

        보유하지 않은 종목도 지금 조건이 어디까지 찼는지 보여준다.
        개별 알림은 조건이 다 맞아야 나오지만, 요약은 '아직 뭐가 모자란지'를
        알려줘서 사람이 직접 판단할 여지를 남긴다.
        프리마켓 종목은 참고용으로만 붙인다 — 지표를 믿을 수 없기 때문이다.
        """
        now = datetime.now(timezone.utc)
        if now - self.last_summary < timedelta(minutes=SUMMARY_INTERVAL_MIN):
            return
        self.last_summary = now

        lines = []
        for t in active:
            snap = self.snapshots.get(t)
            if not snap:
                continue
            label = snap["label"]
            st = self.state.get(t, "관망")
            held = holdings.get(t)

            if held and held["qty"] > 0:
                avg = held["avg"]
                pnl = round((snap["price"] - avg) / avg * 100, 2) if avg > 0 else 0
                mark = "🟢" if pnl > 0 else "🔴"
                if st == "청산대기":
                    tail = "청산 대기"
                elif snap["pos"] == "above":
                    # 거래량이 줄어도 기준선 위면 눌림목일 수 있다. 청산 알림은
                    # 안 나가지만 상태는 알려준다.
                    peak = snap["peak"]
                    faded = snap["rvol"] / peak if peak >= FADE_MIN_PEAK and peak > 0 else None
                    tail = ("거래량 줄었으나 기준선 위 — 눌림목 가능"
                            if faded is not None and faded <= FADE_WEAK_RATIO
                            else "기준선 위 유지")
                else:
                    tail = f"기준선 {'아래' if snap['pos'] == 'below' else '중립대'}"
                lines.append(f"{mark} {label}  보유 {held['qty']:g}주 {pnl:+.2f}% | {tail}")
                continue

            # 미보유: 매수 조건 3개 중 몇 개가 찼는지
            checks = []
            checks.append(("방향", snap["direction_ok"]))
            checks.append(("거래량", snap["rvol"] >= RVOL_TRIGGER))
            checks.append(("기준선", snap["pos"] == "above"))
            done = sum(1 for _, ok in checks if ok)
            miss = ", ".join(n for n, ok in checks if not ok)
            mark, stance = self._stance(snap)

            if st == "진입대기":
                # 실제 매수 알림이 나간 유일한 경우. 여기만 🔵을 쓴다.
                lines.append(f"🔵 {label}  매수 알림 발생 — 아직 미진입")
            elif done == 3:
                # 수준으로는 다 찼지만 '돌파 순간'이 아니라 알림은 안 나간 상태.
                # 이미 높은 거래량이 유지되는 중이면 늦은 진입이다.
                lines.append(f"{mark} {label}  {stance} | 조건 근접 (돌파 알림 대기)")
            else:
                lines.append(f"{mark} {label}  {stance} | {done}/3 (부족: {miss})")

        # 프리마켓은 별도 구획에 참고용으로만. 조건 충족 여부를 따지지 않는다.
        for t in (pre or []):
            snap = self.snapshots.get(t)
            if not snap:
                continue
            prev = self.prev_close.get(t)
            if prev and prev > 0:
                chg = round((snap["price"] - prev) / prev * 100, 2)
                lines.append(f"🌙 {snap['label']}  프리마켓 {chg:+.2f}% "
                             f"({snap['price']}) | 거래량 {snap['rvol']}배 · 참고용")
            else:
                lines.append(f"🌙 {snap['label']}  프리마켓 {snap['price']} | 참고용")

        if not lines:
            return
        # 시황을 보고 진입하는 사고를 막는다. 행동 신호는 개별 알림뿐이다.
        lines.append("")
        lines.append("※ 참고용. 매수·매도는 개별 알림(🔵🔴🟢)이 왔을 때만")
        ref = (active or pre)[0]
        clock = now_local(WATCHLIST[ref]["market"]).strftime("%H:%M")
        self.notify.send("📊 시황", clock, "\n".join(lines))

    @staticmethod
    def _stance(snap) -> tuple:
        """조건이 다 안 차도 지금 어느 쪽이 우세한지 한 줄로 알려준다.

        개별 알림은 3개 조건이 다 맞아야 나가지만, 그 사이에도 종목은 계속
        움직인다. 보유하지 않은 종목의 방향을 알아야 사람이 직접 판단할 수 있다.
        기준선(VWAP) 위/아래를 1차 기준으로, 선행 지표를 보조로 쓴다.
        """
        pos, strength = snap["pos"], snap["strength"]
        lean = ""
        if strength is not None:
            if strength >= LEADER_GAP_PCT:
                lean = f", 선행 +{strength}%"
            elif strength <= -LEADER_GAP_PCT:
                lean = f", 선행 {strength}%"
        # '우위'라는 표현은 행동을 유도한다. 기준선 위/아래는 3개 조건 중 하나일 뿐이라
        # 매수 근거로 턱없이 약하다. 방향만 서술하고 판단은 개별 알림에 맡긴다.
        if pos == "above":
            return "▲", f"기준선 위{lean}"
        if pos == "below":
            return "▼", f"기준선 아래{lean}"
        return "－", f"기준선 중립대{lean}"

    # -- 메인 루프 -------------------------------------------------------------
    def run(self):
        log.info("알림 전용 모드 시작 (주문 없음). 대상=%s", TICKERS)
        # 직전 사이클에 열려 있던 시장. 열고 닫힐 때 알림을 보내
        # '조용한 이유'를 사람이 알 수 있게 한다.
        prev_open = set()
        while True:
            open_now = [t for t in TICKERS if self.market_open(WATCHLIST[t]["market"])]
            # hold_only 종목(3배 레버리지)은 보유 중일 때만 감시한다.
            # 안 들고 있으면 조회할 이유가 없다 — 매수 신호는 SOXX 가 낸다.
            held_syms = set(self._last_holdings.keys())
            active = [t for t in open_now
                      if not WATCHLIST[t].get("hold_only") or t in held_syms]
            # 프리마켓 종목은 시황에만 쓴다. 알림 판단에는 넣지 않는다.
            pre = [t for t in TICKERS
                   if t not in active and self.market_premarket(WATCHLIST[t]["market"])]
            now_open = {WATCHLIST[t]["market"] for t in active}

            for m in now_open - prev_open:
                names = [WATCHLIST[t].get("name") or t
                         for t in TICKERS if WATCHLIST[t]["market"] == m]
                self.notify.send("🔔 장 시작", "한국" if m == "KR" else "미국",
                                 f"{', '.join(names)} 감시 시작")
            for m in prev_open - now_open:
                self.notify.send("🔕 장 마감", "한국" if m == "KR" else "미국",
                                 "감시 종료 — 다음 개장까지 알림이 없어")
                # 오늘 청산된 거래가 있으면 성적표를 보낸다
                report = self.trades.daily_summary(m)
                if report:
                    self.notify.send("📈 오늘 성적", "한국" if m == "KR" else "미국", report)
            prev_open = now_open

            if not active and not pre:
                time.sleep(POLL_INTERVAL_SEC)
                continue

            watch = active + pre
            leaders = sorted({s for t in watch for s in (WATCHLIST[t].get("leaders") or [])})
            try:
                self.refresh_volume_profile(watch)
                # 프리마켓 등락률 계산에 종목 자신의 전일 종가도 필요하다
                self.refresh_prev_closes(leaders + pre)
                prices = self.client.get_prices(watch + leaders)
                holdings = self.client.get_holdings() if self.watch_holdings else {}
            except Exception as e:
                log.exception("시세/보유 조회 실패: %s", e)
                time.sleep(POLL_INTERVAL_SEC)
                continue

            # 조회 실패(None)면 이번 사이클은 판단하지 않는다.
            # 현재가를 모르면 손절을 오래된 캔들 종가로 판단하게 되고,
            # 보유를 모르면 ENTRY(미보유 전제)도 STOP(보유 전제)도 신뢰할 수 없다.
            if prices is None:
                log.warning("현재가 조회 실패 — 이번 사이클 판단 보류")
                time.sleep(POLL_INTERVAL_SEC)
                continue
            if holdings is None:
                log.warning("보유 조회 실패 — 이번 사이클 판단 보류")
                time.sleep(POLL_INTERVAL_SEC)
                continue
            self._last_holdings = {k: v for k, v in holdings.items() if v.get("qty", 0) > 0}

            self.report_stats()
            if self.tracker:
                self.tracker.flush(prices)
            for t in active:
                try:
                    self.evaluate(t, prices, holdings)
                except Exception as e:
                    log.exception("%s 평가 오류: %s", t, e)
            # 프리마켓은 지표만 갱신하고 판단은 건너뛴다 (알림 없음)
            for t in pre:
                try:
                    snap = self._snapshot(t, prices)
                    if snap:
                        self.snapshots[t] = snap
                except Exception as e:
                    log.exception("%s 프리마켓 조회 오류: %s", t, e)
            # 요약은 평가 뒤에 보낸다. 이번 사이클의 지표를 써야 최신 상태가 담긴다.
            self.market_summary(active, holdings, pre)
            time.sleep(POLL_INTERVAL_SEC)


# ---------------------------------------------------------------------------


def log_timestamp_sample(client: TossReadOnlyClient):
    """기동 시 실제 타임스탬프 형식을 한 번 보여준다. UTC 가정이 맞는지 사람이 확인한다."""
    for t in TICKERS:
        candles = client.get_candles(t, count=1)
        if candles:
            raw = candles[-1].get("timestamp")
            local = parse_ts(raw, WATCHLIST[t]["market"])
            log.info("타임스탬프 확인 — %s 원본: %s → 현지 해석: %s", t, raw,
                     local.strftime("%Y-%m-%d %H:%M %Z") if local else "해석 실패")
            return


if __name__ == "__main__":
    if not CLIENT_ID or not CLIENT_SECRET:
        raise SystemExit("토스 인증 정보가 없다. 같은 폴더 .env 에 TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 을 넣을 것.")
    cli = TossReadOnlyClient(CLIENT_ID, CLIENT_SECRET)
    log_timestamp_sample(cli)
    watch = WATCH_HOLDINGS and cli.load_account()
    if not watch:
        log.info("보유 조회 비활성 — ENTRY 알림만 나온다")
    notifier = Notifier()
    if TG_TOKEN and TG_CHATS:
        log.info("텔레그램 수신자 %d명", len(TG_CHATS))
        notifier.send("⚪ 시스템", "감시 시작", f"{', '.join(TICKERS)}")
    else:
        log.info("텔레그램 미설정 — 로그 파일에만 기록된다")
    SignalEngine(cli, notifier, watch).run()
