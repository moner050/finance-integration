# 단타 알림·자동매매 시스템 (토스증권 Open API)

토스증권 Open API 로 1분봉·현재가·보유를 30초마다 읽어 VWAP·RVOL·선행 바스켓 조건으로
**매수 / 손절 / 익절 신호**를 만들고 텔레그램으로 보낸다. 감시 종목은 백오피스 화면에서 지정한다.
자동매매는 구현되어 있지만 **기본은 꺼져 있고**, 세 가지 게이트를 모두 켜야 실제 주문이 나간다.

| 구성 요소 | 실행 | 역할 |
|---|---|---|
| 엔진 워커 | `python run_engine.py` | 30초 폴링 → 지표 → 상태기계 → 텔레그램 알림 → (자동매매) |
| 백오피스 | `python run_backoffice.py` | http://127.0.0.1:8000 — 종목·상태·신호 이력·채널·자동매매 |
| Binance 워커 | `python run_binance.py` | 20초 폴링 → 5분봉 ETC 급락 매수 후보 · 4시간봉/일봉 BTC 급등 추종 · 일봉 ETC 급락 추종 숏 → 텔레그램 (토스 엔진과 독립, 3.5절 · 자동매매 dry/live 는 3.6절) |
| MySQL | 이미 쓰는 서버 | `alert_*` 테이블 5개. 세 프로세스가 공유하는 유일한 통로 |

---

## 1. 동작 원리

### 1.1 신호 흐름

```
토스 API ──(1분봉·현재가·보유)──▶ 엔진 ──▶ 지표 ──▶ 상태기계 ──▶ Signal ──▶ Dispatcher ──▶ 텔레그램
                                          │                                       └──▶ MySQL alert_signal_log
                                          └──▶ (AUTOTRADE_MODE≠off) 실행기 ──▶ 정책 ──▶ 브로커 ──▶ alert_orders
```

- **지표**는 완성된 봉만 쓴다. 진행 중인 마지막 봉은 거래량이 부분값이라 뺀다.
- **RVOL(상대 거래량)**: 같은 현지 시각의 과거 8세션 중앙값 대비 배수. 표본이 없으면 직전 정규장 20봉 평균 대비.
- **VWAP·세션 정점 RVOL**: 당일 정규장 봉을 누적해서 계산한다(120봉 창이 아니다).
- **매수 신호(🔵)**: 선행 바스켓 방향 OK + RVOL 2.0 돌파 + 신호봉 종가가 VWAP 밴드 위 + 강봉(종가가 봉 상단 절반). 정규장 봉에서만.
- **보유 중**: 손절(-5%) > 신호봉 저점/밴드 이탈 매도 > 거래량 소진 익절 > 불타기 > 일부 익절 검토 > 판단 애매 > 마감 30분 전.

### 1.2 종목별 상태기계

`관망 → 진입대기(매수 알림) → 보유(실제 보유 확인) → 청산대기(매도 알림) → 관망(보유 소멸)`.
상태 전이는 시간이 아니라 **실제 보유 변화**로 일어난다. 알림대로 안 움직이면 같은 알림이 쿨다운 간격으로 반복된다.
상태·손절선·타이머는 MySQL 에 저장되어 재시작해도 이어진다.

### 1.3 알림 등급과 쿨다운

| 등급 | 알림 | 쿨다운 |
|---|---|---|
| action | 🔵 매수 · 🔴 손절/매도 · 🟢 전량 익절 · 🔵 추가매수 · 🟠 마감 정리 · 📤✅🚫⛔ 주문 관련 · 🔵 급락 매수 후보 · 🔵 눌림 재돌파 진입 후보 · 🔴 반등 실패 숏 후보 · 📥📤 가상 포지션 진입/종료(Binance) | 15분 (주문 관련은 없음, Binance 급락 매수 60분 · 추종은 보유 한도 7일/20일) |
| review | 🟡 절반/1/3 익절 검토 · ⚪ 매수 취소 · 🎉✅ 청산 완료 · 📈 급등 확인 관찰 · 📉 급락 확인 관찰(Binance) | 45분 / 15분 (Binance 추종은 7일/20일) |
| info | 📊 시황(30분) · 🔔🔕 장 시작/마감 · 📈 오늘 성적 · ⚪ 시스템 | 없음 |

쿨다운 키는 (신호 종류, 종목)이다. `TELEGRAM_MIN_SEVERITY` 로 받을 최소 등급을 정한다.

---

## 2. 디렉터리

```
alertbot/
  config.py          .env 로드, 임계값 상수, 초기 종목(SEED_WATCHLIST)
  timeutil.py        거래소 현지시각 변환 (KR/US, 서머타임)
  toss_client.py     토스 읽기 전용 클라이언트 (토큰·캔들·현재가·보유·캘린더) — GET 만
  market_hours.py    개장/휴장/조기폐장 판정 (캘린더 API, 실패 시 고정 시간)
  indicators.py      RVOL·VWAP(세션 누적)·EMA·RSI(Wilder)·ATR(True Range)
  engine.py          신호 엔진 (상태기계, 시황 요약, 워치리스트 핫리로드, 상태 저장·복원, 자동매매 훅)
  binance_crash.py   Binance 선물 5분봉 급락 매수 알림 (공개 REST 폴링 · 판정 · 워커) — 별도 프로세스
  binance_follow.py  Binance 선물 추종 알림 — 급등 추종 롱(4시간봉·일봉)·급락 추종 숏(일봉). 사양(FOLLOW_SPECS)별 워커, 일봉 EMA200 국면
  binance_trade.py   Binance 선물 자동매매 dry/live — 진입 후보 체결, 마크 손절·보유 한도·펀딩, alert_binance_positions
  binance_broker.py  Binance 선물 서명 클라이언트 (live) — 헤지 모드·격리·배율 설정, 시장가, 손절 알고 주문, 포지션 조회
check_binance_dry.py   과거 신호봉으로 dry 진입·종료를 실제 경로로 확인하는 도구 (3.6절 확인 절차)
  tracking.py        signal_tracking.csv(신호 뒤 15/30/60분 가격), trade_log.csv(청산 기록)
  models.py          Signal (종류·등급·쿨다운)
  notify/            Dispatcher(쿨다운·이력) + telegram 채널
  db.py              MySQL 저장소 (alert_watchlist / alert_engine_status / alert_signal_log / alert_settings / alert_orders)
  trading/           자동매매: broker(TossOrderClient·DryRunBroker) · policy(리스크 정책) · executor(실행기) · models
  backoffice/        FastAPI + Jinja2 + HTMX 화면
run_engine.py        엔진 진입점          run_backoffice.py   백오피스 진입점      run_binance.py   Binance 워커 진입점
Dockerfile           docker-compose.yml   우분투 배포          tests/   pytest 99개
```

