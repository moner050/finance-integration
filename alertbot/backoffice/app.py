"""백오피스 — 종목 관리, 엔진 상태, 신호 이력, 자동매매, 계정·API 키.

FastAPI + Jinja2 + HTMX. 모든 화면은 Google 로그인(허용 계정만) 뒤에 있다 — 전역 의존성이 세션·CSRF 를 확인하고,
관리자 전용 조작은 admin_only 로 막는다. 기본 바인딩은 127.0.0.1 이다.
엔진과는 MySQL 만 공유한다: 여기서 바꾼 종목·계정·키는 워커가 다음 사이클(30초)에 읽는다.
"""

import hmac
import html
import json
import logging
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import accounts, binance_scan, config, db, supervisor
from ..config import (ADMIN_EMAIL, AUTOTRADE_HARD_MAX_AMOUNT_KRW, AUTOTRADE_HARD_MAX_AMOUNT_USD, AUTOTRADE_MODE,
                      CLIENT_ID, CLIENT_SECRET, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, SESSION_HOURS,
                      TG_PUBLIC_CHATS, TG_PUBLIC_TOKEN)
from ..config import BINANCE_SIGNAL_TRADE_FILE, BINANCE_TRADE_MODE, DATA_DIR, SCAN_EXCLUDE, SCAN_TOP_N, SIGNAL_TRADE_FILE
from ..crypto import CryptoError
from ..models import Signal
from ..notify import build_channels
from ..notify.dispatcher import Dispatcher
from ..timeutil import to_local
from ..toss_client import TossReadOnlyClient
from ..tracking import SignalTradeLog
from . import auth

log = logging.getLogger("scalper")
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
            accounts.ensure_admin(_store, ADMIN_EMAIL)      # 첫 관리자 보장 (중지·강등돼 있으면 되돌림)
            accounts.purge_expired(_store)
        yield _store


# -- 인증 ----------------------------------------------------------------------

def require_session(request: Request):
    """로그인 확인. 동기 함수라 쓰레드풀에서 돈다 — DB 락을 기다리는 동안 이벤트 루프를 막지 않는다.

    세션이 없으면 일반 요청은 로그인 화면으로 보내고, HTMX 부분 요청은 401 + HX-Redirect 로 페이지 전체를 옮긴다
    (303 을 주면 XHR 이 따라가 로그인 화면이 표 칸 안에 끼워진다).
    """
    if request.url.path in auth.PUBLIC_PATHS:
        return None
    with get_db() as d:
        me = accounts.session_account(d, request.cookies.get(auth.SESSION_COOKIE))
    if me is None:
        if request.headers.get("HX-Request"):
            raise HTTPException(401, "로그인이 필요하다", headers={"HX-Redirect": "/login"})
        raise HTTPException(303, "로그인이 필요하다", headers={"Location": "/login"})
    request.state.me = me
    return me


async def require_csrf(request: Request, me=Depends(require_session)):
    """상태를 바꾸는 요청은 세션의 CSRF 토큰을 헤더(HTMX, base.html hx-headers)나 폼 필드(csrf)로 가져와야 한다."""
    if me is None or request.method in ("GET", "HEAD", "OPTIONS"):
        return
    token = request.headers.get("X-CSRF-Token")
    if not token:
        token = (await request.form()).get("csrf")
    if not token or not hmac.compare_digest(str(token), me["session_csrf"]):
        raise HTTPException(403, "CSRF 토큰이 없거나 틀렸다 — 페이지를 새로고침할 것")


def admin_only(me=Depends(require_session)):
    if me is None or me["role"] != "admin":
        raise HTTPException(403, "관리자만 할 수 있다")
    return me


app = FastAPI(title="단타 알림 백오피스", dependencies=[Depends(require_csrf)],
              docs_url=None, redoc_url=None, openapi_url=None)       # /docs·/openapi.json 은 인증 밖이라 끈다


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


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
templates.env.filters["kname"] = lambda sym, names: names.get(sym, sym)   # 심볼 → 표시명


def render(request: Request, name: str, status_code: int = 200, **ctx):
    me = getattr(request.state, "me", None)
    ctx = {"me": me, "csrf": me["session_csrf"] if me else "", **ctx}
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return render(request, "login.html", error=None, configured=bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET),
                  login_url=auth.login_url())


# state 쿠키는 콜백 경로에만 실린다. 콜백 경로는 .env 리다이렉트 URL 이 정해서 시작 경로(/auth/google)와 다를 수 있다
def _clear_oauth(resp):
    resp.delete_cookie(auth.OAUTH_COOKIE, path=auth.CALLBACK_PATH, secure=auth.SECURE_COOKIE, httponly=True, samesite="lax")
    return resp


