"""시나리오 기본값 시드 — 홈의 SOXX 연말 시나리오 4개.

확률·가격대는 사용자의 분석 프레임이라 수집할 곳이 없다. 처음 한 번만 넣고, 그 뒤엔 '매크로 관리' 에서 고친다.
캘린더(FOMC·BOJ·일본 CPI·실적)와 지표는 전부 calendar_sources.py·sources.py 가 주기적으로 받아 온다.
"""

SCENARIOS = [
    {"code": "S1", "name": "멀티플 재확장", "sort": 1, "soxx_low": 580, "soxx_high": 650, "base_prob": 25,
     "trigger_text": "유가 하락 → 10월 FOMC 동결 → 12월 점도표 완화 + 실적 상향 지속"},
    {"code": "S2", "name": "박스권", "sort": 2, "soxx_low": 470, "soxx_high": 560, "base_prob": 35,
     "trigger_text": "10월 클러스터 무난 통과, 피크 논쟁이 상단·실적이 하단"},
    {"code": "S3", "name": "10월 클러스터 쇼크", "sort": 3, "soxx_low": 380, "soxx_high": 450, "base_prob": 25,
     "trigger_text": "Fed 추가 인상 + BOJ 매파 전망보고서 48시간 이중 타격 + 엔 캐리 청산"},
    {"code": "S4", "name": "피크아웃 확정", "sort": 4, "soxx_low": 300, "soxx_high": 380, "base_prob": 15,
     "trigger_text": "Q4 메모리 정점 확인 + 하이퍼스케일러 캐펙스 가이던스 하향"},
]