---

## 3. 설정값

### 3.1 `.env` (프로젝트 루트, 따옴표·등호 앞뒤 공백 없이)

| 키 | 필수 | 의미 | 기본 |
|---|---|---|---|
| `TOSS_CLIENT_ID` / `TOSS_CLIENT_SECRET` | ✔ | 토스 WTS > 설정 > Open API 에서 발급. **허용 IP 관리에 실행 PC 의 공인 IP 등록** (미등록 IP 는 403) | |
| `TELEGRAM_BOT_TOKEN` | ✔ | BotFather 가 주는 `1234567890:AA...` 전체 | |
| `TELEGRAM_CHAT_ID` | ✔ | **받는 사람** 채팅의 숫자 ID (봇 ID 아님). 여러 명은 쉼표. 각 수신자는 봇에게 먼저 `/start` | |
| `TELEGRAM_MIN_SEVERITY` | | 받을 최소 등급 `info` / `review` / `action` | info |
| `MYSQL_HOST` `MYSQL_PORT` `MYSQL_DATABASE` `MYSQL_USER` `MYSQL_PASSWORD` | ✔ | 기존 MySQL. 테이블은 `alert_` 접두어로 자동 생성 | |
| `ALERT_BACKOFFICE_HOST` / `ALERT_BACKOFFICE_PORT` | | 백오피스 바인드 주소·포트. 인증이 없으므로 로컬 전용 권장 | 127.0.0.1 / 8000 |
| `ALERT_DATA_DIR` | | 로그·CSV 저장 폴더. Docker 는 `/data` | 프로젝트 루트 |
| `ALERT_BINANCE_SYMBOLS` | | Binance 5분봉 급락 매수 알림 심볼(쉼표, USDⓈ-M 무기한). 공개 API 라 키 불필요 | ETCUSDT |
| `ALERT_BINANCE_SURGE_SYMBOLS` | | Binance 4시간봉 급등 추종 롱 알림 심볼(쉼표) | BTCUSDT |
| `ALERT_BINANCE_SURGE_1D_SYMBOLS` | | Binance 일봉 급등 추종 롱 알림 심볼 | BTCUSDT |
| `ALERT_BINANCE_CRASHFOLLOW_1D_SYMBOLS` | | Binance 일봉 급락 추종 숏 알림 심볼 | ETCUSDT |
| `ALERT_BINANCE_TRADE_MODE` | | Binance 자동매매 모드 `off`(알림만) / `dry`(가상 체결) / `live`(실제 주문), 3.6절 | off |
| `ALERT_BINANCE_API_KEY` / `ALERT_BINANCE_API_SECRET` | live 만 | Binance 선물 API 키 — **선물 거래 권한만, 출금 권한 없이** | |
| `ALERT_BINANCE_TRADE_CAPITAL` | | dry 자동매매의 전략별 배분 자본 (USDT) | 1000 |
| `AUTOTRADE_MODE` | | 자동매매 모드 `off` / `dry` / `live` (4절) | off |
| `AUTOTRADE_BUY_BUFFER_PCT` | | 매수 지정가 = 신호가 × (1 + 이 %) | 0.3 |
| `AUTOTRADE_BUY_TTL_MIN` | | 매수 지정가가 이 분 안에 안 체결되면 취소 | 3 |
| `AUTOTRADE_HARD_MAX_AMOUNT_KRW` / `_USD` | | 1회 매수 금액 하드캡. DB 한도보다 우선 | 2,000,000 / 2,000 |

같은 `.env` 를 다른 프로젝트와 공유해도 된다. 이 프로젝트가 읽는 접두어는 `TOSS_ TELEGRAM_ MYSQL_ ALERT_ AUTOTRADE_` 뿐이다.
환경변수로도 같은 키를 줄 수 있다(Docker `env_file`). `.env` 값이 우선한다.

### 3.2 임계값 (`alertbot/config.py`, 코드 상수)