@app.get(auth.START_PATH)
def google_start():
    if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET):
        return RedirectResponse("/login", status_code=303)
    url, cookie = auth.start_login()
    resp = RedirectResponse(url, status_code=303)
    resp.set_cookie(auth.OAUTH_COOKIE, cookie, max_age=auth.OAUTH_TTL_SEC, path=auth.CALLBACK_PATH, httponly=True,
                    samesite="lax", secure=auth.SECURE_COOKIE)
    return resp


@app.get(auth.CALLBACK_PATH, response_class=HTMLResponse)
def google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    """허용 목록(alert_accounts 활성)에 있는 이메일만 세션을 받는다. 첫 로그인의 구글 sub 를 고정해 같은 이메일의 다른 계정을 막는다."""
    try:
        if error:
            raise auth.AuthError("구글 로그인이 취소됐다")
        verifier = auth.verifier_for(request.cookies.get(auth.OAUTH_COOKIE), state)
        who = auth.exchange_code(code, verifier)
        with get_db() as d:
            acc = accounts.get_by_email(d, who["email"])
            if acc is None or not acc["active"]:
                raise auth.AuthError(f"{who['email']} 은 허용된 계정이 아니다 — 관리자에게 등록을 요청할 것")
            if acc["google_sub"] and acc["google_sub"] != who["sub"]:
                raise auth.AuthError("등록된 구글 계정과 다른 계정이다")
            accounts.record_login(d, acc["id"], who["sub"])
            token, _ = accounts.create_session(d, acc["id"])
    except auth.AuthError as e:
        log.warning("백오피스 로그인 거부: %s", e)
        return _clear_oauth(render(request, "login.html", status_code=403, error=str(e),
                                   configured=bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET), login_url=auth.login_url()))
    log.info("백오피스 로그인: %s", who["email"])
    resp = _clear_oauth(RedirectResponse("/", status_code=303))
    resp.set_cookie(auth.SESSION_COOKIE, token, max_age=int(SESSION_HOURS * 3600), path="/", httponly=True,
                    samesite="lax", secure=auth.SECURE_COOKIE)
    return resp


@app.post("/logout")
def logout(request: Request):
    with get_db() as d:
        accounts.delete_session(d, request.cookies.get(auth.SESSION_COOKIE))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.SESSION_COOKIE, path="/", secure=auth.SECURE_COOKIE, httponly=True, samesite="lax")
    return resp


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
    names = {r["symbol"]: r["name"] or r["symbol"] for r in rows}
    return {"status": st, "tickers": tickers, "age": age, "names": names,
            "stale": age is None or age > HEARTBEAT_WARN_SEC,
            "watch_count": len(rows), "enabled_count": sum(1 for r in rows if r["enabled"])}


@app.get("/", response_class=HTMLResponse)
def status_page(request: Request, limit: int = 16):
    """상태 탭 = 지금 지표(30초 갱신) + 시황 시계열. 둘 다 '시장이 어디까지 왔나' 라 한 화면이다."""
    return render(request, "status.html", **(summary_context(min(max(limit, 2), 96)) | status_context()))


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


def watchlist_context(edit: str = None, error: str = None, coin_error: str = None) -> dict:
    with get_db() as d:
        rows = db.list_watch_rows(d)
        edit_row = db.get_watch_row(d, edit.upper()) if edit else None
        settings = db.get_settings(d)
    try:
        snapshot = json.loads(settings.get(binance_scan.SNAPSHOT_KEY) or "{}")
    except ValueError:
        snapshot = {}
    coins = {"snapshot": snapshot, "error": coin_error, "top_n": SCAN_TOP_N, "fixed_exclude": SCAN_EXCLUDE,
             "include": ", ".join(binance_scan.parse_list(settings.get(binance_scan.INCLUDE_KEY))),
             "exclude": ", ".join(binance_scan.parse_list(settings.get(binance_scan.EXCLUDE_KEY)))}
    return {"rows": rows, "pair_warn": _pair_warnings(rows), "edit": edit_row, "error": error, "coins": coins}


@app.get("/watchlist", response_class=HTMLResponse)
def watchlist_page(request: Request, edit: str = None):
    return render(request, "watchlist.html", **watchlist_context(edit))


