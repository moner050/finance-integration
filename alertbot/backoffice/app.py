"""백오피스 — 종목 관리, 엔진 상태, 신호 이력, 채널 테스트.

FastAPI + Jinja2 + HTMX. 로컬 전용(인증 없음)이라 기본 127.0.0.1 에만 바인딩한다.
엔진과는 MySQL 만 공유한다: 여기서 바꾼 종목은 엔진이 다음 사이클(30초)에 읽는다.
"""

import html
import logging
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import db
from ..config import (AUTOTRADE_HARD_MAX_AMOUNT_KRW, AUTOTRADE_HARD_MAX_AMOUNT_USD, AUTOTRADE_MODE,
                      CLIENT_ID, CLIENT_SECRET, TG_CHATS, TG_MIN_SEVERITY, TG_TOKEN)
from ..config import BINANCE_TRADE_MODE
from ..models import Signal
from ..notify import build_channels
from ..notify.dispatcher import Dispatcher
from ..timeutil import to_local
from ..toss_client import TossReadOnlyClient

log = logging.getLogger("scalper")
app = FastAPI(title="단타 알림 백오피스")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

HEARTBEAT_WARN_SEC = 90         # 30초 폴링이 세 번 빠지면 엔진이 멈춘 것으로 본다

_store = None
_lock = threading.Lock()        # PyMySQL 연결은 스레드 안전이 아니다. 요청은 순서대로 DB 를 쓴다


@contextmanager
def get_db():
    global _store
    with _lock:
        if _store is None:
            _store = db.connect()
        yield _store


def kst(value) -> str:
    """UTC ISO 문자열 → 'MM-DD HH:MM:SS' (KST)."""
    if not value:
        return "-"
    try:
        return to_local(datetime.fromisoformat(str(value)), "KR").strftime("%m-%d %H:%M:%S")
    except ValueError:
        return str(value)


def age_sec(value):
    if not value:
        return None
    try:
        return int((datetime.now(timezone.utc) - datetime.fromisoformat(str(value))).total_seconds())
    except ValueError:
        return None


templates.env.filters["kst"] = kst


def render(request: Request, name: str, status_code: int = 200, **ctx):
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


# -- 상태 ----------------------------------------------------------------------

def status_context() -> dict:
    with get_db() as d:
        st = db.load_engine_status(d)
        rows = db.list_watch_rows(d)
    tickers = []
    if st:
        for sym in sorted(set(st["snapshots"]) | set(st["state"])):
            snap = st["snapshots"].get(sym, {})
            s = st["state"].get(sym, {})
            tickers.append({"symbol": sym, **snap, "state": s.get("state", "관망"),
                            "stop_ref": s.get("stop_ref"), "pending": s.get("pending")})
    age = age_sec(st["heartbeat_at"]) if st else None
    return {"status": st, "tickers": tickers, "age": age,
            "stale": age is None or age > HEARTBEAT_WARN_SEC,
            "watch_count": len(rows), "enabled_count": sum(1 for r in rows if r["enabled"])}


@app.get("/", response_class=HTMLResponse)
def status_page(request: Request):
    return render(request, "status.html", **status_context())


@app.get("/partials/status", response_class=HTMLResponse)
def status_partial(request: Request):
    return render(request, "_status.html", **status_context())


# -- 종목 ----------------------------------------------------------------------

def _pair_warnings(rows: list) -> set:
    """페어가 목록에 없거나 상대의 페어가 나를 가리키지 않는 종목."""
    by_symbol = {r["symbol"]: r for r in rows}
    bad = set()
    for r in rows:
        p = r["pair"]
        if p and (p not in by_symbol or by_symbol[p]["pair"] != r["symbol"]):
            bad.add(r["symbol"])
    return bad


def watchlist_context(edit: str = None, error: str = None) -> dict:
    with get_db() as d:
        rows = db.list_watch_rows(d)
        edit_row = db.get_watch_row(d, edit.upper()) if edit else None
    return {"rows": rows, "pair_warn": _pair_warnings(rows), "edit": edit_row, "error": error}


@app.get("/watchlist", response_class=HTMLResponse)
def watchlist_page(request: Request, edit: str = None):
    return render(request, "watchlist.html", **watchlist_context(edit))