| 상수 | 기본 | 의미 |
|---|---|---|
| `POLL_INTERVAL_SEC` | 30 | 폴링 주기. 매수 판단은 완성봉 기준이라 봉당 1회 |
| `RVOL_TRIGGER` | 2.0 | 매수 신호 거래량 배수 (직전봉 < 2.0 ≤ 현재봉 돌파) |
| `VWAP_BAND_PCT` / `ATR_BAND_MULT` | 0.15 / 0.5 | 기준선 밴드 하한 % / 변동성(ATR%) 배수 중 큰 쪽 |
| `STRONG_BAR_MIN` | 0.5 | 신호봉 종가가 봉 범위의 이 비율 이상 위치 |
| `LEADER_GAP_PCT` | 1.0 | 선행 바스켓 평균 등락률 트리거 (%) |
| `LEADER_MOMENTUM_MIN` / `LEADER_MOMENTUM_GATE` | 5 / False | 선행 최근 5분 변화율 기록(추적 CSV `leader_mom`) / True 면 방향 판정에도 사용 |
| `STOP_LOSS_PCT` | -5.0 | 평단 대비 고정 손절 한도 |
| `FADE_STRONG_RATIO` / `FADE_WEAK_RATIO` / `FADE_MIN_PEAK` | 0.4 / 0.6 / 2.5 | 세션 정점 대비 거래량 소진 비율(전량 익절 / 절반 검토), 정점 최소 배수 |
| `OPEN_EXCLUDE_MIN` | 10 | 개장 후 이 분 동안의 봉은 정점 계산에서 제외 |
| `ALERT_COOLDOWN_MIN` / `WEAK_COOLDOWN_MIN` | 15 / 45 | 강한/약한 알림 재발송 간격 |
| `REENTRY_BLOCK_MIN` / `EXIT_GRACE_MIN` | 60 / 20 | 매도 알림 뒤 매수 차단 / 매수 뒤 익절 알림 유예 |
| `ENTRY_MIN_PEAK_RATIO` | 0.6 | 매수 신호 RVOL 이 그날 정점의 이 비율 이상이어야 함 |
| `CLOSE_WARN_MIN` / `SUMMARY_INTERVAL_MIN` | 30 / 30 | 마감 전 정리 알림 / 시황 요약 주기 |
| `ENABLE_ADD_ON` `ADDON_MIN_PROFIT_PCT` `ADDON_MAX_COUNT` | True / 2.0 / 1 | 불타기 알림 |
| `PROFILE_PAGES` / `MIN_PROFILE_SESSIONS` | 16 / 3 | 거래량 프로파일 이력(200봉×16) / 시각당 최소 표본 |
| `WATCH_HOLDINGS` | True | 보유 조회. False 면 매수 알림만 |

### 3.3 워치리스트 항목 (백오피스 '종목')

| 필드 | 의미 |
|---|---|
| 심볼 / 시장 | KR 은 6자리 코드(005930), US 는 티커(SOXX). 시장은 장 시간·타임존을 정한다 |
| 표시명 | 알림에 보이는 이름 (없으면 심볼) |
| 선행 바스켓 | 방향 조건에 쓸 종목들(쉼표). 비우면 방향 조건 생략(개별주) |
| 인버스 | 선행 방향을 뒤집는다 (KODEX 인버스 등) |
| 페어 | 동시 진입을 막을 반대 종목 (SOXL↔SOXS). 양쪽이 서로를 가리켜야 하며 아니면 `!` 경고 |
| 보유 중에만 감시 | 3배 상품처럼 매수 신호는 내지 않고 보유 중 손절·익절만 감시 |
| 활성 | 끄면 감시에서 빠진다(이력 보존). 삭제보다 권장 |
| 자동매매 대상 / 1회 매수 금액 | 자동매매 게이트(4절). 금액 통화는 시장을 따른다(KRW / USD) |

저장하면 엔진이 다음 사이클(30초 안)에 반영한다. 재시작 불필요.

### 3.4 자동매매 한도 (백오피스 '자동매매', MySQL `alert_settings`)

| 키 | 기본 | 의미 |
|---|---|---|
| `autotrade_enabled` | 0 | 킬 스위치. 1 이어야 주문 |
| `max_positions` | 3 | 동시 보유 종목 수 상한 (열린 매수 의도 포함) |
| `max_orders_per_day` | 20 | 하루 주문 횟수 상한. **손절 매도는 면제** |
| `daily_loss_limit_krw` / `_usd` | 300,000 / 200 | 오늘 실현손실이 이 아래면 **매수 중단** (매도는 계속) |
| `max_order_amount_krw` / `_usd` | 1,000,000 / 1,000 | 1회 매수 금액 상한. `.env` 하드캡이 더 작으면 그쪽 |

---

### 3.5 Binance 알림 (`run_binance.py` — `binance_crash.py` 급락 매수 5분봉, `binance_follow.py` 추종 알림 상위 봉)

토스 엔진과 별개의 워커 한 프로세스가 네 알림을 돌린다. Binance USDⓈ-M 무기한 선물 봉을 공개 REST 로 20초마다 받아(키 불필요)
**완성봉마다 한 번** 판정한다. 신호는 텔레그램과 `alert_signal_log`(백오피스 '신호 이력')에 남고, 로그는 `binance_signals.log` 다.
자동매매는 3.6절(dry 가상 체결 / live 실제 주문)에 있고, 보유 판단은 없다.

**급락 매수 (5분봉, 기본 ETCUSDT, 종류 `CRASH_BUY`, 같은 심볼 60분 쿨다운)**

| 조건 (모두 만족) | 값 | 상수 |
|---|---|---|
| 급락 | 직전 48봉(4시간) 고점 대비 종가 하락폭 ≥ 기준 ATR × 10 | `CRASH_LOOKBACK` / `CRASH_ATR_MULT` |
| 과매도 | RSI14 ≤ 30 | `CRASH_RSI_MAX` |
| 반전봉 | 신호봉 종가가 봉 범위의 상위 40% (종가 위치 ≥ 0.6) | `CRASH_CLOSE_POS_MIN` |
| 국면 | 4시간봉 EMA9 ≤ EMA21 (하락 배열). 상승 배열이면 로그만 남기고 보류. 급락 조건이 성립한 때만 4시간봉 120개를 받아 판정 | `CRASH_H4_FILTER` / `CRASH_H4_KLINES` |