@app.post("/watchlist", dependencies=[Depends(admin_only)])
def watchlist_save(request: Request, symbol: str = Form(...), market: str = Form(...),
                   name: str = Form(""), leaders: str = Form(""), inverse: bool = Form(False),
                   pair: str = Form(""), hold_only: bool = Form(False), note: str = Form(""),
                   enabled: bool = Form(False), auto_trade: bool = Form(False), auto_amount: str = Form("0"),
                   day_trade: bool = Form(False)):
    try:
        amount = float(auto_amount or 0)
        if amount < 0:
            raise ValueError("1회 매수 금액은 0 이상")
        with get_db() as d:
            db.upsert_watch(d, symbol, market, name.strip(), leaders.split(","), inverse, pair,
                            hold_only, note.strip(), enabled, auto_trade, amount, day_trade)
    except ValueError as e:
        return render(request, "watchlist.html", status_code=400, **watchlist_context(error=str(e)))
    log.info("백오피스: 종목 저장 %s", symbol.strip().upper())
    return RedirectResponse("/watchlist", status_code=303)


@app.post("/watchlist/{symbol}/toggle", dependencies=[Depends(admin_only)])
def watchlist_toggle(symbol: str):
    with get_db() as d:
        row = db.get_watch_row(d, symbol)
        if row:
            db.set_enabled(d, symbol, not row["enabled"])
    return RedirectResponse("/watchlist", status_code=303)


@app.post("/watchlist/{symbol}/delete", dependencies=[Depends(admin_only)])
def watchlist_delete(symbol: str):
    with get_db() as d:
        db.delete_watch(d, symbol)
    log.info("백오피스: 종목 삭제 %s", symbol)
    return RedirectResponse("/watchlist", status_code=303)


@app.post("/watchlist/coins", dependencies=[Depends(admin_only)])
def watchlist_coins(request: Request, include: str = Form(""), exclude: str = Form("")):
    """코인 급변 감시에 더하거나 뺄 코인 — 코인 워커가 다음 사이클(20초 안)에 반영한다. 거래할 수 없는 코인은 워커가 목록에 표시한다."""
    inc, exc = binance_scan.parse_list(include), binance_scan.parse_list(exclude)
    both = sorted(set(inc) & set(exc))
    if both:
        return render(request, "watchlist.html", status_code=400,
                      **watchlist_context(coin_error=f"추가와 제외에 같은 코인이 있다: {', '.join(both)}"))
    with get_db() as d:
        db.set_setting(d, binance_scan.INCLUDE_KEY, ",".join(inc))
        db.set_setting(d, binance_scan.EXCLUDE_KEY, ",".join(exc))
    log.info("백오피스: 급변 감시 코인 — 추가 %s · 제외 %s", ",".join(inc) or "-", ",".join(exc) or "-")
    return RedirectResponse("/watchlist", status_code=303)


@app.post("/watchlist/validate", response_class=HTMLResponse, dependencies=[Depends(admin_only)])
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

def visible_accounts(me: dict):
    """화면에 보일 장부 — 관리자는 전부(None), 일반 계정은 공용(None)과 자기 계정."""
    return None if me["role"] == "admin" else [None, me["id"]]


def account_emails(d, me: dict) -> dict:
    return {a["id"]: a["email"] for a in accounts.list_accounts(d)} if me["role"] == "admin" else {me["id"]: me["email"]}


@app.get("/signals", response_class=HTMLResponse)
def signals_page(request: Request, symbol: str = "", severity: str = "", limit: int = 200, me=Depends(require_session)):
    with get_db() as d:
        rows = db.recent_signals(d, limit=min(max(limit, 1), 1000), symbol=symbol.strip().upper() or None,
                                 severity=severity or None, account_ids=visible_accounts(me))
        emails = account_emails(d, me)
    return render(request, "signals.html", rows=rows, symbol=symbol, severity=severity, channels=channel_rows(), emails=emails)


# -- 시황 시계열 -----------------------------------------------------------------

MINE_HEADERS = ("가상 보유", "가상 포지션 (dry)", "내 보유", "내 포지션", "내 포지션 (LIVE)")


