"""Binance 무기한 선물 추종 알림 — 급등 추종 롱 / 급락 추종 숏 (상위 봉, 사양별).

run_binance.py 가 5분봉 급락 매수 워커와 같은 프로세스에서 사양(config.FOLLOW_SPECS)마다 워커 하나씩 돌린다.
데이터·지표 도우미는 binance_crash 를 재사용한다.

판정 — 2026-09-15 스윙 분석 「스윙의 급등과 급락」에서 우위가 확인된 'M2' 계열 트리거:
  급등 추종 롱  룩백 저점 대비 상승폭 ≥ 기준 ATR × 배수, RSI14 ≥ 70 이 처음 성립한 봉에 📈 관찰(review).
                그 급등 봉 뒤 N봉 안에 종가가 EMA9 아래로 눌렸다가 다시 위로 마감하면 🔵 진입 후보(action).
  급락 추종 숏  룩백 고점 대비 하락폭 ≥ 기준 ATR × 배수, RSI14 ≤ 30 이 처음 성립한 봉에 📉 관찰(review).
                그 급락 봉 뒤 N봉 안에 종가가 EMA9 위로 반등했다가 다시 아래로 마감하면 🔴 숏 후보(action).
  국면          급등 추종은 일봉 종가가 EMA200 위(강세), 급락 추종은 아래(약세)일 때만 — 반대 국면 표본은 기대값이 음수였다.
'급변 뒤 n봉'은 눌림·반등 이전의 마지막 급변 봉 기준이고, 눌림 저점·반등 고점은 그 뒤 봉들의 극값이다.
기준 ATR = 직전 base_bars 봉 ATR14% 의 중앙값. 재알림 간격 = 보유 한도.
손절 참고선은 사양의 stop 규칙(2026-09-15 레버리지 분석, 마크 가격 봉 재생): 4시간봉 눌림 저점 - 2.5 기준ATR, 일봉 롱 진입 -10%,
일봉 숏 진입 +25%. 목표 지정가는 두지 않는다 — 두면 세 사양 모두 평균이 내려갔다. 관찰 단계에는 규칙과 현재 기준 ATR 만 싣는다.
급등 숏·급락 매수(상위 봉)는 만들지 않는다 — 분석상 손실이거나 일봉 청산 바닥에 한정된다.
"""

import logging
from datetime import datetime, timedelta, timezone
from statistics import median

from .binance_crash import KST, atr_pct_series, fetch_funding, fetch_klines, fmt_price
from .config import FOLLOW_KLINES, SURGE_FUNDING_WARN
from .indicators import compute_ema, compute_rsi
from .models import Signal

log = logging.getLogger("binance")

BAR_HOURS = {"1h": 1, "4h": 4, "1d": 24}
RVOL_WINDOW = 60


def base_atr_pct(bars: list, base_bars: int) -> float:
    """직전 base_bars 봉 ATR% 중앙값 (마지막 봉 제외). 표본이 절반도 안 되면 0 — 판정을 보류한다."""
    series = atr_pct_series(bars)[:-1][-base_bars:]
    if len(series) < base_bars // 2:
        return 0.0
    return median(series)


def move_metrics(bars: list, base: float, spec: dict):
    """마지막 봉의 급변 지표. 롱 사양은 룩백 저점 대비 상승폭, 숏 사양은 룩백 고점 대비 하락폭(양수)."""
    lb = spec["lookback"]
    if len(bars) < lb + 16:
        return None
    sig, long = bars[-1], spec["side"] == "long"
    if long:
        ref = min(b["low"] for b in bars[-1 - lb:-1])
        move = (sig["close"] / ref - 1) * 100
    else:
        ref = max(b["high"] for b in bars[-1 - lb:-1])
        move = (1 - sig["close"] / ref) * 100
    _, rsi = compute_rsi([{"closePrice": b["close"]} for b in bars])
    mult = move / base
    rsi_ok = rsi >= spec["rsi"] if long else rsi <= spec["rsi"]
    return {"ref": ref, "move": move, "mult": mult, "rsi": rsi, "hit": mult >= spec["atr_mult"] and rsi_ok}


