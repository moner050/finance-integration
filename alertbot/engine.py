"""신호 엔진 — 스냅샷 → 상태기계(관망/진입대기/보유/청산대기) → 알림, 시황 요약, 메인 루프.

주요 설계 결정
------------
- 지표는 '완성된 봉'으로만 계산한다. 마지막 봉은 진행 중이라 거래량이 부분값이다.
- 보유 조회가 실패하면 그 사이클은 판단을 보류한다. 보유 여부를 모르면
  ENTRY(미보유 전제)와 STOP(보유 전제) 어느 쪽도 신뢰할 수 없다.
"""

import logging
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from . import db
from .config import (ADDON_MAX_COUNT, ADDON_MIN_PROFIT_PCT, ALERT_COOLDOWN_MIN, CLOSE_WARN_MIN, DATA_DIR,
                     ENABLE_ADD_ON, ENABLE_AMBIGUOUS, ENABLE_EXIT_SIGNAL, ENABLE_TRACKING,
                     ENTRY_MIN_PEAK_RATIO, ENTRY_PENDING_MAX_MIN, ENTRY_SKIP_BEAR_EMA, EXIT_GRACE_MIN,
                     EXIT_PORTION_HALF, EXIT_PORTION_STRONG, EXIT_PORTION_THIRD, EXIT_REPEAT_MAX_MIN,
                     FADE_BARS, FADE_MIN_PEAK, FADE_STRONG_RATIO, FADE_WEAK_RATIO, LEADER_GAP_PCT,
                     LEADER_MOMENTUM_GATE, LEADER_MOMENTUM_MIN, OPEN_EXCLUDE_MIN, POLL_INTERVAL_SEC,
                     PROFILE_PAGES, REENTRY_BLOCK_MIN, RVOL_TRIGGER, RVOL_WINDOW, STATS_REPORT_MIN,
                     STOP_LOSS_PCT, STOP_RECOVER_PCT, SUMMARY_INTERVAL_MIN, TRACK_FILE, TRADE_FILE,
                     TRAIL_MIN_PROFIT_PCT, VWAP_BAND_PCT)
from .indicators import (SessionState, bar_minutes_from_open, build_volume_profile, compute_rsi,
                         compute_rvol, effective_band, ema_alignment, is_regular_bar, rvol_at, strong_bar,
                         vwap_position)
from .market_hours import MarketHours
from .models import Signal
from .notify.dispatcher import Dispatcher
from .timeutil import now_local, parse_ts, to_local
from .toss_client import TossReadOnlyClient
from .tracking import SignalTracker, TradeLog

log = logging.getLogger("scalper")