@app.post("/watchlist")
def watchlist_save(request: Request, symbol: str = Form(...), market: str = Form(...),
                   name: str = Form(""), leaders: str = Form(""), inverse: bool = Form(False),
                   pair: str = Form(""), hold_only: bool = Form(False), note: str = Form(""),
                   enabled: bool = Form(False), auto_trade: bool = Form(False), auto_amount: str = Form("0")):
    try:
        amount = float(auto_amount or 0)
        if amount < 0:
            raise ValueError("1회 매수 금액은 0 이상")
        with get_db() as d:
            db.upsert_watch(d, symbol, market, name.strip(), leaders.split(","), inverse, pair,
                            hold_only, note.strip(), enabled, auto_trade, amount)
    except ValueError as e:
        return render(request, "watchlist.html", status_code=400, **watchlist_context(error=str(e)))
    log.info("백오피스: 종목 저장 %s", symbol.strip().upper())
    return RedirectResponse("/watchlist", status_code=303)


@app.post("/watchlist/{symbol}/toggle")
def watchlist_toggle(symbol: str):
    with get_db() as d:
        row = db.get_watch_row(d, symbol)
        if row:
            db.set_enabled(d, symbol, not row["enabled"])
    return RedirectResponse("/watchlist", status_code=303)


@app.post("/watchlist/{symbol}/delete")
def watchlist_delete(symbol: str):
    with get_db() as d:
        db.delete_watch(d, symbol)
    log.info("백오피스: 종목 삭제 %s", symbol)
    return RedirectResponse("/watchlist", status_code=303)


@app.post("/watchlist/validate", response_class=HTMLResponse)
def watchlist_validate(symbol: str = Form(""), leaders: str = Form("")):
    """토스 현재가 API 로 심볼·선행 종목이 실제로 조회되는지 본다."""
    syms = [s.strip().upper() for s in [symbol] + leaders.split(",") if s.strip()]
    if not syms:
        return '<span class="warn">심볼을 입력하세요</span>'
    try:
        prices = TossReadOnlyClient(CLIENT_ID, CLIENT_SECRET).get_prices(syms)
    except (Exception, SystemExit) as e:          # 토큰 실패(403 등)는 SystemExit 로 올라온다
        return f'<span class="warn">조회 실패: {html.escape(str(e))}</span>'
    if prices is None:
        return '<span class="warn">토스 API 응답 없음 — 허용 IP·인증 정보를 확인할 것</span>'
    parts = [f"{s}: " + (f"✅ {prices[s]}" if s in prices else "❌ 미확인") for s in syms]
    return "<span>" + html.escape(" · ".join(parts)) + "</span>"


# -- 신호 이력 ---------------------------------------------------------------

@app.get("/signals", response_class=HTMLResponse)
def signals_page(request: Request, symbol: str = "", severity: str = "", limit: int = 200):
    with get_db() as d:
        rows = db.recent_signals(d, limit=min(max(limit, 1), 1000), symbol=symbol.strip().upper() or None,
                                 severity=severity or None)
    return render(request, "signals.html", rows=rows, symbol=symbol, severity=severity)


# -- 자동매매 ------------------------------------------------------------------

def trading_context(message: str = None) -> dict:
    with get_db() as d:
        settings = db.get_settings(d)
        orders = db.recent_orders(d, 200)
        rows = db.list_watch_rows(d)
        bn = db.binance_positions(d, limit=100)
    auto_rows = [r for r in rows if r.get("auto_trade")]
    live_ready = AUTOTRADE_MODE == "live" and settings["autotrade_enabled"] == "1" and bool(auto_rows)
    return {"mode": AUTOTRADE_MODE, "settings": settings, "orders": orders, "auto_rows": auto_rows,
            "live_ready": live_ready, "hard_max": {"KRW": AUTOTRADE_HARD_MAX_AMOUNT_KRW, "USD": AUTOTRADE_HARD_MAX_AMOUNT_USD},
            "open_count": sum(1 for o in orders if o["status"] in ("sent", "open")), "message": message,
            "bn_mode": BINANCE_TRADE_MODE, "bn_positions": bn}


@app.get("/trading", response_class=HTMLResponse)
def trading_page(request: Request, message: str = None):
    return render(request, "trading.html", **trading_context(message))


@app.post("/trading/toggle")
def trading_toggle():
    with get_db() as d:
        cur = db.get_settings(d)["autotrade_enabled"]
        db.set_setting(d, "autotrade_enabled", 0 if cur == "1" else 1)
    log.info("백오피스: 자동매매 킬 스위치 → %s", "OFF" if cur == "1" else "ON")
    return RedirectResponse("/trading", status_code=303)