기준 ATR 은 직전 3일(864봉) ATR14% 의 중앙값(`CRASH_BASE_ATR_BARS`)이라 급락 자체가 ATR 을 부풀리는 효과가 없다.
ETC 라면 대략 -2% 이상의 4시간 낙폭이다. 메시지에는 판단 참고로 RVOL(직전 60봉 중앙값 대비), 아래꼬리, 테이커 매수비,
같은 구간 BTC 하락폭(베타 1.35 보정 기여 ≥ 0.6 이면 "BTC 동반", < 0.25 면 "ETC 단독"), 직전 펀딩비, 그리고
4시간봉 EMA9/EMA21 배열, 손절 참고선(신호봉 종가 −3%, `CRASH_STOP_PCT`)과 보유 한도(8시간, `CRASH_HOLD_HOURS`), 낙폭의 50% 되돌림선을 함께 싣는다. 목표 지정가는 두지 않는다.

근거는 2026-09-15 분석(1분·5분·1시간봉, ETC·BTC 30~90일): 5분봉 ETC 에서 이 트리거가 수수료(왕복 0.1%) 뒤에도
양(+)이었던 유일한 급락 매수 조건이다(37건, 승률 62%, ±8 기준 ATR 브래킷 순평균 +0.31%). 1분봉은 어느 조건도
수수료를 못 넘겼고, 급등 숏은 전부 손실이라 만들지 않았다. 표본 국면이 상승장이었다는 한계가 있다.
손절 참고선은 2026-09-15 레버리지 분석(마크 가격 봉으로 재생, 보고서 「청산선 밖의 배율」)에서 바꿨다: 원래의 신호봉 저가 − 2 기준 ATR(진입 대비 0.7%)은
5분봉 스윕 깊이(레벨 재시험 때 되돌아온 침투의 p90 3.75 기준 ATR) 안이라 45건 중 19건이 걸려 우위가 사라졌고, 종가 −3% 재난 손절 + 5시간 시간 종료는
평균 +0.17%(승률 56%, 손절 3건)로 남았다. 50% 되돌림 목표를 두면 평균이 +0.15% 로 내려가 목표는 두지 않는다.
2026-09-15 표본 확장(1-1~9-15 257일, 129건, 보고서 「여덟 달의 급락」): 종가 −3%·5시간 규칙은 전체 평균 −0.04% 로 우위가 없었다(1~6월 −0.17%,
6-17 이후 +0.18% — 90일 표본의 우위는 반등장 국면 효과). 살아남은 것은 보유 8시간과 4시간봉 하락 배열 필터다. 둘을 합친 규칙(80건)은 평균 +0.49%
(t 2.1), 9개월 중 8개월 양수이고, 상승 배열 중의 급락은 −0.41%(t −2.0)라 알리지 않는다. 129건 위에서 고른 필터라 dry 로 재확인한다. BTC 5분봉은 24개 조합 전부 우위가 없다.

**추종 알림 (상위 봉, 사양별 두 단계, 단계별 쿨다운 = 보유 한도)** — 사양은 `config.FOLLOW_SPECS`

| 사양 | 기본 심볼 | 급변 조건 (📈/📉 관찰, review) | 진입 조건 (🔵/🔴 후보, action) | 국면 | 보유 한도 | 손절 참고선 (`stop`) |
|---|---|---|---|---|---|---|
| 급등 추종 롱 4시간봉 | BTCUSDT | 30봉(5일) 저점 대비 상승 ≥ 기준 ATR × 6, RSI14 ≥ 70 | 급등 봉 뒤 10봉 안에 EMA9 아래로 눌렸다가 다시 위로 마감 (`SURGE_ENTRY`) | 일봉 EMA200 위 | 7일(42봉) | 눌림 저점 − 2.5 기준 ATR (진입 대비 중앙 −6%) |
| 급등 추종 롱 일봉 | BTCUSDT | 20봉 저점 대비 상승 ≥ 기준 ATR × 4 (약 +16%), RSI14 ≥ 70 | 위와 같음 (`SURGE_ENTRY_1D`) | 일봉 EMA200 위 | 20일 | 진입 −10% |
| 급락 추종 숏 일봉 | ETCUSDT | 20봉 고점 대비 하락 ≥ 기준 ATR × 4 (약 -27%), RSI14 ≤ 30 | 급락 봉 뒤 10봉 안에 EMA9 위로 반등했다가 다시 아래로 마감 (`CRASH_SHORT_1D`) | 일봉 EMA200 아래 | 20일 | 진입 +25% |

관찰 알림은 급변 조건이 **처음 성립한 봉**에 한 번, 진입 후보는 재돌파(재이탈) 봉에 한 번 나간다. 국면이 맞지 않으면 로그만 남기고 보류한다.
기준 ATR 은 직전 30일(4시간봉 180봉) 또는 90일(일봉) ATR14% 중앙값이고, 메시지에는 RSI·RVOL·EMA9·눌림 저점(반등 고점)·국면(일봉 종가/EMA200)·
펀딩(롱은 +3bp/8h 초과, 숏은 -3bp 미만이면 과밀 표기)과 손절 참고선(사양의 `stop` 규칙; 관찰 단계에는 규칙과 현재 기준 ATR 만)을 싣는다. 목표 지정가는 두지 않는다.
급등 **숏** 과 상위 봉 급락 **매수** 알림은 만들지 않았다 — 스윙 분석에서 전자는 어느 봉이든 손실, 후자는 일봉 청산 바닥에서만 통했다.

