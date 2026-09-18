"""매크로 점수 — 지표 등급(색)과 SOXX 시나리오 확률 보정. 순수 함수만 둔다(DB·네트워크 없음).

등급 0 안정 · 1 주의 · 2 경계 · 3 위험 은 'SOXX 에 얼마나 역풍인가' 다. 값이 오르고 내리는 방향과는 따로 본다
(화면의 ▲▼ 는 중립색, 색은 등급에만).
- 금리·달러: 3년 롤링 백분위(85/95 → 주의/경계, 최대 경계)·절대 하한·5영업일 변화 속도 중 큰 쪽.
  임계는 stock-market-monitor app/scoring/components.py 의 표를 옮겼다.
- USD/JPY: 엔 급강세(하락)가 캐리 청산 위험이다 — 수준 145/142 이하, 5일 −0.5/−1.0/−1.5% 이하.

시나리오 보정 (분석 프레임, 예측·자문 아님):
  logit_i = ln(base_i) + K · Σ_j  w_j · s_j · A_j[i],   p = softmax
  s_j ∈ [−1, +1] 은 SOXX 에 우호(+)·비우호(−), A_j 는 그 신호가 +일 때 시나리오별로 미는 방향, w_j 는 가중치.
  신호는 켜고 끄는 값이 아니라 임계 사이를 잇는 연속값이다 — 임계를 살짝 넘었다고 확률이 왈칵 쏠리지 않는다.
  값이 없는 지표는 빠진다(미반영). 이벤트 결과 플래그는 기본 확률을 저장한 뒤에 난 것만 센다 — 기본값이 이미 알고 있던 일을 두 번 세지 않게.
"""

import math
from datetime import date

GRADE_LABEL = ("안정", "주의", "경계", "위험")
K = 0.30

# 절대 하한 {등급: 이 값 이상} — 금리(%)
LEVEL_FLOORS = {
    "ust_2y": {2: 5.0, 3: 5.5},
    "ust_10y": {1: 4.75, 2: 5.10, 3: 5.40},
    "ust_30y": {1: 5.00, 2: 5.25, 3: 5.60},
    "jgb_2y": {1: 1.50, 2: 2.00, 3: 2.80},
    "jgb_10y": {1: 2.00, 2: 2.80, 3: 3.50},
    "jgb_30y": {1: 3.50, 2: 4.20, 3: 5.00},
}
# 5영업일 변화(bp) {등급: 상승 이상 / 하락 이상}
MOVE_BP = {
    "ust_2y": ((10, 20, 35), (15, 25, 40)),
    "ust_10y": ((10, 20, 35), (15, 28, 45)),
    "ust_30y": ((12, 25, 40), (18, 32, 50)),
    "jgb_2y": ((5, 12, 25), (10, 20, 35)),
    "jgb_10y": ((6, 15, 30), (12, 25, 45)),
    "jgb_30y": ((8, 20, 35), (15, 30, 50)),
}
PCT_KEYS = ("ust_2y", "ust_10y", "ust_30y", "jgb_2y", "jgb_10y", "jgb_30y", "dxy")


def pct_rank(history: list, value: float) -> float:
    """history 안에서 value 이하인 비율(%)."""
    if not history:
        return None
    return 100.0 * sum(1 for h in history if h <= value) / len(history)


def pct_grade(p) -> int:
    if p is None:
        return 0
    return 3 if p >= 99 else 2 if p >= 95 else 1 if p >= 85 else 0


def floor_grade(key: str, value: float) -> int:
    g = 0
    for grade, th in LEVEL_FLOORS.get(key, {}).items():
        if value >= th:
            g = max(g, grade)
    return g


def move_grade(key: str, bp5: float) -> int:
    if bp5 is None or key not in MOVE_BP:
        return 0
    ups, downs = MOVE_BP[key]
    ths = ups if bp5 >= 0 else downs
    return sum(1 for th in ths if abs(bp5) >= th)


def change(points: list, n: int):
    """points = [값...] 오름차순. n 관측 전 대비 차이 (없으면 None)."""
    if len(points) <= n:
        return None
    return points[-1] - points[-1 - n]