def regime(daily_bars: list):
    """(강세 여부, 마지막 완성 일봉 종가, EMA200). 일봉이 200개 미만이면 None."""
    closes = [b["close"] for b in daily_bars or []]
    if len(closes) < 200:
        return None
    ema = compute_ema(closes, 200)
    return closes[-1] > ema, closes[-1], ema


def stop_line(spec: dict, close: float, pull: float, base: float) -> float:
    """손절 참고선. ('pull_atr', m) 은 눌림 저점/반등 고점 ∓ m 기준ATR, ('pct', p) 는 진입(신호 종가) ∓ p%."""
    kind, v = spec["stop"]
    sgn = 1 if spec["side"] == "long" else -1
    return pull * (1 - sgn * v * base / 100) if kind == "pull_atr" else close * (1 - sgn * v / 100)


def stop_text(spec: dict) -> str:
    """메시지·시작 알림용 손절 규칙 설명."""
    kind, v = spec["stop"]
    long = spec["side"] == "long"
    if kind == "pull_atr":
        return f"{'눌림 저점' if long else '반등 고점'} {'-' if long else '+'}{v:g} 기준ATR"
    return f"진입 {'-' if long else '+'}{v:g}%"


def evaluate(bars: list, spec: dict, regime_bars: list = None, funding=None):
    """마지막 완성봉에서 관찰(watch)·진입(entry) 판정. 해당 없으면 None.

    국면은 판정하지 않고 값만 싣는다 — 보류 여부는 워커가 spec["regime"] 으로 정한다.
    일봉 사양은 regime_bars 를 안 주면 자기 봉으로 국면을 잰다.
    """
    base = base_atr_pct(bars, spec["base_bars"])
    if base <= 0:
        return None
    now, prev = move_metrics(bars, base, spec), move_metrics(bars[:-1], base, spec)
    if now is None or prev is None:
        return None
    long = spec["side"] == "long"
    closes = [b["close"] for b in bars]
    ema_now, ema_prev = compute_ema(closes, 9), compute_ema(closes[:-1], 9)
    crossed = ((closes[-1] > ema_now and closes[-2] <= ema_prev) if long
               else (closes[-1] < ema_now and closes[-2] >= ema_prev))
    stage, ago = None, 0
    if crossed:                                                   # EMA9 재돌파(롱) / 재이탈(숏) 봉
        n = spec["reentry_bars"]

        def against(k):                                           # k 봉 전이 눌림(롱: EMA9 이하)/반등(숏: 이상) 상태인가
            e = compute_ema(closes[:len(closes) - k], 9)
            return closes[-1 - k] <= e if long else closes[-1 - k] >= e

        run = 1                                                   # 직전 봉부터 이어진 눌림/반등 봉 수
        while run < n and against(run + 1):
            run += 1
        for k in range(run + 1, n + 1):                           # 눌림/반등 이전의 마지막 급변 봉을 찾는다
            m = move_metrics(bars[:-k], base, spec)
            if m and m["hit"]:
                stage, ago = "entry", k
                break
    if stage is None and now["hit"] and not prev["hit"]:        # 급변이 처음 성립한 봉
        stage = "watch"
    if stage is None:
        return None
    sig = bars[-1]
    vol_med = median(b["volume"] for b in bars[-1 - RVOL_WINDOW:-1])
    after = bars[-ago:] if ago else bars[-1:]
    pull = min(x["low"] for x in after) if long else max(x["high"] for x in after)       # 눌림 저점 / 반등 고점
    out = {
        "stage": stage, "side": spec["side"], "open_time": sig["open_time"], "close": sig["close"],
        "ref": now["ref"], "move": now["move"], "mult": now["mult"], "rsi": now["rsi"], "base": base, "ema9": ema_now,
        "rvol": sig["volume"] / vol_med if vol_med > 0 else 0.0, "ago": ago,
        "pull": pull, "stop": stop_line(spec, sig["close"], pull, base) if stage == "entry" else None,   # 관찰 단계엔 손절 없음
        "funding": funding,
    }
    reg = regime(regime_bars if regime_bars is not None else (bars if spec["interval"] == "1d" else None))
    if reg is not None:
        out["bull"], out["daily_close"], out["ema200"] = reg
    return out