근거는 2026-09-15 스윙 분석(1시간·4시간·일·주봉, 상장 이후 전체, 펀딩·수수료 반영): 4시간봉 BTC 급등 추종(M2) 순평균 +0.86%(승률 60%, 63건,
강세 국면 +1.19%), 일봉 BTC 급등 추종(M2) +7.4%(승률 84%, 19건), 일봉 ETC 급락 추종 숏(M2) +3.5%(9건 중 8건 이익, 전부 약세 국면).
일봉 표본은 20건 안팎이라 승률의 소수점은 의미가 없다.
손절 참고선은 2026-09-15 레버리지 분석에서 정했다: ±8 기준 ATR 브래킷(4시간봉 −12%)은 58건 중 4건만 걸려 손절 역할을 못 했고, 눌림 저점 − 2.5 기준 ATR 이
평균 +1.5%·켈리 3.0 으로 최고였다. 일봉 롱은 이긴 거래의 최대 역행폭이 8.2% 라 −10%, 일봉 숏은 반등 고점이 8건 중 6건에서 5~7% 더 뚫려 +25% 다.
세 사양 모두 목표를 두면 평균이 내려가 목표 지정가는 없다.

### 3.6 Binance 자동매매 — dry / live (`binance_trade.py`, `binance_broker.py`, `ALERT_BINANCE_TRADE_MODE`)

`run_binance.py` 가 진입 후보 알림(🔵 급락 매수 후보 · 🔵 눌림 재돌파 진입 후보 · 🔴 반등 실패 숏 후보)을 체결하고 열린 포지션을 20초마다 감시한다.
`dry` 는 공개 시세로 **가상 체결**(API 키 불필요, 주문 없음), `live` 는 `binance_broker.py` 로 **실제 주문**을 낸다. 설계는 2026-09-15 레버리지 분석
「청산선 밖의 배율」을 따른다.

| 항목 | 규칙 | 상수 |
|---|---|---|
| 크기 | 명목가 = 전략별 배분 자본 × 유효 배율. 급락 매수 2 · 4시간봉 급등 추종 1 · 일봉 급등 추종 1.5 · 일봉 급락 숏 0.5 (분석의 시작값, 최대 3 / 1.5 / 2 / 1) | `ALERT_BINANCE_TRADE_CAPITAL`(기본 1000 USDT), `BINANCE_TRADE_LEVERAGE` |
| 펀딩 게이트 | 진입 시 펀딩이 롱 +3bp/8h 초과, 숏 -3bp 미만이면 크기 절반 | `SURGE_FUNDING_WARN` |
| 체결 | 진입·종료 모두 마지막 체결가 ± 슬리피지(ETC 0.05%, BTC 0.02%), 수수료 테이커 0.05% 편도 | `BINANCE_TRADE_SLIP`, `BINANCE_TRADE_FEE` |
| 손절 | 알림의 손절 참고선(3.5절). **마크 가격**이 닿으면 종료 — 실제 STOP_MARKET(MARK_PRICE) 주문과 같은 조건 | |
| 종료 | 보유 한도(5분봉 8시간 · 4시간봉 7일 · 일봉 20일)에 닿으면 종료. 목표 지정가 없음 | |
| 펀딩 | 정산 시각(00·08·16 UTC)을 지날 때마다 정산된 펀딩비 × 명목가 (롱은 양의 펀딩 지급) | |
| 한도 | 전략당 열린 포지션 하나 · 합산 명목 ≤ 자본 합 × 3 · 오늘(KST) 실현손실이 자본 합의 6% 를 넘으면 신규 진입 중단 | `BINANCE_TRADE_MAX_TOTAL_LEV`, `BINANCE_TRADE_DAILY_LOSS_PCT` |

포지션은 MySQL `alert_binance_positions` 에만 있어 재시작해도 이어지고, 백오피스 '자동매매' 화면 아래쪽 표와 텔레그램(📥 진입 · 📤 종료 · ⏸ 보류 · ⛔ 실패,
본문 앞에 `[DRY]`/`[LIVE]`)으로 본다. 같은 심볼의 롱(5분봉)과 숏(일봉)이 동시에 열릴 수 있어 live 는 헤지 모드를 쓴다.

**live 동작** — 3중 조건: `.env` `ALERT_BINANCE_TRADE_MODE=live` + API 키 + 백오피스 '자동매매' 화면의 **Binance 킬 스위치 ON**.

- 기동 때 서버 시각 동기화, 심볼 필터(수량·가격 단위, 최소 명목) 조회, **헤지 모드 · 격리 마진 · 심볼 배율 3배**(`BINANCE_TRADE_EXCHANGE_LEV`, 청산 거리 33%)를 맞춘다.
  포지션이 열려 있어 모드를 못 바꾸거나 키가 틀리면 기동을 멈춘다. 시작 메시지에 가용 USDT 와 킬 스위치 상태가 나온다.
- 진입: 명목가를 수량 단위로 내림해 **시장가**(positionSide LONG/SHORT) → 체결 직후 **STOP_MARKET 알고 주문**(`/fapi/v1/algoOrder`, 마크 가격 트리거,
  closePosition). Binance 는 2025-12-09 부터 조건부 주문을 알고 주문 API 로만 받는다. 손절 주문만 실패하면 포지션은 두고 ⛔ 알림 뒤 사이클마다 다시 건다.
- 감시: 손절 주문이 트리거됐으면 그 체결가로 종료 기록, 포지션이 거래소에서 사라졌으면 '직접 종료' 기록, 손절 주문이 취소·만료됐으면 다시 건다.
  보유 한도가 되면 손절을 취소하고 시장가로 닫는다.