@app.post("/trading/settings")
def trading_settings(request: Request, max_positions: int = Form(...), max_orders_per_day: int = Form(...),
                     daily_loss_limit_krw: float = Form(...), daily_loss_limit_usd: float = Form(...),
                     max_order_amount_krw: float = Form(...), max_order_amount_usd: float = Form(...)):
    values = {"max_positions": max_positions, "max_orders_per_day": max_orders_per_day,
              "daily_loss_limit_krw": daily_loss_limit_krw, "daily_loss_limit_usd": daily_loss_limit_usd,
              "max_order_amount_krw": max_order_amount_krw, "max_order_amount_usd": max_order_amount_usd}
    if any(v < 0 for v in values.values()):
        return render(request, "trading.html", status_code=400, **trading_context("한도는 0 이상이어야 한다"))
    with get_db() as d:
        for k, v in values.items():
            db.set_setting(d, k, f"{v:g}")
    return RedirectResponse("/trading", status_code=303)


@app.post("/trading/orders/{intent_id}/cancel", response_class=HTMLResponse)
def trading_cancel(intent_id: str):
    """미결 주문 수동 취소. live 는 실제 취소, dry 는 기록만 바꾼다."""
    with get_db() as d:
        row = db.get_order(d, intent_id)
        if not row or row["status"] not in ("sent", "open"):
            return '<span class="warn">취소할 수 있는 상태가 아니다</span>'
        if row["mode"] == "live":
            try:
                from ..trading.broker import BrokerError, TossOrderClient
                cli = TossReadOnlyClient(CLIENT_ID, CLIENT_SECRET)
                cli.load_account()
                TossOrderClient(cli).cancel(row["order_id"])
            except (Exception, SystemExit) as e:
                return f'<span class="warn">취소 실패: {html.escape(str(e))}</span>'
        db.update_order(d, intent_id, status="canceled", reason="manual")
    return '<span class="ok">취소됨 (새로고침)</span>'


@app.post("/trading/check-permission", response_class=HTMLResponse)
def trading_check_permission():
    """주문 권한 확인: 매수가능금액 조회(읽기 전용)가 prerequisite-required 를 돌려주는지 본다."""
    try:
        from ..trading.broker import BrokerError, TossOrderClient
        cli = TossReadOnlyClient(CLIENT_ID, CLIENT_SECRET)
        if not cli.load_account():
            return '<span class="warn">BROKERAGE 계좌를 찾지 못했다</span>'
        krw = TossOrderClient(cli).buying_power("KRW")
        return f'<span class="ok">주문 API 접근 가능 · 매수가능금액 {krw:,.0f} KRW</span>'
    except BrokerError as e:
        if e.code == "prerequisite-required":
            return '<span class="warn">사전 자격 미충족 — 토스 WTS 에서 약관 동의·교육 이수·위험 고지를 완료해야 한다</span>'
        return f'<span class="warn">주문 API 오류: {html.escape(str(e))}</span>'
    except (Exception, SystemExit) as e:
        return f'<span class="warn">조회 실패: {html.escape(str(e))}</span>'


# -- 채널 ----------------------------------------------------------------------

def channel_rows() -> list:
    return [
        {"name": "telegram", "configured": bool(TG_TOKEN and TG_CHATS), "recipients": len(TG_CHATS),
         "min_severity": TG_MIN_SEVERITY, "detail": "Bot API sendMessage"},
    ]


@app.get("/channels", response_class=HTMLResponse)
def channels_page(request: Request):
    return render(request, "channels.html", channels=channel_rows())


@app.post("/channels/{name}/test", response_class=HTMLResponse)
def channel_test(name: str):
    """해당 채널 하나로 테스트 발송. 쿨다운·등급을 무시하고 이력에는 남긴다."""
    channels = [c for c in build_channels() if c.name == name]
    if not channels:
        return '<span class="warn">채널이 설정되어 있지 않다 (.env 확인)</span>'
    channels[0].min_severity = "info"
    stamp = kst(datetime.now(timezone.utc).isoformat())
    with get_db() as d:
        dispatcher = Dispatcher(channels, record=lambda s, r: db.log_signal(d, s, r))
        result = dispatcher.send(Signal("SYSTEM", "⚪ 시스템", "테스트 발송",
                                        f"백오피스에서 보낸 테스트 ({stamp} KST)"), force=True)
    out = str(result.get(name, "no result"))
    css = "ok" if out == "ok" else "warn"
    return f'<span class="{css}">{html.escape(out)}</span>'