def build_signal(symbol: str, r: dict, spec: dict) -> Signal:
    long = r["side"] == "long"
    when = datetime.fromtimestamp(r["open_time"] / 1000, tz=timezone.utc).astimezone(KST)
    lines = [f"{fmt_price(r['close'])} ({when:%m-%d %H:%M} KST 봉) · {spec['lookback']}봉 {'저점' if long else '고점'} "
             f"{fmt_price(r['ref'])} 대비 {'+' if long else '-'}{r['move']:.2f}% (기준ATR {r['mult']:.1f}배) · "
             f"RSI14 {r['rsi']:.1f} · RVOL {r['rvol']:.1f}배"]
    if r["stage"] == "entry":
        pull = (r["pull"] / r["close"] - 1) * 100
        lines.append(f"{'급등' if long else '급락'} 뒤 {r['ago']}봉 만에 EMA9 {fmt_price(r['ema9'])} "
                     f"{'재돌파' if long else '재이탈'} · {'눌림 저점' if long else '반등 고점'} {fmt_price(r['pull'])} ({pull:+.2f}%)")
    else:
        lines.append(f"EMA9 {fmt_price(r['ema9'])} · {'눌림 뒤 재돌파를' if long else '반등 뒤 재이탈을'} "
                     f"{spec['reentry_bars']}봉 안에 기다린다")
    if "bull" in r:
        reg = f"국면 {'강세' if r['bull'] else '약세'} (일봉 {fmt_price(r['daily_close'])} / EMA200 {fmt_price(r['ema200'])})"
    else:
        reg = "국면 불명 (일봉 부족)"
    if r.get("funding") is not None:
        reg += f" · 펀딩 {r['funding'] * 100:+.4f}%/8h"
        if long and r["funding"] > SURGE_FUNDING_WARN:
            reg += " ⚠ 롱 과열 — 크기 축소"
        elif not long and r["funding"] < -SURGE_FUNDING_WARN:
            reg += " ⚠ 숏 과밀 — 크기 축소"
    lines.append(reg)
    hold_days = spec["hold_bars"] * BAR_HOURS[spec["interval"]] / 24
    if r["stage"] == "entry":
        lines.append(f"참고: 손절 {fmt_price(r['stop'])} ({stop_text(spec)}) · 보유 한도 {hold_days:g}일 · 목표 지정가 없음")
    else:
        lines.append(f"참고: 진입 후보가 뜨면 손절 = {stop_text(spec)} (지금 기준ATR {r['base']:.2f}%) · 보유 한도 {hold_days:g}일")
    watch_kind, entry_kind = spec["kinds"]
    if r["stage"] == "entry":
        title = "🔵 눌림 재돌파 — 추종 진입 후보" if long else "🔴 반등 실패 — 추종 숏 후보"
        return Signal(entry_kind, title, f"{symbol} {spec['label']}", "\n".join(lines), symbol)
    title = "📈 급등 확인 — 추종 관찰" if long else "📉 급락 확인 — 추종 관찰"
    return Signal(watch_kind, title, f"{symbol} {spec['label']}", "\n".join(lines), symbol)