- 실패: 진입 주문이 3회 연속 실패하면 킬 스위치를 끄고 ⛔ 알림. 손익·수수료·펀딩은 dry 와 같은 추정값이다(거래소 정산값이 아니다).
- 전환 순서: dry 로 기록을 검토 → `.env` 에 키와 `live` → 워커 재시작(시작 메시지의 가용 USDT 확인) → 백오피스에서 Binance 킬 스위치 ON.
  live 로 바꿔도 dry 때의 열린 가상 포지션은 live 트레이더가 건드리지 않는다(모드별로 분리).

**dry 확인 절차** — 실제 신호는 드물어서(5분봉 하루 0.5건, 상위 봉 연 수 건) 기다리지 않고 세 단계로 확인한다.

1. 기동 확인. `.env` 에 `ALERT_BINANCE_TRADE_MODE=dry`, `ALERT_BINANCE_TRADE_CAPITAL` 을 넣고 워커를 잠깐 돌린다.
   ```bash
   timeout 45 python run_binance.py
   ```
   로그에 `Binance 자동매매 dry: 전략별 자본 … · 합산 명목 한도 … · 일손실 한도 …` 가 찍히고, 텔레그램 시작 메시지 마지막 줄에
   `자동매매 dry (가상 체결): 전략별 자본 … · 유효 배율 …` 이 오면 설정이 읽힌 것이다. 백오피스 '신호 이력'의 SYSTEM 행 결과가 `telegram: ok` 다.
2. 진입·종료 경로 확인. 과거에 실제로 신호가 났던 봉을 워커에 먹여 알림 → 📥 진입 → 감시 → 📤 종료까지 돌린다. 봉은 Binance 에서 받아 오고,
   체결 시세는 현재 값이며, 확인이 끝나면 강제 종료하고 확인용 포지션 행을 지운다. `--quiet` 를 붙이면 텔레그램 대신 콘솔에 찍는다.
   ```bash
   python check_binance_dry.py crash "2026-09-14 07:15"
   ```
   사양별로 검증된 과거 사례: `crash "2026-09-14 07:15"` · `surge_4h "2026-09-05 21:00"` · `surge_1d "2026-09-03 09:00"` · `crash_1d "2026-06-12 09:00"`
   (모두 KST, 신호봉 시작 시각). 다른 시각을 주면 "그 봉은 신호 조건이 아니다" 로 끝난다. 이 도구는 `.env` 모드와 무관하게 항상 dry 트레이더를 쓴다.
   확인할 것: 알림 본문의 손절 참고선과 📥 메시지의 손절이 같은지, 명목 = 자본 × 배율인지(펀딩 게이트에 걸리면 절반), 보유 한도 시각이 사양(8시간·7일·20일)과 맞는지. 5분봉 사례가 "보류"로 끝나면 그 시각의 4시간봉이 상승 배열이었던 것이다.
3. 화면 확인. 백오피스 '자동매매' 화면 아래 "Binance 선물 자동매매 — 포지션" 표에 열린·닫힌 포지션이 KST 시각으로 나온다(도구가 지운 확인용 행은 안 보인다).
   상시 운영 뒤 실제 신호가 오면 같은 형식의 📥/📤 가 오고 표에 남는다. dry 로 1~2주 운영해 표본 성과(3.5절)와 비슷한지 본 뒤 live 로 간다.

## 4. `AUTOTRADE_MODE` 상세

`.env` 의 `AUTOTRADE_MODE` 는 엔진이 **실행기(Executor)를 만들지, 어떤 브로커를 붙일지**를 정한다.
키가 없으면 `off` 다. 코드 배포·업데이트만으로는 절대 live 가 되지 않는다.

| 모드 | 실행기 | 브로커 | 정책 검사 | `alert_orders` 기록 | 텔레그램 |
|---|---|---|---|---|---|
| `off` | 만들지 않음 | — | — | 없음 | 신호 알림만 |
| `dry` | 만듦 | `DryRunBroker` (API 호출 없음, 지정가/참조가로 즉시 가상 체결) | 실제와 동일 | 의도·거절 사유·가상 체결 | `[DRY]` 접두어로 주문 접수/체결 알림 |
| `live` | 만듦 | `TossOrderClient` (실제 `POST /api/v1/orders`) | 실제와 동일 | 실제 주문번호·체결·실현손익 | 주문 접수/체결/실패/차단 알림 |

### 4.1 실제 주문이 나가는 조건 (3중 게이트)

1. `AUTOTRADE_MODE=live` (`.env`, 엔진 재시작 필요)
2. 백오피스 '자동매매' 화면의 **킬 스위치 ON** (`alert_settings.autotrade_enabled=1`, 재시작 불필요, 엔진이 매 사이클 읽음)
3. 해당 종목의 **자동매매 대상 체크 + 1회 매수 금액 > 0** ('종목' 화면)

하나라도 아니면 의도는 `rejected` 로 기록되고 사유(`disabled`, `symbol-not-auto` …)가 남는다. 알림은 그대로 나간다.

### 4.2 dry 모드가 하는 일

- 매수 신호 → 지정가·수량을 계산하고 정책을 통과하면 `alert_orders` 에 `dry` 의도로 기록, 다음 사이클에 지정가로 가상 체결.
- 매도 신호 → 보유 수량 전량 시장가 의도, 참조가(신호 시점 현재가)로 가상 체결, 평단 대비 실현손익 계산.
- 잔고는 무한대로 본다. dry 의 목적은 "무엇을 얼마나 주문했을지" 를 보는 것이지 잔고 시뮬레이션이 아니다.
- **live 로 가기 전 최소 1주 dry 로 운영**하고 백오피스 '자동매매' 표에서 의도·사유·가상 손익을 검토한다.

### 4.3 live 에서 주문이 만들어지는 방식