def parse_summary(body: str) -> dict:
    """시황 본문(30분마다 저장되는 SUMMARY 신호) → 종목별 상태.

    줄 형식은 엔진·Binance 워커가 만든 그대로다: '<표시> <이름>  <상세> | <조건>'. 이름과 상세는 공백 두 개로 나뉜다.
    '가상 보유'/'가상 포지션 (dry)' 아래 줄은 공용 가상 장부의 보유 현황이라 따로 모은다 (옛 이력의 '내 보유'/'내 포지션' 도 같다).
    각주(※)와 빈 줄은 버린다.
    """
    items, mine, section = {}, [], "market"
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("※"):
            continue
        if line in MINE_HEADERS:
            section = "mine"
            continue
        if section == "mine":
            mine.append(line)
            continue
        head, _, detail = line.partition("  ")
        mark, _, name = head.partition(" ")
        if not mark or any(ch.isalnum() for ch in mark):      # 기호(▲▼－🔵🌙…)가 아니면 이름의 일부다 (코인: '급락 매수 5분봉 ETCUSDT')
            mark, name = "", head
        if not detail:                                  # 형식 밖의 줄은 그대로 보여 준다
            mark, name, detail = "", head, ""
        cond = detail.rsplit(" | ", 1)[1] if " | " in detail else detail
        # 칸에는 핵심만: '2/3' 같은 충족 개수, 아니면 조건 문구의 앞부분. 부족 항목·수치는 마우스 오버(detail)로 본다.
        frac = re.search(r"\d/\d", cond)
        short = frac.group(0) if frac else cond.split(" — ")[0].split(" (")[0]
        items[name] = {"mark": mark, "detail": detail, "cond": cond, "short": short}
    return {"items": items, "mine": mine}


def summary_series(rows: list) -> dict:
    """SUMMARY 행(최신순) → {"times": [...], "symbols": [...], "cells": {symbol: [cell|None, ...]}, "latest": row}.

    열은 시각(오래된 것부터), 행은 종목. 종목 순서는 가장 최근 시황에 나온 순서, 그 뒤 나머지.
    """
    if not rows:
        return {"times": [], "symbols": [], "cells": {}, "latest": None, "mine": []}
    parsed = [(r, parse_summary(r["body"])) for r in reversed(rows)]        # 오래된 것부터
    order = list(parsed[-1][1]["items"])
    for _, p in parsed:
        order += [s for s in p["items"] if s not in order]
    times = []
    for r, _ in parsed:
        t = to_local(datetime.fromisoformat(str(r["sent_at"])), "KR")
        times.append({"clock": t.strftime("%H:%M"), "day": t.strftime("%m-%d")})
    cells = {s: [p["items"].get(s) for _, p in parsed] for s in order}
    return {"times": times, "symbols": order, "cells": cells, "latest": parsed[-1][0], "mine": parsed[-1][1]["mine"]}


def summary_context(limit: int) -> dict:
    with get_db() as d:
        rows = db.recent_signals(d, limit=limit * 2, kind="SUMMARY")
        names = {r["symbol"]: r["name"] or r["symbol"] for r in db.list_watch_rows(d)}
    stock = [r for r in rows if r["title"] == "📊 시황"][:limit]
    coin = [r for r in rows if r["title"] == "📊 코인 시황"][:limit]
    return {"stock": summary_series(stock), "coin": summary_series(coin), "limit": limit, "names": names}


@app.get("/summary")
def summary_page(limit: int = 16):
    """옛 주소 — 시황 시계열은 상태 탭으로 합쳤다."""
    return RedirectResponse(f"/?limit={limit}", status_code=303)


# -- 매매 결과 -------------------------------------------------------------------

def build_results(rows: list) -> dict:
    """체결된 매도(alert_orders) → 건별(최신순, 평단 대비 수익률)과 일별 시계열(오래된 순, 통화별 손익·누적). 시장이 섞이므로 통화별로 따로 더한다."""
    trades, days, cum = [], {}, {"KRW": 0.0, "USD": 0.0}
    for o in rows:
        ccy = "KRW" if o["market"] == "KR" else "USD"
        ref, price = o.get("ref_avg"), o.get("avg_price")
        pct = round((price - ref) / ref * 100, 2) if ref and price else None
        day = to_local(datetime.fromisoformat(str(o["created_at"])), "KR").strftime("%m-%d")
        d = days.setdefault(day, {"day": day, "n": 0, "wins": 0, "pnl": {"KRW": 0.0, "USD": 0.0}, "cum": None})
        d["n"] += 1
        d["wins"] += o["pnl"] > 0
        d["pnl"][ccy] += o["pnl"]
        cum[ccy] += o["pnl"]
        d["cum"] = dict(cum)
        trades.append({**o, "ccy": ccy, "pct": pct, "day": day})
    n, wins = len(trades), sum(1 for t in trades if t["pnl"] > 0)
    return {"trades": trades[::-1], "days": list(days.values()), "n": n, "wins": wins,
            "rate": round(wins / n * 100) if n else 0, "pnl": cum}