def grade_series(key: str, values: list) -> dict:
    """values: 3년치 값 오름차순. 반환 {grade, reasons, pct, d1, d5}."""
    if not values:
        return {"grade": None, "reasons": [], "pct": None, "d1": None, "d5": None}
    v = values[-1]
    d1, d5 = change(values, 1), change(values, 5)
    reasons, grade = [], 0
    pct = pct_rank(values[:-1], v) if key in PCT_KEYS and len(values) > 60 else None
    if pct is not None and pct_grade(pct):
        grade = max(grade, min(2, pct_grade(pct)))      # 백분위만으로는 '경계'까지 — '위험'은 절대 수준·속도가 정한다
        reasons.append("3년 최고권" if pct >= 99.5 else f"3년 상위 {max(1, round(100 - pct))}%")
    if key in LEVEL_FLOORS and floor_grade(key, v):
        grade = max(grade, floor_grade(key, v))
        reasons.append(f"절대 수준 {v:.2f}%")
    if key in MOVE_BP and d5 is not None:
        g = move_grade(key, d5 * 100)
        if g:
            grade = max(grade, g)
            reasons.append(f"5일 {d5 * 100:+.0f}bp")
    if key == "dxy" and len(values) > 5:
        p5 = (v / values[-6] - 1) * 100
        g = 2 if p5 >= 1.5 else 1 if p5 >= 1.0 else 0
        if g:
            grade = max(grade, g)
            reasons.append(f"5일 {p5:+.1f}%")
    if key == "usdjpy":
        g = 3 if v <= 142 else 2 if v <= 145 else 0
        if g:
            grade = max(grade, g)
            reasons.append(f"엔 강세 {v:.1f}")
        if v >= 160:
            grade = max(grade, 1)
            reasons.append("개입 경계 160↑")
        if len(values) > 5:
            p5 = (v / values[-6] - 1) * 100
            g = 3 if p5 <= -1.5 else 2 if p5 <= -1.0 else 1 if p5 <= -0.5 else 0
            if g:
                grade = max(grade, g)
                reasons.append(f"5일 {p5:+.1f}% 엔 급강세")
    return {"grade": grade, "reasons": reasons, "pct": pct, "d1": d1, "d5": d5}


def curve_state(spread: float, history: list) -> dict:
    """장단기 스프레드(%p). 역전은 경고색이 아니라 정보색 — 역전이 풀리는 순간이 더 위험하다."""
    if spread is None:
        return {"label": "-", "tone": "muted"}
    if spread < 0:
        return {"label": "역전", "tone": "info"}
    if history and min(history[-120:]) < 0:
        return {"label": "역전 해소", "tone": "warn"}
    if spread < 0.25:
        return {"label": "평탄", "tone": "muted"}
    return {"label": "정상", "tone": "ok"}


def carry_grade(spread: float) -> int:
    """미·일 10년 금리차(%p) — 좁아질수록 엔 캐리 여유가 줄어든다."""
    if spread is None:
        return None
    return 3 if spread <= 1.50 else 2 if spread <= 1.80 else 1 if spread <= 2.20 else 0


# -- 시나리오 --------------------------------------------------------------------------

INDICATORS = {   # A = 신호가 +1(SOXX 우호)일 때 시나리오별 방향
    "us_core_cpi": {"label": "미 근원 CPI (전년비)", "w": 1.0, "A": {"S1": 1, "S2": 0.3, "S3": -1, "S4": -0.3},
                    "bull": "≤ 2.2%", "bear": "≥ 2.7%"},
    "jp_core_cpi": {"label": "일본 근원 CPI (전년비)", "w": 1.0, "A": {"S1": 0.5, "S2": 0.5, "S3": -1, "S4": 0},
                    "bull": "< 2.0% 지속", "bear": "≥ 2.5%"},
    "eps_rev": {"label": "반도체 EPS 추정치", "w": 2.0, "A": {"S1": 1, "S2": 0.3, "S3": -0.3, "S4": -1},
                "bull": "계속 상향", "bear": "상향 중단 = 최초 경고"},
    "soxx_spy": {"label": "SOXX/SPY 상대강도", "w": 1.0, "A": {"S1": 1, "S2": 0.3, "S3": -0.5, "S4": -0.8},
                 "bull": "50일선 위 · 20일 상승", "bear": "52주 상대 저점 붕괴"},
    "dram": {"label": "DRAM 현물가", "w": 2.0, "A": {"S1": 0.5, "S2": 0.3, "S3": 0, "S4": -1},
             "bull": "강세 지속", "bear": "하락 전환 = S4 확정"},
    "fomc": {"label": "FOMC 결과 (기준일 이후)", "w": 1.0, "A": {"S1": 1, "S2": 0.3, "S3": -1, "S4": 0},
             "bull": "인하·완화", "bear": "추가 인상·매파"},
    "boj": {"label": "BOJ 결과 (기준일 이후)", "w": 1.0, "A": {"S1": 0.5, "S2": 0.3, "S3": -1, "S4": 0},
            "bull": "동결·완화", "bear": "인상·매파 전망"},
}
FLAG_SIGNAL = {"hike": -1, "hawkish": -1, "hold": 0, "cut": 1, "dovish": 1}
EPS_FULL = 5.0      # 30일 +5% 면 '상향' 신호가 최대
EPS_STALL = 0.3     # 상향이 멈췄을 때의 경고분
EPS_DEAD = 0.5      # ±이 안이면 사실상 제자리 = 상향 중단
DRAM_FULL = 10.0    # 30일 ±10% 면 방향 신호가 최대
DIR_LABEL = {1: "S1 방향", 0: "중립", -1: "S3/S4 방향"}


def yoy(points: list):
    """월간 지수 [{'d','v'}] → (최신 전년비 %, 기준월). 12개월 전 같은 달이 없으면 None."""
    if not points:
        return None, None
    last = points[-1]
    target = f"{int(last['d'][:4]) - 1}{last['d'][4:7]}"
    prev = next((p for p in points if p["d"][:7] == target), None)
    if not prev:
        return None, last["d"]
    return (last["v"] / prev["v"] - 1) * 100, last["d"]