- 매수: 신호가 × (1 + `AUTOTRADE_BUY_BUFFER_PCT`%) 지정가(호가 단위 보정), 수량 = 1회 매수 금액 ÷ 지정가 (내림, 1주 미만이면 거절).
  `AUTOTRADE_BUY_TTL_MIN` 안에 미체결이면 취소. 같은 신호봉으로는 한 번만.
- 매도: 🔴 손절/매도 · 🟢 전량 익절 · 🟠 마감 정리 신호에 매도가능수량 전량 **시장가**. 미체결 매수가 있으면 먼저 취소.
  불타기(추가매수)·일부 익절(절반·1/3)은 **알림만** 낸다.
- `clientOrderId` 멱등키(의도 ID)를 보내 재시도·재시작으로 같은 주문이 두 번 나가지 않는다.
- 자동 차단: 주문 실패 3회 연속, 또는 `prerequisite-required`(약관·교육 미완료) → 킬 스위치를 끄고 ⛔ 알림.
- 정규장 봉·정규장 시간에만 주문한다. 한국 NXT 시간외, 미국 프리·애프터마켓은 제외.

### 4.4 사전 조건과 전환 순서

1. 토스 WTS 에서 **약관 동의·교육 이수·위험 고지** 완료. 백오피스 '자동매매' 의 **주문 권한 확인** 버튼(매수가능금액 조회)이 성공해야 한다.
2. `.env` 에 `AUTOTRADE_MODE=dry` → 엔진 재시작 → 킬 스위치 ON → 종목별 자동매매·금액 지정 → 1주 이상 운영·검토.
3. 납득되면 `AUTOTRADE_MODE=live` → 엔진 재시작. 한도(3.4)를 먼저 작게 잡고 시작한다.
4. 문제가 생기면 킬 스위치를 끈다(즉시, 재시작 불필요). 미결 주문은 표에서 수동 취소할 수 있다.

주문 코드는 `alertbot/trading/broker.py` 에만 있다. `toss_client.py` 는 GET 만 한다.

---

## 5. 배포 — Windows (생 파이썬)

요구사항: Python 3.12 이상(개발은 3.14), 인터넷, MySQL 접근.

```powershell
cd C:\workspace\personal\finance-integration
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

1. `.env` 작성(3.1). 토스 허용 IP 에 이 PC 의 공인 IP 를 등록한다.
2. 테이블 생성과 초기 종목 시딩 (엔진이 첫 실행 때 자동으로도 한다):

```powershell
python -m alertbot.db init
python -m alertbot.db seed
```

3. 콘솔 두 개로 실행 (Binance 급락 알림도 쓰면 세 번째 콘솔):

```powershell
python run_engine.py
```
```powershell
python run_backoffice.py
```
```powershell
python run_binance.py
```

4. 텔레그램에 `⚪ 시스템 | 감시 시작` 이 오면 정상. 백오피스 http://127.0.0.1:8000 의 '상태' 에 heartbeat 가 보인다.

백그라운드 실행은 **작업 스케줄러**로 두 항목("로그온 시 시작", 프로그램 `...\.venv\Scripts\python.exe`, 인수 `run_engine.py`, 시작 위치 프로젝트 폴더; 백오피스도 같은 방식)을 만들면 된다. 콘솔 인코딩 문제로 이모지가 `?` 로 보여도 파일 로그와 텔레그램은 정상이다.

산출물: `scalping_signals.log`, `signal_tracking.csv`, `trade_log.csv`, `binance_signals.log` (프로젝트 루트 또는 `ALERT_DATA_DIR`).
업데이트: `git pull` → `pip install -r requirements.txt` → 두 프로세스 재시작. 스키마 변경은 기동 시 자동 반영된다.

---

## 6. 배포 — Ubuntu (Docker)

요구사항: Docker Engine + Compose 플러그인, MySQL 접근. 이미지는 `python:3.12-slim` 기반이며 엔진과 백오피스가 같은 이미지를 쓴다.

```bash
git clone <repo> alertbot && cd alertbot
cp /path/to/.env .env                 # 3.1 의 키. 파일 권한: chmod 600 .env
mkdir -p data                         # 로그·CSV 볼륨
docker compose up -d --build
docker compose logs -f engine         # "감시 시작" 과 "장 운영 KR/US ... (캘린더)" 확인
docker compose logs -f binance        # Binance 워커: "완성봉 1000개 확보" 와 "Binance 감시 시작" 확인
```

- 토스 허용 IP 에 **서버의 공인 IP** 를 등록해야 한다. 등록 전엔 403 으로 엔진이 종료된다.
- 백오피스는 `127.0.0.1:8000` 에만 공개된다(인증 없음). 밖에서 보려면 `ssh -L 8000:127.0.0.1:8000 user@server` 로 터널을 열고 http://localhost:8000 에 접속한다.
- MySQL 이 같은 서버에 있으면 `.env` 의 `MYSQL_HOST` 를 호스트 IP(예: `172.17.0.1`)로 두거나 compose 에 `extra_hosts: ["host.docker.internal:host-gateway"]` 를 추가하고 `host.docker.internal` 을 쓴다.
- 데이터: `./data/` 에 로그와 CSV 가 남는다. `.env` 는 이미지에 들어가지 않고 `env_file` 로 주입된다.
- 운영 명령:

```bash
docker compose restart engine        # .env 변경 반영 (예: AUTOTRADE_MODE)
docker compose up -d --build         # 코드 업데이트 후 재빌드
docker compose down                  # 중지
```

`restart: unless-stopped` 라 서버 재부팅 후 자동으로 올라온다. Docker 는 이 문서를 만든 개발 PC 에 설치되어 있지 않아 실제 빌드 검증은 하지 않았다.

---

## 7. 백오피스 사용법

| 화면 | 경로 | 기능 |
|---|---|---|
| 상태 | `/` | 마지막 사이클 시각(90초 넘으면 "멈췄을 수 있다" 경고), 감시 중·프리마켓 종목, 종목별 상태·현재가·종가·VWAP(밴드)·위치(현재가/종가)·RVOL(직전→현재, 방식)·정점·선행·손절선. 30초 자동 갱신 |
| 종목 | `/watchlist` | 추가/편집/중지/재개/삭제. **심볼 검증** 버튼은 토스 현재가 API 로 심볼·선행 종목을 실제 조회한다. 페어 정합성 `!` 경고 |
| 신호 이력 | `/signals` | `alert_signal_log` 최근 200건. 종목·등급 필터, 채널별 전송 결과(ok / error / skip) |
| 채널 | `/channels` | 텔레그램 설정 상태와 **테스트 발송**(쿨다운·등급 무시, 이력에 남음) |
| 자동매매 | `/trading` | 모드 표시, 킬 스위치, 한도 편집, 주문 의도 200건(상태·사유·주문번호·체결·손익), 미결 수동 취소, **주문 권한 확인** |

주문 의도 상태: `proposed`(검사 전) → `rejected`(정책 거절, 사유 기록) / `sent`·`open`(접수·대기) → `filled` / `partial` / `canceled`(TTL·수동·매도 전 취소) / `failed`(브로커 오류).

---

## 8. 텔레그램 설정

1. BotFather 에서 봇을 만들고 토큰(`1234567890:AA...`)을 `TELEGRAM_BOT_TOKEN` 에 넣는다.
2. 받는 사람이 봇 대화방을 열고 **시작(/start)** 을 누른다. 봇은 먼저 말을 건 상대에게만 보낼 수 있다.
3. 채팅 ID 조회 후 `TELEGRAM_CHAT_ID` 에 넣는다 (봇 자신의 ID 를 넣으면 `can't send messages to the bot` 오류):