def build_signal_results(rows: list) -> dict:
    """신호 포지션 모의 성적(CSV) → 건별(최신순)과 일별 시계열(수익률 합·누적, 건당 같은 금액 기준)."""
    days, cum = {}, 0.0
    for r in rows:
        day = r["closed"].strftime("%m-%d")
        d = days.setdefault(day, {"day": day, "n": 0, "wins": 0, "pnl": 0.0, "cum": 0.0})
        d["n"] += 1
        d["wins"] += r["pnl"] > 0
        d["pnl"] = round(d["pnl"] + r["pnl"], 2)
        cum = round(cum + r["pnl"], 2)
        d["cum"] = cum
    n, wins = len(rows), sum(1 for r in rows if r["pnl"] > 0)
    return {"trades": rows[::-1], "days": list(days.values()), "n": n, "wins": wins,
            "rate": round(wins / n * 100) if n else 0, "pnl": cum}


def results_context(me: dict, account: int = None) -> dict:
    """live 는 한 계정씩 (일반은 자기 계정, 관리자는 ?account= 로 고른다). 가상은 공용 장부. 신호 성적은 CSV."""
    admin = me["role"] == "admin"
    target = int(account) if admin and account else me["id"]
    with get_db() as d:
        live_rows = db.trade_rows(d, mode="live", account_ids=[target])
        dry_rows = db.trade_rows(d, mode="dry", account_ids=[None])
        names = {r["symbol"]: r["name"] or r["symbol"] for r in db.list_watch_rows(d)}
        dry_pos = db.dry_positions(d)
        emails = account_emails(d, me)
    signals = SignalTradeLog(DATA_DIR / SIGNAL_TRADE_FILE).all_rows() + SignalTradeLog(DATA_DIR / BINANCE_SIGNAL_TRADE_FILE).all_rows()
    signals.sort(key=lambda r: r["closed"])
    return {"live": build_results(live_rows), "dry": build_results(dry_rows),
            "signals": build_signal_results(signals), "dry_positions": dry_pos, "names": names,
            "mode": AUTOTRADE_MODE, "emails": emails, "target": target}


@app.get("/results", response_class=HTMLResponse)
def results_page(request: Request, account: int = None, me=Depends(require_session)):
    return render(request, "results.html", **results_context(me, account))


# -- 자동매매 ------------------------------------------------------------------

def trading_context(me: dict, message: str = None) -> dict:
    admin = me["role"] == "admin"
    with get_db() as d:
        settings = db.get_settings(d)
        virtual_orders = db.recent_orders(d, 100, account_ids=[None])
        live_orders = db.recent_orders(d, 200, account_ids=None if admin else [me["id"]])
        rows = db.list_watch_rows(d)
        bn = db.binance_positions(d, limit=100, account_ids=None if admin else [None, me["id"]])
        dry = db.dry_positions(d)                                   # 공용 가상 장부의 모의 보유
        mine = accounts.get(d, me["id"])
        keys = accounts.key_status(d, me["id"])
        emails = account_emails(d, me)
    live_orders = [o for o in live_orders if o.get("account_id") is not None]
    return {"mode": AUTOTRADE_MODE, "bn_mode": BINANCE_TRADE_MODE, "settings": settings, "virtual_orders": virtual_orders,
            "live_orders": live_orders, "auto_rows": [r for r in rows if r.get("auto_trade")], "dry_positions": dry,
            "mine": mine, "keys": keys, "emails": emails, "message": message,
            "hard_max": {"KRW": AUTOTRADE_HARD_MAX_AMOUNT_KRW, "USD": AUTOTRADE_HARD_MAX_AMOUNT_USD},
            "open_count": sum(1 for o in live_orders if o["status"] in ("sent", "open")), "bn_positions": bn}


@app.get("/trading", response_class=HTMLResponse)
def trading_page(request: Request, message: str = None, me=Depends(require_session)):
    return render(request, "trading.html", **trading_context(me, message))


@app.post("/trading/live")
def trading_live(request: Request, toss_live: bool = Form(False), binance_live: bool = Form(False),
                 amount_scale: str = Form("1"), binance_capital: str = Form("0"), me=Depends(require_session)):
    """내 계정의 live 설정. 켜려면 그 공급자 키와 텔레그램 키가 있어야 한다 (알림 없는 실매매 금지)."""
    with get_db() as d:
        keys = accounts.key_status(d, me["id"])
    try:
        scale, capital = float(amount_scale), float(binance_capital)
    except ValueError:
        scale, capital = -1.0, -1.0
    error = None
    if not 0 < scale <= 10:
        error = "금액 배율은 0 초과 10 이하"
    elif capital < 0:
        error = "Binance 자본은 0 이상"
    elif (toss_live or binance_live) and not keys["telegram"]:
        error = "live 를 켜려면 먼저 내 API 키에서 텔레그램 키를 저장해야 한다"
    elif toss_live and not keys["toss"]:
        error = "토스 live 를 켜려면 토스 키가 있어야 한다"
    elif binance_live and not (keys["binance"] and capital > 0):
        error = "Binance live 를 켜려면 Binance 키와 0 보다 큰 자본이 있어야 한다"
    if error:
        return render(request, "trading.html", status_code=400, **trading_context(me, error))
    with get_db() as d:
        accounts.update_live(d, me["id"], toss_live=toss_live, binance_live=binance_live, amount_scale=scale, binance_capital=capital)
    log.info("백오피스: %s live 설정 — 토스 %s · Binance %s · 배율 %g · 자본 %g", me["email"],
             "ON" if toss_live else "OFF", "ON" if binance_live else "OFF", scale, capital)
    return RedirectResponse("/trading", status_code=303)


