"""기록 — 신호 추적 CSV, 청산 거래 CSV, 신호 포지션 모의 성적 CSV."""

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import TRACK_MINUTES
from .timeutil import now_local, to_local

log = logging.getLogger("scalper")


class SignalTracker:
    """ENTRY 발생 후 정해진 시간이 지난 시점의 가격을 CSV 에 기록한다.

    이 파일이 임계값 조정의 유일한 근거다. 신호가 좋았는지 나빴는지는
    "그때 기분"이 아니라 신호 후 실제 가격 변화로만 판단할 수 있다.
    수동 매매라 실제 진입 여부와 무관하게 모든 신호를 기록한다.
    """

    HEADER = ("signal_time,ticker,entry_price,vwap,rvol_prev,rvol,rvol_method,"
              "leader_pct,ema,rsi,leader_mom,horizon_min,later_price,change_pct,grade\n")

    def __init__(self, path: Path):
        self.path = path
        self.pending = []           # 아직 관측 시점이 안 된 신호들
        if self.path.exists():
            # 열이 바뀐 옛 파일(grade 없음)은 날짜를 붙여 옆으로 치우고 새로 시작한다 — 열이 어긋난 CSV 는 못 읽는다
            try:
                first = self.path.open(encoding="utf-8-sig").readline()
            except OSError:
                first = ""
            if first.strip() != self.HEADER.strip():
                keep = self.path.with_name(f"{self.path.stem}.{datetime.now(timezone.utc):%Y%m%d}{self.path.suffix}")
                try:
                    self.path.rename(keep)
                    log.info("추적 CSV 열이 바뀌어 옛 파일을 %s 로 옮겼다", keep.name)
                except OSError as e:
                    log.warning("추적 CSV 옛 파일을 옮기지 못했다 (%s) — 그대로 이어 쓴다", e)
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
                   f'{m.get("ema", "")},{m.get("rsi", "")},{m.get("leader_mom", "")},'
                   f'{item["horizon"]},{later},{change},{m.get("grade", "")}\n')
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


def fmt_num(x: float) -> str:
    """성적표용 가격 — 큰 값은 천 단위 구분, 작은 값은 있는 자리 그대로 (코인 소수점)."""
    return f"{x:,.0f}" if x >= 1000 else f"{x:g}"


def hold_min_text(minutes: float) -> str:
    return f"{minutes / 60:.1f}시간" if minutes >= 120 else f"{round(minutes)}분"


class SignalTradeLog:
    """신호 포지션(확정 매수 신호 → 확정 청산 신호)의 모의 성적을 CSV 에 남기고 하루치를 집계한다.

    체결이 아니라 신호 시점 가격이 기준이고 수량이 없으니, 건마다 같은 금액을 넣었다고 보고 단순 평균·합계로 계산한다.
    수수료·슬리피지는 반영하지 않는다 — '신호가 맞았는가' 를 보는 표다. 코인은 market='BINANCE' 이고 하루는 KST 기준이다.
    """

    HEADER = "closed_at,ticker,label,market,opened_at,entry_price,exit_price,pnl_pct,reason,hold_min\n"

    def __init__(self, path: Path):
        self.path = path
        if not self.path.exists():
            self.path.write_text(self.HEADER, encoding="utf-8-sig")

    @staticmethod
    def _tz(market: str) -> str:
        return "KR" if market == "BINANCE" else market

    def add(self, ticker: str, label: str, market: str, opened_at: str, entry: float, exit_price: float, pnl: float,
            reason: str, now: datetime = None) -> dict:
        now = now or datetime.now(timezone.utc)
        try:
            hold = round((now - datetime.fromisoformat(opened_at)).total_seconds() / 60, 1)
        except ValueError:
            hold = 0.0
        row = {"closed_at": now.isoformat(), "ticker": ticker, "label": label, "market": market, "opened_at": opened_at,
               "entry": entry, "exit": exit_price, "pnl": pnl, "reason": reason, "hold_min": hold}
        try:
            with self.path.open("a", encoding="utf-8-sig") as f:
                f.write(f"{row['closed_at']},{ticker},{label},{market},{opened_at},{entry},{exit_price},{pnl},{reason},{hold}\n")
        except OSError as e:
            log.warning("신호 성적 기록 실패: %s", e)
        return row

    def all_rows(self) -> list:
        """전체 기록 (오래된 순). 시각은 market 현지(코인은 KST)로 바꿔 둔다 — 백오피스 매매 결과 화면과 하루치 집계가 쓴다."""
        out = []
        try:
            lines = self.path.read_text(encoding="utf-8-sig").splitlines()[1:]
        except OSError:
            return out
        for ln in lines:
            p = ln.split(",")
            if len(p) < 10:
                continue
            tz = self._tz(p[3])
            try:
                out.append({"market": p[3], "closed": to_local(datetime.fromisoformat(p[0]), tz), "ticker": p[1], "label": p[2],
                            "opened": to_local(datetime.fromisoformat(p[4]), tz), "entry": float(p[5]), "exit": float(p[6]),
                            "pnl": float(p[7]), "reason": p[8], "hold_min": float(p[9])})
            except ValueError:
                continue
        return out

    def rows_on(self, market: str, day: str = None) -> list:
        """market 의 현지 날짜 day(YYYY-MM-DD, 기본 오늘)에 청산된 신호들 (기록 순서)."""
        day = day or now_local(self._tz(market)).strftime("%Y-%m-%d")
        return [r for r in self.all_rows() if r["market"] == market and r["closed"].strftime("%Y-%m-%d") == day]

    def daily_summary(self, market: str, open_lines=(), day: str = None) -> str:
        """마감(코인은 자정) 성적표. 청산된 신호도 진행 중인 신호도 없으면 빈 문자열."""
        rows, open_lines = self.rows_on(market, day), list(open_lines)
        if not rows and not open_lines:
            return ""
        parts = []
        if rows:
            wins = [r for r in rows if r["pnl"] > 0]
            losses = [r for r in rows if r["pnl"] < 0]
            avg = sum(r["pnl"] for r in rows) / len(rows)
            parts.append(f"청산 {len(rows)}건: {len(wins)}익절 {len(losses)}손절 (승률 {round(len(wins) / len(rows) * 100)}%)")
            parts.append(f"평균 {avg:+.2f}% · 합계 {sum(r['pnl'] for r in rows):+.2f}%  (건당 같은 금액 기준)")
            if wins and losses:
                aw = sum(r["pnl"] for r in wins) / len(wins)
                al = abs(sum(r["pnl"] for r in losses) / len(losses))
                parts.append(f"손익비 1:{round(aw / al, 2)}  (평균 익절 {aw:+.2f}% / 평균 손절 -{al:.2f}%)")
            for r in rows:
                parts.append(f"· {r['opened']:%H:%M} {r['label']} {fmt_num(r['entry'])} → {fmt_num(r['exit'])} "
                             f"{r['pnl']:+.2f}% {r['reason']} ({hold_min_text(r['hold_min'])})")
        else:
            parts.append("오늘 청산된 신호 없음")
        if open_lines:
            parts.append(f"진행 중 {len(open_lines)}건 — 청산 신호가 나올 때까지 계속 관리")
            parts += [f"· {x}" for x in open_lines]
        parts += ["", "※ 신호가 → 청산 신호 시점 가격의 모의 성적. 체결·수수료 미반영"]
        return "\n".join(parts)