```bash
python -c "import requests, alertbot.config as c; print(requests.get(f'https://api.telegram.org/bot{c.TG_TOKEN}/getUpdates', timeout=10).json())"
```

4. 백오피스 '채널' 의 테스트 발송으로 확인한다.

---

## 9. 문제 해결

| 증상 | 원인 · 조치 |
|---|---|
| 기동 직후 `403 — 허용 IP 미등록` 종료 | 토스 WTS > Open API > 허용 IP 관리에 실행 PC/서버 공인 IP 등록 |
| `텔레그램 전송 실패: chat not found` | `TELEGRAM_CHAT_ID` 가 채팅 ID 가 아니거나 수신자가 `/start` 를 안 눌렀다 |
| `텔레그램 전송 실패: Not Found` | 봇 토큰이 불완전하다 (`숫자:35자` 전체를 넣을 것) |
| `the bot can't send messages to the bot` | 채팅 ID 자리에 봇 ID 를 넣었다 |
| 백오피스 heartbeat 경고 | 엔진이 꺼졌거나 MySQL 연결 실패. 엔진 로그 확인 |
| `MySQL 연결 재수립 후 재시도` 경고 | 서버가 놀던 연결을 끊은 것. 자동 복구되며 정상 |
| `캘린더 ... 고정 시간으로 판단한다` | 캘린더 API 실패. 고정 시간(KR 09:00~15:30, US 09:30~16:00)으로 동작 |
| 주문 권한 확인 → `사전 자격 미충족` | 토스 WTS 에서 약관 동의·교육 이수·위험 고지 완료 후 재확인 |
| ⛔ 자동매매 차단 알림 | 연속 실패 또는 권한 오류. '자동매매' 표의 사유를 확인하고 킬 스위치를 다시 켠다 |
| 레이트리밋 경고(429) | 자동 대기·재시도. 반복되면 `MIN_CALL_GAP_SEC` 을 늘린다 |

---

## 10. 테스트

```bash
python -m pytest
```

지표 골든값(원본 스크립트 기준), 세션 누적, 캘린더 파싱, 엔진 상태 전이·복원, 알림 쿨다운·채널 격리, DB, 백오피스,
브로커(요청 페이로드·멱등키·호가 보정·오류 매핑), 정책 경계값, 실행기 시나리오(dry 체결·중복 방지·자동 차단),
Binance 급락 판정(합성 급락·반전봉 유무·BTC 동반·워커 쿨다운), Binance 추종 알림(급변 첫 봉·눌림 재돌파·반등 재이탈·국면 게이트·쿨다운, 4시간봉·일봉 롱·일봉 숏)을 덮는다.
실제 토스·텔레그램·MySQL·Binance 는 호출하지 않는다(DB 는 메모리 SQLite).

---

## 11. 원본 대비 감지기 변경 요약

- 세션 VWAP·정점 RVOL 을 120봉 창이 아니라 **정규장 봉 누적**으로 계산 (개장 2시간 뒤 기준선 표류·익절 신호 소실 제거).
- 이동평균 RVOL 기준선에서 프리마켓·시간외 봉 제외. 매수 판정은 신호봉 **종가** 기준, 정규장 봉에서만.
- 전일 종가를 날짜로 선택. 캘린더 API 로 휴장·조기폐장 반영. RSI 는 Wilder, ATR 은 True Range.
- 재시작해도 손절선·타이머·세션 누적값을 MySQL 에서 복원.
- 선행 바스켓 최근 5분 변화율을 `signal_tracking.csv` 의 `leader_mom` 에 기록 (`LEADER_MOMENTUM_GATE=True` 로 판정에 반영).