@app.post("/trading/settings", dependencies=[Depends(admin_only)])
def trading_settings(request: Request, max_positions: int = Form(...), max_orders_per_day: int = Form(...),
                     daily_loss_limit_krw: float = Form(...), daily_loss_limit_usd: float = Form(...),
                     max_order_amount_krw: float = Form(...), max_order_amount_usd: float = Form(...)):
    values = {"max_positions": max_positions, "max_orders_per_day": max_orders_per_day,
              "daily_loss_limit_krw": daily_loss_limit_krw, "daily_loss_limit_usd": daily_loss_limit_usd,
              "max_order_amount_krw": max_order_amount_krw, "max_order_amount_usd": max_order_amount_usd}
    if any(v < 0 for v in values.values()):
        return render(request, "trading.html", status_code=400, **trading_context(request.state.me, "한도는 0 이상이어야 한다"))
    with get_db() as d:
        for k, v in values.items():
            db.set_setting(d, k, f"{v:g}")
    return RedirectResponse("/trading", status_code=303)


@app.post("/trading/orders/{intent_id}/cancel", response_class=HTMLResponse)
def trading_cancel(intent_id: str, me=Depends(require_session)):
    """미결 주문 수동 취소. live 는 그 주문 계정의 키로 실제 취소, 가상은 기록만 바꾼다.
    자기 계정 주문만 (관리자는 전부, 가상 장부는 관리자만)."""
    with get_db() as d:
        row = db.get_order(d, intent_id)
        if not row or row["status"] not in ("sent", "open"):
            return '<span class="warn">취소할 수 있는 상태가 아니다</span>'
        if me["role"] != "admin" and row.get("account_id") != me["id"]:
            raise HTTPException(403, "내 주문만 취소할 수 있다")
        if row["mode"] == "live":
            try:
                from ..trading.broker import TossOrderClient
                keys = accounts.load_keys(d, row["account_id"], "toss") if row.get("account_id") else None
                if not keys:
                    return '<span class="warn">이 주문 계정의 토스 키가 없다</span>'
                cli = TossReadOnlyClient(keys["client_id"], keys["client_secret"])
                cli.load_account()
                TossOrderClient(cli).cancel(row["order_id"])
            except (Exception, SystemExit) as e:
                return f'<span class="warn">취소 실패: {html.escape(str(e))}</span>'
        db.update_order(d, intent_id, status="canceled", reason="manual")
    return '<span class="ok">취소됨 (새로고침)</span>'


# -- 채널 ----------------------------------------------------------------------

def channel_rows() -> list:
    return [
        {"name": "telegram_public", "configured": bool(TG_PUBLIC_TOKEN and TG_PUBLIC_CHATS), "recipients": len(TG_PUBLIC_CHATS),
         "min_severity": "info", "detail": "공용 채널 — 시장 신호·시황·시스템·가상매매·성적표. 계정별 live 알림은 각 계정 텔레그램(내 API 키)"},
    ]


@app.get("/channels")
def channels_page():
    """옛 주소 — 채널 표는 알림(신호 이력) 탭으로 합쳤다."""
    return RedirectResponse("/signals", status_code=303)


@app.post("/channels/{name}/test", response_class=HTMLResponse, dependencies=[Depends(admin_only)])
def channel_test(name: str):
    """공용 채널로 테스트 발송. 쿨다운·등급을 무시하고 이력에는 남긴다. 계정 텔레그램 테스트는 내 API 키 화면에서."""
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


# -- 내 API 키 -------------------------------------------------------------------

KEY_FORMS = [
    {"name": "toss", "title": "토스증권", "note": "허용 IP 관리에 서버 공인 IP 를 등록해야 한다 (미등록이면 403)",
     "fields": [{"name": "client_id", "label": "client_id"}, {"name": "client_secret", "label": "client_secret"}]},
    {"name": "binance", "title": "Binance 선물", "note": "선물 거래 권한만 — 출금 권한은 켜지 않는다",
     "fields": [{"name": "api_key", "label": "API Key"}, {"name": "api_secret", "label": "Secret Key"}]},
    {"name": "telegram", "title": "텔레그램 (내 live 알림)", "note": "받는 채팅에서 봇에게 먼저 /start 를 보내야 한다",
     "fields": [{"name": "bot_token", "label": "봇 토큰"}, {"name": "chat_id", "label": "채팅 ID (숫자)"}]},
]