class SignalEngine:
    def __init__(self, client: TossReadOnlyClient, notifier: Dispatcher, watch_holdings: bool,
                 watchlist: dict, store=None, executor=None):
        self.client = client
        self.notify = notifier
        self.watch_holdings = watch_holdings
        self.watchlist = watchlist      # symbol -> 설정 dict (market/leaders/inverse/pair/hold_only/name/note)
        self.store = store              # db.DB. None 이면 핫리로드·상태 영속 없이 돈다 (테스트)
        self.executor = executor        # trading.Executor. AUTOTRADE_MODE=off 면 None — 알림만
        self.watch_version = None       # 마지막으로 읽은 워치리스트 버전
        self.last_bar = {}              # ticker -> 마지막으로 평가한 완성봉 timestamp
        self.prev_close = {}
        self.prev_close_date = {}       # symbol -> 전일 종가를 받은 현지 세션 날짜
        self.volume_profile = {}        # ticker -> {HH:MM: [거래량...]}
        self.profile_date = {}          # ticker -> 구축한 세션 날짜. 시장별로 세션이 달라 종목별로 관리
        self.hours = MarketHours(client)    # 개장/휴장/마감 시각 (캘린더 캐시)
        self.sessions = {}              # ticker -> SessionState (당일 VWAP·정점 RVOL 누적)
        self.price_hist = {}            # symbol -> deque[(utc, price)] 선행 모멘텀용
        self.stats = {}
        self.last_report = datetime.now(timezone.utc)
        self.tracker = (SignalTracker(DATA_DIR / TRACK_FILE)
                        if ENABLE_TRACKING else None)
        self.trades = TradeLog(DATA_DIR / TRADE_FILE)
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
        self._holdings_now = {}     # 이번 evaluate 에 넘어온 보유. 실행기가 매도 수량·평단을 여기서 본다
        # 청산 직전 마지막으로 관측한 평단·시세. 마감 메시지의 손익 추정에 쓴다.
        self.last_seen = {}
        # 매수 신호봉의 저점. 이게 구조적 손절선이다.
        # 기준선 밴드 대신 이 선을 쓰면 '사자마자 살짝 눌림'에 안 흔들린다.
        self.stop_ref = {}
        self.stop_src = {}          # ticker -> 손절선의 출처 문구 (매수 신호봉 / 보유 확인 봉 / 추가매수 봉)
        self.snapshots = {}         # 최근 지표. 시황 요약이 재계산 없이 쓴다
        # 기동 직후 첫 시황이 바로 나가도록 과거 시각으로 초기화한다.
        # 30분을 기다리면 '돌고 있는 건지' 확인이 늦어진다.
        self.last_summary = datetime.now(timezone.utc) - timedelta(minutes=SUMMARY_INTERVAL_MIN)
        if store is not None:
            self._restore_state()
        if executor is not None:
            executor.hours = self.hours     # 정규장 판정을 엔진과 같은 캘린더로

    @property
    def tickers(self) -> list:
        return list(self.watchlist.keys())

    # -- 알림 ---------------------------------------------------------------
    def _emit(self, kind: str, title: str, label: str, symbol, body: str, account: str = None):
        """알림 한 건. 채널 선택·쿨다운·이력 기록은 Dispatcher 가 맡는다. 발송 결과를 돌려준다 (억제되면 None).

        body 는 시장 근거, account 는 손익·평단·보유 수량처럼 내 계좌에서만 나오는 줄이다 — 공개 채널에는 body 만 간다.
        자동매매가 켜져 있으면 실제로 발송된 신호만 실행기에도 넘긴다. 쿨다운에 억제된 반복까지 넘기면
        진입대기·청산대기 동안 30초마다 주문 의도가 생긴다. 실행기 오류가 알림을 막으면 안 되므로
        알림을 먼저 보내고, 실행기 예외는 잡아서 로그만 남긴다.
        """
        signal = Signal(kind, title, label, body, symbol, account)
        sent = self.notify.send(signal)
        if sent is not None and self.executor is not None and symbol:
            try:
                self.executor.on_signal(signal, self.snapshots.get(symbol), self._holdings_now)
            except Exception as e:
                log.exception("자동매매 실행기 오류 (%s %s): %s", kind, symbol, e)
        return sent

    def _reconcile_orders(self):
        """미결 주문 추적. 체결된 매도의 실제 평균가를 청산 메시지에 쓰도록 남긴다."""
        if self.executor is None:
            return
        try:
            for symbol, qty, avg in self.executor.reconcile():
                if symbol in self.last_seen:
                    self.last_seen[symbol].update({"price": avg, "actual": True})
        except Exception as e:
            log.exception("자동매매 미결 추적 오류: %s", e)

    # -- 워치리스트 핫리로드 · 상태 영속 ----------------------------------------
    def _reload_watchlist(self):
        """백오피스가 바꾼 목록을 재시작 없이 반영한다. 버전이 바뀐 사이클에만 다시 읽는다."""
        if self.store is None:
            return
        try:
            version = db.watchlist_version(self.store)
            if version == self.watch_version:
                return
            new = db.load_watchlist(self.store)
        except Exception as e:
            log.warning("워치리스트 조회 실패 — 이전 목록 유지: %s", e)
            return
        added = sorted(set(new) - set(self.watchlist))
        removed = sorted(set(self.watchlist) - set(new))
        changed = sorted(t for t in set(new) & set(self.watchlist) if new[t] != self.watchlist[t])
        for t in removed:
            self._forget(t)
        for t in changed:
            if new[t]["market"] != self.watchlist[t]["market"]:
                self._forget(t)                 # 시장이 바뀌면 세션·프로파일이 다른 시간대다
        self.watchlist = new
        self.watch_version = version
        if added or removed or changed:
            log.info("감시 목록 변경 — 추가 %s / 제거 %s / 수정 %s → 현재 %s", added, removed, changed, self.tickers)

    def _forget(self, ticker: str):
        """감시에서 빠진 종목의 상태를 지운다. 보유 중이면 손절 알림이 더는 나가지 않으니 경고한다."""
        if ticker in self._last_holdings:
            log.warning("%s 보유 중인데 감시 목록에서 빠졌다 — 손절·매도 알림이 나가지 않는다", ticker)
        for d in (self.state, self.pending, self.stop_ref, self.stop_src, self.entry_at, self.exit_at, self.addon_count,
                  self.last_seen, self.last_bar, self.sessions, self.volume_profile, self.profile_date,
                  self.snapshots, self.stats):
            d.pop(ticker, None)

    _SNAP_KEYS = ("label", "market", "price", "close", "vwap", "band", "pos", "pos_close", "prev_rvol",
                  "rvol", "rvol_method", "peak", "strength", "direction_ok", "momentum", "strong")

    def _save_status(self, active: list, pre: list, error: str = None):
        """사이클마다 heartbeat·종목 상태·지표를 저장한다. 백오피스가 읽고, 재시작 때 복원한다."""
        if self.store is None:
            return
        tickers = set(self.state) | set(self.sessions) | set(self.stop_ref) | set(self.pending)
        state = {}
        for t in tickers:
            item = {
                "state": self.state.get(t, "관망"),
                "pending": self.pending.get(t),
                "stop_ref": self.stop_ref.get(t),
                "stop_src": self.stop_src.get(t),
                "entry_at": self.entry_at[t].isoformat() if t in self.entry_at else None,
                "exit_at": self.exit_at[t].isoformat() if t in self.exit_at else None,
                "addon_count": self.addon_count.get(t, 0),
                "last_seen": self.last_seen.get(t),
                "last_bar": self.last_bar.get(t),
                "session": self.sessions[t].to_dict() if t in self.sessions else None,
            }
            state[t] = item
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        snaps = {t: {k: s.get(k) for k in self._SNAP_KEYS} | {"at": now}
                 for t, s in self.snapshots.items()}
        try:
            db.save_engine_status(self.store, active, pre, state, snaps, error)
        except Exception as e:                  # DB 장애가 감시를 멈추면 안 된다
            log.warning("엔진 상태 저장 실패: %s", e)

    def _restore_state(self):
        """재시작 직후 이전 상태를 되살린다.

        없으면 _sync_state 가 기존 보유를 '방금 매수'로 오인해 익절 유예를 다시 주고,
        신호봉 저점 손절선(stop_ref)이 사라져 매도선이 밴드로 바뀌고, 불타기 횟수가 리셋된다.
        오래된 타이머는 그대로 두어도 된다 — 유예·재진입 차단은 시간이 지나면 자연히 풀린다.
        """
        try:
            saved = db.load_engine_status(self.store)
        except Exception as e:
            log.warning("엔진 상태 복원 실패: %s", e)
            return
        if not saved or not saved.get("state"):
            return
        restored = []
        for t, item in saved["state"].items():
            if t not in self.watchlist:
                continue
            self.state[t] = item.get("state", "관망")
            if item.get("pending"):
                self.pending[t] = item["pending"]
            if item.get("stop_ref"):
                self.stop_ref[t] = float(item["stop_ref"])
            if item.get("stop_src"):
                self.stop_src[t] = item["stop_src"]
            for key, target in (("entry_at", self.entry_at), ("exit_at", self.exit_at)):
                if item.get(key):
                    try:
                        target[t] = datetime.fromisoformat(item[key])
                    except ValueError:
                        pass
            if item.get("addon_count"):
                self.addon_count[t] = int(item["addon_count"])
            if item.get("last_seen"):
                self.last_seen[t] = item["last_seen"]
            if item.get("last_bar"):
                self.last_bar[t] = item["last_bar"]
            if item.get("session"):
                self.sessions[t] = SessionState.from_dict(self.watchlist[t]["market"], item["session"])
            restored.append(t)
        if restored:
            log.info("엔진 상태 복원 (%s 기준): %s", saved.get("heartbeat_at"), restored)

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
                    # 같은 이력으로 오늘 세션(VWAP·정점)을 백필한다. 추가 호출 없이 끝난다.
                    ss = self._session(t)
                    added = ss.update(self._completed(candles, market), self.volume_profile[t])
                    log.info("세션 백필 %s: %d봉 (VWAP %s, 정점 %s배)", t, added, ss.vwap, ss.peak)
            except Exception as e:
                log.warning("프로파일 %s 실패(이동평균 대체): %s", t, e)
            self.profile_date[t] = session

    def refresh_prev_closes(self, symbols: dict):
        """symbols: symbol -> market. 시장별 현지 세션 날짜가 바뀌면 다시 받는다.

        전일 종가는 '오늘 세션보다 앞선 날짜의 마지막 일봉'으로 고른다. daily[-2] 처럼
        위치로 고르면 오늘 일봉이 아직 없는 프리마켓에 그저께 종가가 잡힌다.
        """
        for s, market in symbols.items():
            session = now_local(market).strftime("%Y-%m-%d")
            if self.prev_close_date.get(s) == session and s in self.prev_close:
                continue
            prev = None
            for c in self.client.get_candles(s, interval="1d", count=5):
                dt = parse_ts(c.get("timestamp"), market)
                if dt is None or dt.strftime("%Y-%m-%d") >= session:
                    continue
                try:
                    prev = float(c["closePrice"])
                except (KeyError, TypeError, ValueError):
                    continue
            if prev is not None:
                self.prev_close[s] = prev
            self.prev_close_date[s] = session

    def leader_strength(self, leaders: list, prices: dict) -> float:
        ch = []
        for s in leaders:
            cur, prev = prices.get(s), self.prev_close.get(s)
            if cur and prev and prev > 0:
                ch.append((cur - prev) / prev * 100)
        return round(sum(ch) / len(ch), 2) if ch else 0.0

    def _record_prices(self, prices: dict):
        """현재가 이력. 선행 모멘텀(LEADER_MOMENTUM_MIN 분 전 대비)을 구하는 데 쓴다."""
        now = datetime.now(timezone.utc)
        keep = timedelta(minutes=LEADER_MOMENTUM_MIN + 5)
        for s, p in prices.items():
            hist = self.price_hist.setdefault(s, deque())
            hist.append((now, p))
            while hist and now - hist[0][0] > keep:
                hist.popleft()

    def leader_momentum(self, leaders: list, prices: dict):
        """선행 바스켓의 최근 LEADER_MOMENTUM_MIN 분 평균 변화율(%). 이력이 모자라면 None."""
        if LEADER_MOMENTUM_MIN <= 0:
            return None
        now = datetime.now(timezone.utc)
        horizon = timedelta(minutes=LEADER_MOMENTUM_MIN)
        ch = []
        for s in leaders:
            cur = prices.get(s)
            past = [p for t, p in self.price_hist.get(s, ()) if now - t >= horizon]
            if cur and past and past[-1] > 0:
                ch.append((cur - past[-1]) / past[-1] * 100)
        return round(sum(ch) / len(ch), 2) if ch else None

    @staticmethod
    def _completed(candles: list, market: str) -> list:
        """진행 중인 마지막 봉을 뺀다. 거래량이 부분값이라 RVOL 이 낮게 나오고,
        같은 봉이 폴링마다 다른 값으로 재평가된다."""
        if not candles:
            return candles
        last_dt = parse_ts(candles[-1].get("timestamp"), market)
        cur_min = now_local(market).replace(second=0, microsecond=0)
        if last_dt is not None and last_dt >= cur_min:
            return candles[:-1]
        return candles

    def _session(self, ticker: str) -> SessionState:
        ss = self.sessions.get(ticker)
        if ss is None:
            ss = self.sessions[ticker] = SessionState(self.watchlist[ticker]["market"])
        return ss

    # -- 종목별 판단 -----------------------------------------------------------
    def _snapshot(self, ticker, prices):
        """지표를 한 번에 계산해 돌려준다. 판단과 시황 요약이 같은 값을 쓰도록."""
        cfg = self.watchlist[ticker]
        market = cfg["market"]
        candles = self.client.get_candles(ticker)
        if not candles:
            return None

        candles = self._completed(candles, market)
        if len(candles) < RVOL_WINDOW + 2:
            return None

        try:
            close = float(candles[-1]["closePrice"])
        except (KeyError, TypeError, ValueError):
            return None
        price = prices.get(ticker)
        if price is None or price <= 0:
            price = close

        prof = self.volume_profile.get(ticker)
        prev_rvol, rvol, rvol_method = compute_rvol(candles, market, prof)
        # 세션 누적값: 새 완성봉만 더해진다. VWAP 과 정점은 120봉 창이 아니라 세션 전체다.
        ss = self._session(ticker)
        ss.update(candles, prof)
        vwap = ss.vwap
        band = effective_band(candles)

        leaders = cfg.get("leaders") or []
        if leaders:
            strength = self.leader_strength(leaders, prices)
            direction_ok = (strength <= -LEADER_GAP_PCT if cfg["inverse"]
                            else strength >= LEADER_GAP_PCT)
            momentum = self.leader_momentum(leaders, prices)
            if LEADER_MOMENTUM_GATE and direction_ok and momentum is not None:
                # 전일 대비로는 올랐어도 최근 몇 분 흘러내리는 중이면 방향을 인정하지 않는다
                direction_ok = momentum <= 0 if cfg["inverse"] else momentum >= 0
        else:
            strength, direction_ok, momentum = None, True, None

        strong, regular = strong_bar(candles[-1]), is_regular_bar(candles[-1], market)
        # 거래량 돌파: 직전봉 아래 → 현재봉 위로 교차. 매수와 불타기가 같은 기준을 쓴다 —
        # 정규장 강봉이어야 하고, 오늘 정점이 있었다면 그 근처여야 '새 추세'다.
        breakout = rvol_method != "혼합" and prev_rvol < RVOL_TRIGGER <= rvol
        if breakout and ss.peak >= FADE_MIN_PEAK and rvol < ss.peak * ENTRY_MIN_PEAK_RATIO:
            breakout = False
        # 소진 판정용 최근 거래량: 1분봉 하나는 시끄러워 조용한 1분에 '전량 정리' 가 뜬다. 최근 몇 봉 평균을 쓴다.
        recent = [rvol_at(candles, i, market, prof) for i in range(len(candles) - FADE_BARS, len(candles))]
        recent = [r for r, m in recent if m != "부족"] or [rvol]
        rvol_recent = round(sum(recent) / len(recent), 2)

        return {
            "cfg": cfg, "market": market, "label": cfg.get("name") or ticker,
            "candles": candles, "bar_key": candles[-1].get("timestamp"),
            "price": price, "close": close, "vwap": vwap, "band": band,
            "pos": vwap_position(price, vwap, band),
            # 매수 판정은 신호봉 종가로 한다. 현재가는 봉 중간값이라 종가가 기준선 아래인
            # 봉에서도 순간 위로 튈 수 있고, 같은 봉은 다시 판정하지 않아 되돌릴 수 없다.
            "pos_close": vwap_position(close, vwap, band),
            "last_low": float(candles[-1].get("lowPrice") or 0),
            "strong": strong, "regular": regular,
            "since_open": bar_minutes_from_open(candles[-1], market),
            "prev_rvol": prev_rvol, "rvol": rvol, "rvol_method": rvol_method,
            "rvol_recent": rvol_recent, "breakout": breakout and strong and regular,
            "peak": ss.peak,
            "strength": strength, "direction_ok": direction_ok, "momentum": momentum,
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
            if ticker not in self.stop_ref:
                # 우리 신호 없이 산 포지션. 밴드 하단을 곧바로 매도선으로 쓰면 '사자마자 팔라' 가 되므로
                # 신호봉 저점과 같은 역할을 보유를 처음 확인한 봉의 저점에 맡긴다.
                low = self.snapshots.get(ticker, {}).get("last_low", 0)
                if low > 0:
                    self.stop_ref[ticker], self.stop_src[ticker] = low, "보유 확인 봉 저점"
        elif not has_pos and st in ("보유", "청산대기"):
            st = "관망"                     # 청산 실행됨
            self.exit_at[ticker] = datetime.now(timezone.utc)
            self.addon_count.pop(ticker, None)
            self.pending.pop(ticker, None)
            self.stop_ref.pop(ticker, None)
            self.stop_src.pop(ticker, None)
            self._closing_note(ticker)
        self.state[ticker] = st
        return st

    def evaluate(self, ticker: str, prices: dict, holdings: dict):
        self._holdings_now = holdings
        snap = self._snapshot(ticker, prices)
        if snap is None:
            return
        self.snapshots[ticker] = snap

        held = holdings.get(ticker)
        has_pos = bool(held and held["qty"] > 0)
        st = self._sync_state(ticker, has_pos)

        label, market = snap["label"], snap["market"]
        price, vwap, pos, pos_close = snap["price"], snap["vwap"], snap["pos"], snap["pos_close"]
        rvol, prev_rvol, peak = snap["rvol"], snap["prev_rvol"], snap["peak"]

        # ---- 보유 중 / 청산 대기 ----
        if st in ("보유", "청산대기"):
            # 청산 뒤에는 보유 정보가 사라지므로, 매 사이클 마지막 값을 남겨둔다
            self.last_seen[ticker] = {"avg": held["avg"], "price": price, "qty": held["qty"]}
            ctx = {"ema": ema_alignment(snap["candles"]), "rsi": compute_rsi(snap["candles"])}
            self._check_holding(ticker, label, market, price, vwap, pos,
                                rvol, prev_rvol, snap["breakout"], held, ctx, peak)
            return

        # ---- 진입 대기: 아직 안 샀다. 조건이 살아있으면 다시 알린다 ----
        if st == "진입대기":
            sig = self.pending.get(ticker, {})
            # 돌파봉의 근거는 오래가지 않는다. 한 시간 뒤의 '매수하세요' 는 늦은 진입이고,
            # 정규장이 끝난 뒤의 반복은 시간외 가격을 보고 하는 말이다.
            at = sig.get("at")
            stale = bool(at) and (datetime.now(timezone.utc) - datetime.fromisoformat(at)
                                  >= timedelta(minutes=ENTRY_PENDING_MAX_MIN))
            if pos_close == "below":
                # 근거가 무너졌으면 기다릴 이유가 없다. 판정은 완성봉 종가 — 현재가 틱 하나로 취소하면
                # 매수 알림 2분 뒤 취소, 반복 알림 33초 뒤 취소 같은 왕복이 생긴다.
                self.state[ticker] = "관망"
                self.pending.pop(ticker, None)
                self._emit("ENTRY_CANCEL", "⚪ 매수 취소", label, ticker,
                                 f"종가 {snap['close']}가 기준선 {vwap} 아래로 내려감\n"
                                 f"진입 근거 소멸 — 관망으로 전환")
            elif stale or not snap["regular"]:
                self.state[ticker] = "관망"
                self.pending.pop(ticker, None)
                why = f"{ENTRY_PENDING_MAX_MIN}분 안에 진입하지 않음" if stale else "정규장 종료"
                self._emit("ENTRY_CANCEL", "⚪ 매수 신호 만료", label, ticker,
                                 f"신호가 {sig.get('price', price)} → 현재가 {price}\n"
                                 f"{why} — 관망으로 전환")
            else:
                snap["signal_bar"] = sig.get("bar_key")     # 실행기는 원래 신호봉으로 중복을 판단한다
                self._emit("ENTRY", "🔵 매수하세요", label, ticker,
                                 f"현재가 {price}  (신호가 {sig.get('price', price)})\n"
                                 f"거래량 {rvol}배, 기준선 {vwap} 위 유지\n"
                                 f"아직 미진입 — 조건 유지 중")
            return

        # ---- 관망: 매수 판단 ----
        if snap["cfg"].get("hold_only"):
            return          # 3배 상품은 매수 신호를 내지 않는다
        if not snap["regular"]:
            # 매수 신호는 정규장 봉에서만. 마감 뒤 10분 여유 구간이나 프리마켓의 시간외 봉은
            # 유동성이 얇아 거래량 배수가 튀고, 기준선(정규장 VWAP)과 비교할 대상도 아니다.
            return
        if snap["since_open"] < OPEN_EXCLUDE_MIN:
            # 세션 VWAP 이 봉 한두 개로 만들어진 구간. '기준선 위' 가 자기 봉 typical price 와의
            # 비교가 되어 강봉 필터와 다를 게 없다. 정점 계산과 같은 개장 구간을 뺀다.
            return
        if self.hours.near_close(market):
            # 마감 CLOSE_WARN_MIN 분 안의 진입은 곧바로 '마감 전 정리' 를 받는 자기모순이다.
            return
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
        if pos_close == "above":
            self.bump(ticker, "vwap")

        # 거래량이 터져도 긴 윗꼬리에 종가가 아래면 매수세가 밀린 봉이다
        if rvol_breakout and not snap["strong"]:
            log.debug("%s 돌파했으나 신호봉이 약함(윗꼬리) — 보류", ticker)
            rvol_breakout = False
        # 역배열(EMA 9<20<50)에서의 돌파는 하락 추세 속 반등이다. 추적된 신호 중 역배열·혼조 진입이
        # 모두 음수였다 (표본이 작아 ENTRY_SKIP_BEAR_EMA 로 켜고 끈다).
        align = ema_alignment(snap["candles"])
        if rvol_breakout and ENTRY_SKIP_BEAR_EMA and align == "역배열":
            log.debug("%s 돌파했으나 EMA 역배열 — 보류", ticker)
            rvol_breakout = False

        if direction_ok and rvol_breakout and pos_close == "above":
            self.bump(ticker, "all")
            self.stop_ref[ticker], self.stop_src[ticker] = snap["last_low"], "매수 신호봉 저점"
            rsi_prev, rsi_now = compute_rsi(snap["candles"])
            slope = "상승" if rsi_now > rsi_prev else "하락"
            self.state[ticker] = "진입대기"
            self.pending[ticker] = {"price": price, "at": datetime.now(timezone.utc).isoformat(),
                                    "bar_key": snap["bar_key"]}
            note = snap["cfg"].get("note")
            self._emit("ENTRY", "🔵 매수하세요", label, ticker,
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
                    "leader_mom": snap["momentum"] if snap["momentum"] is not None else "",
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

    @staticmethod
    def _exit_pending(kind: str, level: str, why: str) -> dict:
        """청산대기 정보. 최초 사유를 고정하고, 첫 반복은 강한 알림 쿨다운 뒤에 온다."""
        nxt = datetime.now(timezone.utc) + timedelta(minutes=ALERT_COOLDOWN_MIN)
        return {"kind": kind, "level": level, "why": why, "repeats": 0, "next_at": nxt.isoformat()}

    @staticmethod
    def _exit_recovered(first: dict, pnl: float, price: float, pos_now: str, sell_line: float, band: float) -> str:
        """청산대기의 근거가 사라졌으면 그 설명을, 아니면 빈 문자열.

        경계에서 왔다갔다하지 않게 여유를 둔다: 손절은 한도에서 STOP_RECOVER_PCT 넘게 회복,
        매도선은 밴드만큼 위. 거래량 소진(EXIT_FULL)은 가격이 기준선 위로 올라서야 철회한다 —
        그 자리였으면 애초에 발동하지 않았다(holding_up).
        """
        if pnl <= STOP_LOSS_PCT + STOP_RECOVER_PCT:
            return ""
        floor = round(sell_line * (1 + band / 100), 4) if sell_line > 0 else 0
        if floor and price < floor:
            return ""
        kind = first.get("kind")
        if kind == "EXIT_FULL":
            return "기준선 위로 복귀 — 거래량 소진 판단 철회" if pos_now == "above" else ""
        if kind == "STOP":
            return f"손절 한도 {STOP_LOSS_PCT}% 에서 {STOP_RECOVER_PCT}% 넘게 회복 (매도선 {sell_line} 위)"
        return f"매도선 {sell_line} 위로 회복 (밴드 {band}% 여유 포함)"

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
        body += ("\n\n※ 자동매매 체결가 기준" if seen.get("actual")
                 else "\n\n※ 실제 체결가는 다를 수 있음 (추정치)")
        self._emit("CLOSED", head, label, ticker, body)
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
        snap = self.snapshots.get(ticker, {})
        close, band = snap.get("close", price), snap.get("band", VWAP_BAND_PCT)
        if not snap.get("regular", True) and pnl > STOP_LOSS_PCT:
            # 정규장 봉이 아니면(마감 뒤 여유 구간, 시간외) 손절 한도 외의 판단은 하지 않는다.
            # 시간외 가격은 얇고, 기준선(정규장 VWAP)과 비교할 대상도 아니다.
            return

        # 매도 기준선. 신호봉(또는 보유를 처음 확인한 봉) 저점, 없으면 기준선 밴드.
        # 신호봉 저점은 '이 봉이 무너지면 진입 근거가 깨진 것'이라는 뜻이라
        # 밴드보다 종목 상황에 밀착돼 있다. 사자마자 살짝 눌리는 정도로는 안 걸린다.
        # 판정은 완성봉 종가로 한다 — 현재가 틱 하나로 팔라고 하면 3배 상품 꼬리에 청산대기가 걸린다.
        stop_ref = self.stop_ref.get(ticker, 0)
        band_low = round(vwap * (1 - band / 100), 4) if vwap > 0 else 0
        if stop_ref > 0:
            sell_line, sell_why = stop_ref, self.stop_src.get(ticker, "기준봉 저점")
            if pnl >= TRAIL_MIN_PROFIT_PCT and band_low > stop_ref:
                # 수익이 붙었고 기준선이 저점 위로 올라왔으면 매도선도 따라 올린다. 첫 저점에 두면
                # 불타기로 커진 물량까지 처음 위험으로 되돌아간다.
                sell_line, sell_why = band_low, f"기준선 밴드 하단 (VWAP -{band}%, 저점 {stop_ref} 에서 상향)"
        else:
            sell_line, sell_why = band_low, f"기준선 밴드 하단 (VWAP -{band}%)"
        broke = sell_line > 0 and close < sell_line

        # 이미 청산 신호를 낸 상태면 사유를 바꾸지 않는다.
        # 매 사이클 조건을 다시 평가하면 '매도하세요 → 익절하세요 → 1/3 익절'처럼
        # 같은 행동(팔아라)에 다른 이름표가 붙어 헷갈린다. 최초 사유를 고정하고
        # 손익만 갱신해 반복한다. 단, 손절 한도(-5%)는 더 급한 상황이라 갈아탄다.
        if self.state.get(ticker) == "청산대기":
            first = self.pending.setdefault(ticker, {})
            # 근거가 사라졌으면 보유로 돌아간다. 포지션이 없어질 때까지 반복하면
            # 가격이 회복한 뒤에도 '이탈했다'고 계속 말하는 꼴이다.
            recovered = self._exit_recovered(first, pnl, close, snap.get("pos_close", pos_now), sell_line, band)
            if recovered:
                self.state[ticker] = "보유"
                self.pending.pop(ticker, None)
                self._emit("EXIT_CANCEL", "⚪ 청산 신호 해제", label, ticker,
                                 f"{recovered}\n"
                                 f"청산 근거 소멸 — 보유로 전환",
                                 account=f"손익 {pnl}%  (평단 {avg} → 현재 {price})")
                return
            if pnl <= STOP_LOSS_PCT and first.get("kind") != "STOP":
                first = self.pending[ticker] = self._exit_pending("STOP", "🔴 손절하세요",
                                                                  f"손절 한도 {STOP_LOSS_PCT}% 도달")
            # 같은 사유를 15분마다 종일 반복하면 진짜 위험 알림이 묻힌다.
            # 반복할수록 간격을 두 배로 늘린다 (15→30→60→…, 상한 EXIT_REPEAT_MAX_MIN).
            now = datetime.now(timezone.utc)
            due = first.get("next_at")
            if due and now < datetime.fromisoformat(due):
                return
            n = first.get("repeats", 0) + 1
            gap = min(ALERT_COOLDOWN_MIN * 2 ** n, EXIT_REPEAT_MAX_MIN)
            sent = self._emit(first.get("kind", "SELL"), first.get("level", "🔴 매도하세요"), label, ticker,
                              f"{first.get('why', '청산 신호 유지')} — 청산 신호 유지 중\n"
                              f"다음 알림 {gap}분 뒤",
                              account=f"손익 {pnl}%  (평단 {avg} → 현재 {price}) · 아직 미청산, {held['qty']:g}주 보유 중")
            if sent is not None:            # 알림기 쿨다운에 걸렸으면 다음 사이클에 다시 시도한다
                first["repeats"], first["next_at"] = n, (now + timedelta(minutes=gap)).isoformat()
            return

        # 거래량 소진도: 세션 정점 대비 현재 비율.
        # 정점이 충분히 높아야(FADE_MIN_PEAK) '한 번 터진 추세'로 인정한다.
        # 애초에 거래가 안 붙었던 종목에는 소진 개념이 성립하지 않는다.
        rvol_recent = snap.get("rvol_recent", rvol)
        faded = rvol_recent / rvol_peak if rvol_peak >= FADE_MIN_PEAK and rvol_peak > 0 else None

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
            self.pending[ticker] = self._exit_pending("STOP", "🔴 손절하세요", f"손절 한도 {STOP_LOSS_PCT}% 도달")
            self._emit("STOP", "🔴 손절하세요", label, ticker,
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
            self.pending[ticker] = self._exit_pending("SELL", "🔴 매도하세요", f"{sell_why} {sell_line} 이탈")
            self._emit("SELL", "🔴 매도하세요", label, ticker,
                             f"{flavor}\n"
                             f"종가 {close}가 {sell_why} {sell_line} 아래로 내려감",
                             account=f"손익 {pnl}%  (평단 {avg} → 현재 {price})")
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
            self.pending[ticker] = self._exit_pending("EXIT_FULL", f"🟢 {EXIT_PORTION_STRONG} {verb}",
                                                      f"거래량 정점 {rvol_peak}배 대비 {round(faded * 100)}% 로 소진")
            self._emit("EXIT_FULL", f"🟢 {EXIT_PORTION_STRONG} {verb}", label, ticker,
                             f"{EXIT_PORTION_STRONG} 정리 권장\n"
                             f"거래량이 오늘 정점 {rvol_peak}배 → 최근 {FADE_BARS}봉 평균 {rvol_recent}배 "
                             f"({round(faded * 100)}% 수준)\n"
                             f"상승 연료 소진 — 더 오를 힘이 남지 않음",
                             account=f"손익 {pnl}%  (평단 {avg} → 현재 {price}) · 보유 {qty:g}주")
        elif (ENABLE_ADD_ON and pos_now == "above" and rvol_breakout
              and pnl >= ADDON_MIN_PROFIT_PCT
              and self.addon_count.get(ticker, 0) < ADDON_MAX_COUNT):
            # 불타기: 진입 근거(VWAP 위)가 유지되고 새 거래량이 붙었으며 이미 수익 중.
            # 돌파 기준은 매수와 같다(정규장 강봉·정점 근접). 손실 중에는 절대 발동하지 않는다 —
            # 물타기는 이 시스템이 다루지 않는다. 물량이 늘었으니 손절선도 이 봉 저점으로 올린다.
            self.addon_count[ticker] = self.addon_count.get(ticker, 0) + 1
            low = snap.get("last_low", 0)
            if low > self.stop_ref.get(ticker, 0):
                self.stop_ref[ticker], self.stop_src[ticker] = low, "추가매수 봉 저점"
            self._emit("ADDON", "🔵 추가매수 검토", label, ticker,
                             f"거래량 {prev_rvol}→{rvol}배 재돌파, 추세 살아있음\n"
                             f"매도선 {self.stop_ref.get(ticker)} 로 상향 (이 봉 저점)\n"
                             f"⚠ 물량 늘리면 손절 시 손실도 같은 배로 커짐",
                             account=f"손익 {pnl}%  (평단 {avg} → 현재 {price})")
        elif (ENABLE_EXIT_SIGNAL and faded is not None and not holding_up
              and faded <= FADE_WEAK_RATIO):
            qty = held["qty"]
            verb = "익절" if pnl > 0 else "정리"
            self._emit("EXIT_HALF", f"🟡 {EXIT_PORTION_HALF} {verb} 검토", label, ticker,
                             f"거래량이 오늘 정점 {rvol_peak}배 → 최근 {FADE_BARS}봉 평균 {rvol_recent}배 "
                             f"({round(faded * 100)}% 수준)\n"
                             f"둔화 시작. 절반 덜어내고 나머지로 추세 확인",
                             account=f"손익 {pnl}%  (평단 {avg} → 현재 {price}) · 보유 {qty:g}주 → {qty / 2:g}주 정리, {qty / 2:g}주 유지")
        elif ENABLE_AMBIGUOUS and not in_grace and self._ambiguous(pos_now, ctx):
            # 판단 애매: 근거가 흐려졌지만 아직 이탈은 아닌 구간.
            # 다른 알림이 하나도 안 걸려 방치되기 쉬운 사각지대다.
            qty = held["qty"]
            part = round(qty / 3, 1)
            verb = "익절" if pnl > 0 else "정리"
            self._emit("EXIT_THIRD", f"🟡 {EXIT_PORTION_THIRD} {verb} 검토", label, ticker,
                             f"흐려진 근거: {self._ambiguous_reason(pos_now, ctx)}\n"
                             f"급하지 않음. 조금 덜어내고 지켜봐도 되는 구간",
                             account=f"손익 {pnl}%  (평단 {avg} → 현재 {price}) · 보유 {qty:g}주 → {part:g}주 정리, {qty - part:g}주 유지")
        elif self.watchlist[ticker].get("day_trade") and self.hours.near_close(market):
            # 당일 청산 종목만. 오버나잇이 전제인 종목에 마감 정리를 말하면(자동매매면 전량 매도) 사고다.
            self._emit("CLOSE_WARN", "🟠 마감 전 정리", label, ticker,
                             f"마감 {CLOSE_WARN_MIN}분 전 — 당일 청산 종목, 오버나잇 금지",
                             account=f"손익 {pnl}%  ({held['qty']:g}주 보유)")

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
                    faded = snap["rvol_recent"] / peak if peak >= FADE_MIN_PEAK and peak > 0 else None
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
        self._emit("SUMMARY", "📊 시황", clock, None, "\n".join(lines))

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

    def _active_tickers(self, open_now: list) -> list:
        """이번 사이클에 평가할 종목.

        hold_only 종목(3배 레버리지)은 보유 중일 때만 감시한다. 안 들고 있으면 조회할 이유가
        없다 — 매수 신호는 SOXX 가 낸다. 단 상태가 남아 있으면(보유·청산대기) 한 사이클은 더 본다.
        그래야 청산이 _sync_state 에 잡혀 관망으로 돌아가고 청산 완료 알림이 나간다. 안 그러면
        청산대기가 영원히 남아, 다음에 같은 종목을 사는 순간 옛 손절 사유가 그대로 반복된다.
        """
        held = set(self._last_holdings)
        return [t for t in open_now
                if not self.watchlist[t].get("hold_only") or t in held or self.state.get(t, "관망") != "관망"]

    # -- 메인 루프 -------------------------------------------------------------
    def run(self):
        log.info("알림 전용 모드 시작 (주문 없음). 대상=%s", self.tickers)
        # 직전 사이클에 열려 있던 시장. 열고 닫힐 때 알림을 보내
        # '조용한 이유'를 사람이 알 수 있게 한다.
        prev_open = set()
        while True:
            self._reload_watchlist()
            open_now = [t for t in self.tickers if self.hours.market_open(self.watchlist[t]["market"])]
            active = self._active_tickers(open_now)
            # 프리마켓 종목은 시황에만 쓴다. 알림 판단에는 넣지 않는다.
            pre = [t for t in self.tickers
                   if t not in active and self.hours.market_premarket(self.watchlist[t]["market"])]
            now_open = {self.watchlist[t]["market"] for t in active}

            for m in now_open - prev_open:
                names = [self.watchlist[t].get("name") or t
                         for t in self.tickers if self.watchlist[t]["market"] == m]
                self._emit("MARKET_OPEN", "🔔 장 시작", "한국" if m == "KR" else "미국", None,
                                 f"{', '.join(names)} 감시 시작")
            for m in prev_open - now_open:
                self._emit("MARKET_CLOSE", "🔕 장 마감", "한국" if m == "KR" else "미국", None,
                                 "감시 종료 — 다음 개장까지 알림이 없어")
                # 오늘 청산된 거래가 있으면 성적표를 보낸다
                report = self.trades.daily_summary(m)
                if report:
                    self._emit("DAILY_REPORT", "📈 오늘 성적", "한국" if m == "KR" else "미국", None, report)
            prev_open = now_open

            if not active and not pre:
                self._reconcile_orders()
                self._save_status([], [])       # 장 밖에서도 heartbeat 는 남긴다
                time.sleep(POLL_INTERVAL_SEC)
                continue

            watch = active + pre
            leaders = sorted({s for t in watch for s in (self.watchlist[t].get("leaders") or [])})
            try:
                self.refresh_volume_profile(watch)
                # 프리마켓 등락률 계산에 종목 자신의 전일 종가도 필요하다.
                # 선행 종목은 감시 종목과 같은 시장으로 본다.
                need = {t: self.watchlist[t]["market"] for t in pre}
                for t in watch:
                    for s in (self.watchlist[t].get("leaders") or []):
                        need.setdefault(s, self.watchlist[t]["market"])
                self.refresh_prev_closes(need)
                prices = self.client.get_prices(watch + leaders)
                holdings = self.client.get_holdings() if self.watch_holdings else {}
            except Exception as e:
                log.exception("시세/보유 조회 실패: %s", e)
                self._save_status(active, pre, error=str(e))
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
            self._record_prices(prices)

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
            self._reconcile_orders()
            self._save_status(active, pre)
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
