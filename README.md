# 단타 알림 시스템 (토스증권 Open API · 알림 전용)

토스증권 읽기 전용 API 로 1분봉·현재가·보유를 30초마다 폴링하고, VWAP·RVOL·선행 바스켓
조건으로 매수/손절/익절 신호를 만들어 **텔레그램·WhatsApp** 으로 보낸다. 주문은 내지 않는다.
감시 종목은 **백오피스 화면**에서 지정하고, 엔진은 재시작 없이 다음 사이클에 반영한다.

```
alertbot/
  config.py        .env 로드, 임계값 상수, 초기 종목(SEED_WATCHLIST)
  timeutil.py      거래소 현지시각 변환
  toss_client.py   토스증권 읽기 전용 클라이언트 (토큰·캔들·현재가·보유·캘린더)
  market_hours.py  개장/휴장/조기폐장 판정 (캘린더 API, 실패 시 고정 시간)
  indicators.py    RVOL·VWAP(세션 누적)·EMA·RSI(Wilder)·ATR(True Range) 순수 함수
  engine.py        신호 엔진: 스냅샷 → 상태기계 → 알림, 시황 요약, 워치리스트 핫리로드, 상태 영속
  tracking.py      신호 추적·거래 기록 CSV
  models.py        Signal (종류·등급·쿨다운)
  notify/          Dispatcher(쿨다운·라우팅·이력) + telegram / whatsapp 채널
  db.py            MySQL 저장소 (alert_watchlist / alert_engine_status / alert_signal_log)
  backoffice/      FastAPI + Jinja2 + HTMX 화면 (상태 · 종목 · 신호 이력 · 채널)
run_engine.py      엔진 워커 진입점
run_backoffice.py  백오피스 진입점 (기본 http://127.0.0.1:8000)
tests/             pytest (지표 골든값, 엔진 시나리오, 알림, DB, 백오피스)
```

## 실행

```bash
pip install -r requirements.txt
```

프로젝트 루트 `.env` (따옴표·공백 없이):

```
TOSS_CLIENT_ID=...            TOSS_CLIENT_SECRET=...          # WTS > 설정 > Open API, 허용 IP 등록 필요
TELEGRAM_BOT_TOKEN=...        TELEGRAM_CHAT_ID=111,222        # 수신자는 봇에게 먼저 /start
TELEGRAM_MIN_SEVERITY=info                                    # info | review | action
WHATSAPP_ACCESS_TOKEN=...     WHATSAPP_PHONE_NUMBER_ID=...    # Meta Cloud API
WHATSAPP_TO=+8210...,+8210...                                 # E.164, 쉼표 구분
WHATSAPP_TEMPLATE=trade_alert WHATSAPP_TEMPLATE_LANG=ko       # 승인 전엔 hello_world / en_US
WHATSAPP_MIN_SEVERITY=review                                  # 행동 + 검토 알림만 (건당 과금)
MYSQL_HOST=... MYSQL_PORT=3306 MYSQL_DATABASE=... MYSQL_USER=... MYSQL_PASSWORD=...
ALERT_BACKOFFICE_HOST=127.0.0.1  ALERT_BACKOFFICE_PORT=8000
```

```bash
python -m alertbot.db init      # MySQL 에 alert_* 테이블 생성 (엔진·백오피스가 자동으로도 만든다)
python -m alertbot.db seed      # 기존 9종목 시딩 (엔진도 목록이 비어 있으면 자동 시딩)
python run_engine.py            # 콘솔 1
python run_backoffice.py        # 콘솔 2 → http://127.0.0.1:8000
python -m pytest                # 테스트
```

## 알림 등급과 채널

| 등급 | 알림 | 쿨다운 | 기본 수신 |
|---|---|---|---|
| action | 🔵 매수 · 🔴 손절/매도 · 🟢 익절 · 🔵 추가매수 · 🟠 마감 정리 | 15분 | 텔레그램 + WhatsApp |
| review | 🟡 일부 익절 검토(45분) · ⚪ 매수 취소 · 청산 완료 | 45분/15분 | 텔레그램 + WhatsApp |
| info | 📊 시황(30분) · 🔔🔕 장 시작/마감 · 📈 성적 · 시스템 | 없음 | 텔레그램만 |

쿨다운 키는 (신호 종류, 종목)이다. 채널 하나가 실패해도 다른 채널은 보내며, 결과는
`alert_signal_log` 에 남고 백오피스 '신호 이력'에서 본다.

## WhatsApp (Meta Cloud API) 제약

- 수신자가 24시간 안에 먼저 보낸 적이 없으면 **승인된 템플릿**으로만 보낼 수 있다 → 템플릿만 쓴다.
- 템플릿 파라미터 값에 개행이 올 수 없다 → 본문 줄을 ` · ` 로 이어 한 파라미터에 넣는다.
- 템플릿 `trade_alert` (Utility, 한국어) 본문: `{{1}} | {{2}}` 다음 줄 `{{3}}`.
- 준비 순서와 테스트 발송 버튼은 백오피스 '채널' 화면에 있다.

## 백오피스

- **상태**: 엔진 heartbeat(90초 넘으면 경고), 종목별 상태·현재가·VWAP·RVOL·정점·선행·손절선. 30초 자동 갱신.
- **종목**: 추가/편집/중지/삭제, 토스 현재가 API 로 심볼 검증, 페어 정합성 경고. 저장 즉시 엔진이 다음 사이클에 반영.
- **신호 이력**: 종목·등급 필터, 채널별 전송 결과.
- **채널**: 설정 상태와 테스트 발송.

## 감지기 변경 요약 (원본 대비)

- 세션 VWAP·정점 RVOL 을 120봉 창이 아니라 **정규장 봉 누적**으로 (개장 2시간 뒤 기준선 표류 제거).
- 이동평균 RVOL 기준선에서 프리마켓·시간외 봉 제외. 전일 종가는 날짜로 선택.
- 매수 판정은 신호봉 **종가** 기준, 손절·이탈은 현재가 기준.
- 캘린더 API 로 휴장·조기폐장 반영. RSI 는 Wilder, ATR 은 True Range.
- 재시작해도 손절선·타이머·세션 누적값이 MySQL 에서 복원된다.
- 선행 바스켓의 최근 5분 변화율을 `signal_tracking.csv` 의 `leader_mom` 에 기록한다
  (`LEADER_MOMENTUM_GATE=True` 로 켜면 방향 판정에 반영).