def account_context(me: dict, error: str = None) -> dict:
    with get_db() as d:
        status = accounts.key_status(d, me["id"])
    return {"providers": KEY_FORMS, "status": status, "error": error, "master_ok": bool(config.MASTER_KEY)}


@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request, me=Depends(require_session)):
    return render(request, "account.html", **account_context(me))


@app.post("/account/keys/{provider}")
async def account_save_keys(request: Request, provider: str, me=Depends(require_session)):
    if provider not in accounts.PROVIDERS:
        raise HTTPException(404)
    form = await request.form()
    fields = {f: form.get(f) for f in accounts.PROVIDERS[provider]}
    try:
        with get_db() as d:
            accounts.save_keys(d, me["id"], provider, fields)
    except (ValueError, CryptoError) as e:
        return render(request, "account.html", status_code=400, **account_context(me, str(e)))
    log.info("백오피스: %s %s 키 저장", me["email"], provider)
    return RedirectResponse("/account", status_code=303)


@app.post("/account/keys/{provider}/delete")
def account_delete_keys(provider: str, me=Depends(require_session)):
    if provider not in accounts.PROVIDERS:
        raise HTTPException(404)
    with get_db() as d:
        accounts.delete_keys(d, me["id"], provider)
    log.info("백오피스: %s %s 키 삭제", me["email"], provider)
    return RedirectResponse("/account", status_code=303)


def _test_keys(provider: str, keys: dict, email: str) -> tuple:
    """(성공 여부, 짧은 결과). 이 계정의 키로만 부른다. 결과 문구에 키 값이 섞이지 않게 한다."""
    if provider == "toss":
        from ..trading.broker import BrokerError, TossOrderClient
        try:
            cli = TossReadOnlyClient(keys["client_id"], keys["client_secret"])
            if not cli.load_account():
                return False, "BROKERAGE 계좌를 찾지 못했다"
            krw = TossOrderClient(cli).buying_power("KRW")
            return True, f"주문 API 접근 가능 · 매수가능금액 {krw:,.0f} KRW"
        except BrokerError as e:
            if e.code == "prerequisite-required":
                return False, "사전 자격 미충족 — 토스 WTS 에서 약관 동의·교육 이수·위험 고지를 완료해야 한다"
            return False, f"주문 API 오류: {e.code}"
        except (Exception, SystemExit) as e:              # 토큰 실패(403 허용 IP 등)는 SystemExit 로 올라온다
            return False, f"조회 실패: {e}"
    if provider == "binance":
        from ..binance_broker import BinanceFutures, BrokerError as BinanceError
        try:
            b = BinanceFutures(keys["api_key"], keys["api_secret"])
            b.sync_time()
            return True, f"선물 계좌 접근 가능 · 가용 {b.balance():,.2f} USDT"
        except BinanceError as e:
            return False, f"Binance 오류: {e}"
        except Exception as e:
            return False, f"연결 실패: {type(e).__name__}"
    from ..notify.telegram import TelegramChannel
    stamp = kst(datetime.now(timezone.utc).isoformat())
    out = TelegramChannel(keys["bot_token"], [keys["chat_id"]]).send(
        Signal("SYSTEM", "⚪ 시스템", "연결 확인", f"백오피스에서 보낸 테스트 ({stamp} KST) — {email}"))
    return out == "ok", out


@app.post("/account/keys/{provider}/test", response_class=HTMLResponse)
def account_test_keys(provider: str, me=Depends(require_session)):
    if provider not in accounts.PROVIDERS:
        raise HTTPException(404)
    try:
        with get_db() as d:
            keys = accounts.load_keys(d, me["id"], provider)
    except CryptoError as e:
        return f'<span class="warn">{html.escape(str(e))}</span>'
    if keys is None:
        return '<span class="warn">저장된 키가 없다</span>'
    ok, text = _test_keys(provider, keys, me["email"])
    return f'<span class="{"ok" if ok else "warn"}">{html.escape(text)}</span>'


# -- 계정 (관리자) ----------------------------------------------------------------

def accounts_context(error: str = None) -> dict:
    with get_db() as d:
        rows = accounts.list_accounts(d)
        keys = accounts.key_status(d)
    return {"rows": rows, "key_status": keys, "error": error, "admin_email": ADMIN_EMAIL}


