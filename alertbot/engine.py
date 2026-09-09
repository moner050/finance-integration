"""신호 엔진 — 스냅샷 → 상태기계(관망/진입대기/보유/청산대기) → 알림, 시황 요약, 메인 루프.

주요 설계 결정
------------
- 지표는 '완성된 봉'으로만 계산한다. 마지막 봉은 진행 중이라 거래량이 부분값이다.
- 보유 조회가 실패하면 그 사이클은 판단을 보류한다. 보유 여부를 모르면
  ENTRY(미보유 전제)와 STOP(보유 전제) 어느 쪽도 신뢰할 수 없다.
"""

import logging
import time
from datetime import datetime, timedelta, timezone

from .config import (ADDON_MAX_COUNT, ADDON_MIN_PROFIT_PCT, BASE_DIR, CLOSE_WARN_MIN,
                     ENABLE_ADD_ON, ENABLE_AMBIGUOUS, ENABLE_EXIT_SIGNAL, ENABLE_TRACKING,
                     ENTRY_MIN_PEAK_RATIO, EXIT_GRACE_MIN, EXIT_PORTION_HALF, EXIT_PORTION_STRONG,
                     EXIT_PORTION_THIRD, FADE_MIN_PEAK, FADE_STRONG_RATIO, FADE_WEAK_RATIO,
                     LEADER_GAP_PCT, POLL_INTERVAL_SEC, PROFILE_PAGES, REENTRY_BLOCK_MIN,
                     RVOL_TRIGGER, RVOL_WINDOW, STATS_REPORT_MIN, STOP_LOSS_PCT,
                     SUMMARY_INTERVAL_MIN, TRACK_FILE, TRADE_FILE, VWAP_BAND_PCT)
from .indicators import (build_volume_profile, compute_rsi, compute_rvol, compute_vwap,
                         effective_band, ema_alignment, session_peak_rvol, strong_bar,
                         vwap_position)
from .notify.dispatcher import Notifier
from .timeutil import now_local, parse_ts, to_local
from .toss_client import TossReadOnlyClient
from .tracking import SignalTracker, TradeLog

log = logging.getLogger("scalper")


class SignalEngine:
    def __init__(self, client: TossReadOnlyClient, notifier: Notifier, watch_holdings: bool,
                 watchlist: dict):
        self.client = client
        self.notify = notifier
        self.watch_holdings = watch_holdings
        self.watchlist = watchlist      # symbol -> 설정 dict (market/leaders/inverse/pair/hold_only/name/note)
        self.last_bar = {}              # ticker -> 마지막으로 평가한 완성봉 timestamp
        self.prev_close = {}
        self.prev_close_date = None
        self.volume_profile = {}        # ticker -> {HH:MM: [거래량...]}
        self.profile_date = {}          # ticker -> 구축한 세션 날짜. 시장별로 세션이 달라 종목별로 관리
        self.us_close = None
        self.us_close_date = None
        self.stats = {}
        self.last_report = datetime.now(timezone.utc)
        self.tracker = (SignalTracker(BASE_DIR / TRACK_FILE)
                        if ENABLE_TRACKING else None)
        self.trades = TradeLog(BASE_DIR / TRADE_FILE)
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

    @property
    def tickers(self) -> list:
        return list(self.watchlist.keys())

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
                     self.watchlist[t].get("name") or t, s.get("tick", 0), s.get("direction", 0),
                     s.get("rvol", 0), s.get("vwap", 0), s.get("all", 0))

    # -- 일 1회 갱신 ---------------------------------------------------------
    def refresh_volume_profile(self, tickers: list):
        """종목별로 세션당 한 번 프로파일을 만든다.

        캐시를 시장 공용으로 두면 한국장에서 만든 날 미국장이 열릴 때
        '오늘 이미 만들었다'로 판단해 미국 종목이 프로파일 없이 돌아간다.
        """
        for t in tickers:
            market = self.watchlist[t]["market"]
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
        cfg = self.watchlist[ticker]
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
        label = self.watchlist[ticker].get("name") or ticker

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
        clock = now_local(self.watchlist[ref]["market"]).strftime("%H:%M")
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
        log.info("알림 전용 모드 시작 (주문 없음). 대상=%s", self.tickers)
        # 직전 사이클에 열려 있던 시장. 열고 닫힐 때 알림을 보내
        # '조용한 이유'를 사람이 알 수 있게 한다.
        prev_open = set()
        while True:
            open_now = [t for t in self.tickers if self.market_open(self.watchlist[t]["market"])]
            # hold_only 종목(3배 레버리지)은 보유 중일 때만 감시한다.
            # 안 들고 있으면 조회할 이유가 없다 — 매수 신호는 SOXX 가 낸다.
            held_syms = set(self._last_holdings.keys())
            active = [t for t in open_now
                      if not self.watchlist[t].get("hold_only") or t in held_syms]
            # 프리마켓 종목은 시황에만 쓴다. 알림 판단에는 넣지 않는다.
            pre = [t for t in self.tickers
                   if t not in active and self.market_premarket(self.watchlist[t]["market"])]
            now_open = {self.watchlist[t]["market"] for t in active}

            for m in now_open - prev_open:
                names = [self.watchlist[t].get("name") or t
                         for t in self.tickers if self.watchlist[t]["market"] == m]
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
            leaders = sorted({s for t in watch for s in (self.watchlist[t].get("leaders") or [])})
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


def log_timestamp_sample(client: TossReadOnlyClient, watchlist: dict):
    """기동 시 실제 타임스탬프 형식을 한 번 보여준다. UTC 가정이 맞는지 사람이 확인한다."""
    for t in watchlist:
        candles = client.get_candles(t, count=1)
        if candles:
            raw = candles[-1].get("timestamp")
            local = parse_ts(raw, watchlist[t]["market"])
            log.info("타임스탬프 확인 — %s 원본: %s → 현지 해석: %s", t, raw,
                     local.strftime("%Y-%m-%d %H:%M %Z") if local else "해석 실패")
            return