class FollowWorker:
    """사양 하나. 심볼별로 새 완성봉이 생길 때마다 한 번 판정하고, 단계별로 보유 한도 동안 한 번만 알린다."""

    def __init__(self, spec: dict, notifier, fetch_bars=fetch_klines, fetch_fund=fetch_funding, trader=None):
        self.spec = spec
        self.trader = trader        # binance_trade.DryTrader — 진입 후보(entry)만 가상 체결한다
        self.notify = notifier
        self.fetch_bars = fetch_bars
        self.fetch_fund = fetch_fund
        self.last_bar = {}          # symbol -> 마지막으로 판정한 완성봉 open_time
        self.last_alert = {}        # (symbol, stage) -> 마지막 알림 시각
        self.status = {}            # symbol -> 마지막 완성봉의 급변 지표·국면 (시황 요약용)

    def _status(self, bars: list, reg_bars: list):
        base = base_atr_pct(bars, self.spec["base_bars"])
        m = move_metrics(bars, base, self.spec) if base > 0 else None
        if m is None:
            return None
        reg = regime(reg_bars)
        return {**m, "close": bars[-1]["close"], "bull": reg[0] if reg else None}

    def status_lines(self, now: datetime = None) -> list:
        """시황 요약 한 줄씩. 급변 조건이 어디까지 찼는지, 관찰(눌림·반등 대기) 중인지, 국면이 맞는지."""
        now = now or datetime.now(timezone.utc)
        spec, long, out = self.spec, self.spec["side"] == "long", []
        head = f"{spec['name']} {spec['label']}"
        for symbol in spec["symbols"]:
            m = self.status.get(symbol)
            if not m:
                out.append(f"{head} {symbol}  데이터 부족")
                continue
            reg = "국면 불명" if m["bull"] is None else ("강세" if m["bull"] else "약세")
            want = spec.get("regime")
            if want and m["bull"] is not None and m["bull"] != (want == "bull"):
                tail = f"국면 불일치 — 보류 (필요: {'강세' if want == 'bull' else '약세'})"
            else:
                watched = self.last_alert.get((symbol, "watch"))
                window = timedelta(hours=BAR_HOURS[spec["interval"]] * spec["reentry_bars"])
                if watched and now - watched < window:
                    tail = f"관찰 중 — {'눌림 뒤 EMA9 재돌파' if long else '반등 뒤 EMA9 재이탈'} 대기"
                elif m["hit"]:
                    tail = f"{'급등' if long else '급락'} 성립 — 다음 봉부터 {'눌림' if long else '반등'} 확인"
                else:
                    rsi_ok = m["rsi"] >= spec["rsi"] if long else m["rsi"] <= spec["rsi"]
                    miss = [n for n, ok in (("변동폭", m["mult"] >= spec["atr_mult"]), ("RSI", rsi_ok)) if not ok]
                    tail = f"{'급등' if long else '급락'} 조건 {2 - len(miss)}/2 (부족: {', '.join(miss)})"
            signed = m["move"] if long else -m["move"]               # 롱은 저점 대비 상승(+), 숏은 고점 대비 하락(-)
            out.append(f"{head} {symbol}  {fmt_price(m['close'])} · {spec['lookback']}봉 {'저점' if long else '고점'} 대비 "
                       f"{signed:+.2f}% (기준ATR {max(m['mult'], 0):.1f}/{spec['atr_mult']:g}배) · "
                       f"RSI {m['rsi']:.1f} · {reg} | {tail}")
        return out

    def poll_once(self, now: datetime = None) -> list:
        now = now or datetime.now(timezone.utc)
        spec, sent = self.spec, []
        for symbol in spec["symbols"]:
            bars = self.fetch_bars(symbol, spec["interval"], FOLLOW_KLINES)
            if not bars or bars[-1]["open_time"] == self.last_bar.get(symbol):
                continue
            self.last_bar[symbol] = bars[-1]["open_time"]
            reg_bars = bars if spec["interval"] == "1d" else self.fetch_bars(symbol, "1d", FOLLOW_KLINES)
            self.status[symbol] = self._status(bars, reg_bars)
            result = evaluate(bars, spec, reg_bars)
            if result is None:
                continue
            want = spec.get("regime")
            if want and result.get("bull") is not None and result["bull"] != (want == "bull"):
                log.info("%s %s %s(%s)이지만 일봉 EMA200 %s라 보류", symbol, spec["label"], spec["name"], result["stage"],
                         "아래" if want == "bull" else "위")
                continue
            key = (symbol, result["stage"])
            last = self.last_alert.get(key)
            if last and now - last < timedelta(hours=BAR_HOURS[spec["interval"]] * spec["hold_bars"]):
                log.info("%s %s %s 조건 충족했지만 쿨다운 중 (마지막 알림 %s)", symbol, spec["label"], result["stage"],
                         last.isoformat(timespec="minutes"))
                continue
            result["funding"] = self.fetch_fund(symbol)
            signal = build_signal(symbol, result, spec)
            self.notify.send(signal)
            self.last_alert[key] = now
            sent.append(signal)
            if self.trader is not None and result["stage"] == "entry":
                try:
                    self.trader.on_entry(spec["kinds"][1], symbol, spec["side"], result,
                                         spec["hold_bars"] * BAR_HOURS[spec["interval"]], now)
                except Exception as e:          # 자동매매 오류가 알림을 막으면 안 된다
                    log.warning("%s 자동매매 진입 처리 실패: %s", symbol, e)
        return sent