@app.get("/accounts", response_class=HTMLResponse, dependencies=[Depends(admin_only)])
def accounts_page(request: Request):
    return render(request, "accounts.html", **accounts_context())


@app.post("/accounts", dependencies=[Depends(admin_only)])
def accounts_add(request: Request, email: str = Form(...), role: str = Form("member")):
    try:
        with get_db() as d:
            accounts.add_account(d, email, role)
    except ValueError as e:
        return render(request, "accounts.html", status_code=400, **accounts_context(str(e)))
    return RedirectResponse("/accounts", status_code=303)


def _target(d, account_id: int, me: dict) -> dict:
    """권한·중지를 바꿀 대상. 자기 자신과 ALERT_ADMIN_EMAIL 은 막는다 — 관리자가 스스로를 잠그는 사고 방지."""
    row = accounts.get(d, account_id)
    if row is None:
        raise HTTPException(404)
    if row["id"] == me["id"] or row["email"] == ADMIN_EMAIL:
        raise HTTPException(400, "자기 자신과 기본 관리자는 바꿀 수 없다")
    return row


@app.post("/accounts/{account_id}/active")
def accounts_toggle_active(account_id: int, me=Depends(admin_only)):
    with get_db() as d:
        row = _target(d, account_id, me)
        accounts.set_active(d, account_id, not row["active"])
    log.info("백오피스: %s 가 %s 계정을 %s", me["email"], row["email"], "중지" if row["active"] else "재개")
    return RedirectResponse("/accounts", status_code=303)


@app.post("/accounts/{account_id}/role")
def accounts_toggle_role(account_id: int, me=Depends(admin_only)):
    with get_db() as d:
        row = _target(d, account_id, me)
        accounts.set_role(d, account_id, "member" if row["role"] == "admin" else "admin")
    log.info("백오피스: %s 가 %s 계정 권한 변경", me["email"], row["email"])
    return RedirectResponse("/accounts", status_code=303)


@app.post("/accounts/{account_id}/live-off")
def accounts_live_off(account_id: int, me=Depends(admin_only)):
    """관리자 긴급 차단 — 끄기만 한다. 켜는 것은 그 계정 본인만 (자기 키·자기 돈)."""
    with get_db() as d:
        if accounts.get(d, account_id) is None:
            raise HTTPException(404)
        accounts.update_live(d, account_id, toss_live=False, binance_live=False)
    log.warning("백오피스: %s 가 계정 %s 의 live 매매를 껐다", me["email"], account_id)
    return RedirectResponse("/accounts", status_code=303)


# -- 운영 (관리자) — run.py 관리 프로세스 ------------------------------------------------

SERVICE_LABELS = {"engine": "주식 엔진", "binance": "코인 워커", "backoffice": "백오피스"}
STATE_LABELS = {"running": "실행 중", "stopping": "끄는 중", "stopped": "꺼짐", "backoff": "재시작 대기"}


def ops_context() -> dict:
    with get_db() as d:
        rows = db.list_services(d)
    services = [{"name": n, "row": rows.get(n), "age": age_sec(rows[n]["heartbeat_at"]) if n in rows else None}
                for n in supervisor.SERVICES]
    return {"services": services, "supervisor": rows.get("supervisor"), "labels": SERVICE_LABELS, "state_labels": STATE_LABELS,
            "alive": supervisor.supervisor_alive(rows, datetime.now(timezone.utc))}


@app.get("/ops", response_class=HTMLResponse, dependencies=[Depends(admin_only)])
def ops_page(request: Request):
    return render(request, "ops.html", **ops_context())


@app.get("/partials/ops", response_class=HTMLResponse, dependencies=[Depends(admin_only)])
def ops_partial(request: Request):
    return render(request, "_ops.html", **ops_context())


@app.post("/ops/{name}/{action}", response_class=HTMLResponse)
def ops_request(request: Request, name: str, action: str, me=Depends(admin_only)):
    """요청만 남긴다 — 관리 프로세스가 몇 초 안에 읽어 처리한다. 백오피스 끄기는 막는다 (이 화면도 같이 사라진다)."""
    if name not in supervisor.SERVICES or action not in supervisor.ACTIONS:
        raise HTTPException(404)
    if name == "backoffice" and action == "stop":
        raise HTTPException(400, "백오피스는 화면에서 끌 수 없다 — 콘솔에서 python run.py stop backoffice")
    with get_db() as d:
        db.request_service(d, name, action, me["email"])
    log.info("백오피스: %s 가 %s %s 요청", me["email"], name, action)
    return render(request, "_ops.html", **ops_context())