def ramp(v, bull: float, bear: float):
    """bull 이하면 +1, bear 이상이면 −1, 사이는 직선. (물가처럼 낮을수록 우호적인 지표)"""
    if v is None:
        return None
    mid, half = (bull + bear) / 2, (bear - bull) / 2
    return max(-1.0, min(1.0, (mid - v) / half)) if half else 0.0


def signal_us_cpi(v):
    """미 근원 CPI 전년비 — 2.2% 이하면 S1 방향, 2.7% 이상이면 S3/S4 방향."""
    return ramp(v, 2.2, 2.7)


def signal_jp_cpi(v):
    """일본 근원 CPI 전년비 — 2.0% 미만이면 완화 여지, 2.5% 이상이면 인상 압력."""
    return ramp(v, 2.0, 2.5)


def signal_eps(pct):
    """반도체 EPS 추정치 30일 변화율(%). 멈춘 구간(±0.5%)은 '상향 중단' 경고라 약한 음수로 둔다."""
    if pct is None:
        return None
    if pct >= EPS_DEAD:
        return min(1.0, pct / EPS_FULL)
    if pct > -EPS_DEAD:
        return -EPS_STALL
    return max(-1.0, pct / EPS_FULL - EPS_STALL)


def signal_dram(pct):
    """DRAM 현물가 30일 변화율(%). 하락 전환이 S4 의 확정 신호다."""
    if pct is None:
        return None
    return max(-1.0, min(1.0, pct / DRAM_FULL))


def soxx_spy_state(soxx: list, spy: list) -> dict:
    """soxx·spy = [{'d','v'}]. 날짜가 겹치는 종가로 비율을 만든다."""
    spy_by = {p["d"]: p["v"] for p in spy}
    ratio = [(p["d"], p["v"] / spy_by[p["d"]]) for p in soxx if spy_by.get(p["d"])]
    if len(ratio) < 60:
        return {"signal": None, "ratio": None, "text": "데이터 부족"}
    vals = [r for _, r in ratio]
    now = vals[-1]
    ma50 = sum(vals[-50:]) / 50
    slope20 = now / vals[-21] - 1
    prior = vals[-253:-1]
    low = min(prior)
    if now <= low * 1.005:
        s, text = -1, "52주 상대 저점권"
    elif now > ma50 and slope20 > 0:
        s, text = 1, "50일선 위 · 20일 상승"
    else:
        s, text = 0, "50일선 " + ("위" if now > ma50 else "아래") + f" · 20일 {slope20 * 100:+.1f}%"
    return {"signal": s, "ratio": now, "ma50": ma50, "slope20": slope20, "text": text, "series": vals}


def latest_flag(events: list, kind: str, since: str, today: str):
    """기준일(since) 이후 ~ 오늘까지 난 kind 이벤트 중 플래그가 있는 마지막 것."""
    hits = [e for e in events if e["kind"] == kind and e.get("flag") and since < e["event_date"] <= today]
    return hits[-1] if hits else None


def softmax_probs(base: dict, terms: dict) -> dict:
    """base {S: %}, terms {지표: (w·s, A)} → {S: %}."""
    if not base:
        return {}
    logits = {}
    for code, b in base.items():
        z = math.log(max(b, 0.1))
        for ws, A in terms.values():
            z += K * ws * A.get(code, 0)
        logits[code] = z
    m = max(logits.values())
    ex = {c: math.exp(z - m) for c, z in logits.items()}
    tot = sum(ex.values())
    return {c: 100 * e / tot for c, e in ex.items()}


def adjust(scenarios: list, signals: dict) -> dict:
    """scenarios = list_scenarios 행, signals = {지표: s 또는 None}.

    반환 {base, adjusted, top, contributions:[{key,label,scenario,delta}], ev} — 확률은 소수 첫째 자리.
    """
    base = {s["code"]: s["base_prob"] for s in scenarios}
    terms = {k: (INDICATORS[k]["w"] * s, INDICATORS[k]["A"]) for k, s in signals.items() if s is not None and k in INDICATORS}
    adjusted = softmax_probs(base, terms)
    base_norm = softmax_probs(base, {})
    contribs = []
    for k in terms:
        without = softmax_probs(base, {kk: v for kk, v in terms.items() if kk != k})
        code = max(adjusted, key=lambda c: abs(adjusted[c] - without[c]))
        delta = adjusted[code] - without[code]
        if abs(delta) >= 0.05:
            contribs.append({"key": k, "label": INDICATORS[k]["label"], "scenario": code, "delta": delta, "signal": signals[k]})
    contribs.sort(key=lambda c: -abs(c["delta"]))
    ev = sum(adjusted[s["code"]] / 100 * (s["soxx_low"] + s["soxx_high"]) / 2 for s in scenarios) if scenarios else None
    top = max(adjusted, key=adjusted.get) if adjusted else None
    return {"base": {c: round(v, 1) for c, v in base_norm.items()}, "adjusted": {c: round(v, 1) for c, v in adjusted.items()},
            "top": top, "contributions": contribs, "ev": ev}


def d_day(event_date: str, today: date) -> int:
    return (date.fromisoformat(event_date) - today).days
