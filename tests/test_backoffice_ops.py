"""백오피스 '운영' — run.py 관리 프로세스의 서비스 상태와 제어 요청 (관리자 전용)."""
from datetime import datetime, timezone

import pytest

import alertbot.backoffice.app as A
from alertbot import db as DBM
from alertbot import supervisor as S
from tests.backoffice_login import logged_in


@pytest.fixture
def store(monkeypatch):
    store = DBM.DB.sqlite().init_schema()
    monkeypatch.setattr(A, "_store", store)
    DBM.ensure_services(store, [*S.SERVICES, "supervisor"])
    now = S.iso(datetime.now(timezone.utc))
    DBM.update_service(store, "supervisor", state="running", host="srv", pid=42, heartbeat_at=now, started_at=now)
    for name in S.SERVICES:
        DBM.update_service(store, name, state="running", pid=100, started_at=now, heartbeat_at=now)
    DBM.update_service(store, "engine", last_output="Traceback ...\nSystemExit: 토스 403")
    return store


def test_admin_sees_services_member_is_blocked(store):
    admin = logged_in(A, store)
    r = admin.get("/ops")
    assert r.status_code == 200 and 'hx-get="/partials/ops"' in r.text
    assert "주식 엔진" in r.text and "srv" in r.text and "토스 403" in r.text and "run.py 가 돌고 있지 않다" not in r.text
    assert 'href="/ops"' in admin.get("/").text
    member = logged_in(A, store, "friend@example.com", "member")
    assert member.get("/ops").status_code == 403 and member.get("/partials/ops").status_code == 403
    assert member.post("/ops/binance/restart").status_code == 403
    assert 'href="/ops"' not in member.get("/").text


def test_request_is_recorded_and_validated(store):
    admin = logged_in(A, store)
    r = admin.post("/ops/binance/restart")
    assert r.status_code == 200 and "요청 대기: restart" in r.text
    row = DBM.list_services(store)["binance"]
    assert (row["request"], row["requested_by"]) == ("restart", "admin@example.com")
    assert admin.post("/ops/backoffice/stop").status_code == 400          # 이 화면도 같이 사라진다
    assert admin.post("/ops/backoffice/restart").status_code == 200
    assert admin.post("/ops/nope/start").status_code == 404
    assert admin.post("/ops/engine/explode").status_code == 404


def test_csrf_required(store):
    admin = logged_in(A, store)
    del admin.headers["X-CSRF-Token"]
    assert admin.post("/ops/engine/stop").status_code == 403
    assert DBM.list_services(store)["engine"]["request"] is None


def test_stale_supervisor_shows_banner_and_hides_buttons(store):
    DBM.update_service(store, "supervisor", heartbeat_at="2026-01-01T00:00:00+00:00")
    r = logged_in(A, store).get("/partials/ops")
    assert "run.py 가 돌고 있지 않다" in r.text and "hx-post" not in r.text


# -- 종목 화면: 코인 급변 감시 추가·제외 --------------------------------------------------

def test_coin_scan_settings_on_watchlist(store):
    import json as _json
    from alertbot import binance_scan
    DBM.set_setting(store, binance_scan.SNAPSHOT_KEY, _json.dumps({
        "at": "2026-09-17T01:00:00+00:00", "top": [{"symbol": "BTCUSDT", "rank": 1, "quote_volume": 11821000000.0}],
        "include": ["LSKUSDT"], "exclude": [], "invalid": ["NOPEUSDT"], "symbols": ["BTCUSDT", "LSKUSDT"]}))
    admin = logged_in(A, store)
    r = admin.get("/watchlist")
    assert r.status_code == 200 and "코인 급변 감시" in r.text and "11,821,000,000" in r.text and "NOPEUSDT" in r.text
    assert "<td class=\"muted\">추가</td><td><strong>LSKUSDT</strong>" in r.text
    assert admin.post("/watchlist/coins", data={"include": "lsk, arb", "exclude": "ain"}, follow_redirects=False).status_code == 303
    s = DBM.get_settings(store)
    assert (s[binance_scan.INCLUDE_KEY], s[binance_scan.EXCLUDE_KEY]) == ("LSKUSDT,ARBUSDT", "AINUSDT")
    assert "LSKUSDT, ARBUSDT" in admin.get("/watchlist").text
    bad = admin.post("/watchlist/coins", data={"include": "lsk", "exclude": "LSKUSDT"})
    assert bad.status_code == 400 and "같은 코인" in bad.text
    member = logged_in(A, store, "friend@example.com", "member")
    assert member.post("/watchlist/coins", data={"include": "doge"}).status_code == 403
    assert "관리자만 바꿀 수 있다" in member.get("/watchlist").text
