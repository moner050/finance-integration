"""시나리오 이름·발동 조건 시드 — 홈의 SOXX 연말 시나리오 4개.

여기 있는 건 각 경로를 뭐라 부르고 무엇이 그 경로를 만드는지, 두 가지뿐이다(사용자의 분석 프레임).
**가격대와 기본 확률은 넣지 않는다** — SOXX 자신의 수익률 분포에서 매일 다시 계산한다 (worker.job_bands → scoring.make_bands).
캘린더(FOMC·BOJ·일본 CPI·실적)와 지표도 전부 calendar_sources.py·sources.py 가 주기적으로 받아 온다.
"""

SCENARIOS = [
    {"code": "S1", "name": "멀티플 재확장", "sort": 1,
     "trigger_text": "유가 하락 → 10월 FOMC 동결 → 12월 점도표 완화 + 실적 상향 지속"},
    {"code": "S2", "name": "박스권", "sort": 2,
     "trigger_text": "10월 클러스터 무난 통과, 피크 논쟁이 상단·실적이 하단"},
    {"code": "S3", "name": "10월 클러스터 쇼크", "sort": 3,
     "trigger_text": "Fed 추가 인상 + BOJ 매파 전망보고서 48시간 이중 타격 + 엔 캐리 청산"},
    {"code": "S4", "name": "피크아웃 확정", "sort": 4,
     "trigger_text": "Q4 메모리 정점 확인 + 하이퍼스케일러 캐펙스 가이던스 하향"},
]
