"""백오피스 인증 — 로그인 강제, Google 콜백, 허용 계정, CSRF, 관리자/일반 권한, 내 API 키."""
import base64
import json
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

import alertbot.backoffice.app as A
import alertbot.backoffice.auth as AUTH
from alertbot import accounts as ACC
from alertbot import config, crypto
from alertbot import db as DBM
from tests.backoffice_login import logged_in


@pytest.fixture
def store(monkeypatch):
    s = DBM.DB.sqlite().init_schema()
    monkeypatch.setattr(A, "_store", s)
    monkeypatch.setattr(config, "MASTER_KEY", crypto.generate_key())
    for mod in (A, AUTH):
        monkeypatch.setattr(mod, "GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
        monkeypatch.setattr(mod, "GOOGLE_CLIENT_SECRET", "client-secret")
    return s


def id_token(**claims) -> str:
    body = {"aud": "cid.apps.googleusercontent.com", "iss": "https://accounts.google.com", "exp": 4102444800,
            "email": "Friend@Example.com", "email_verified": True, "sub": "sub-1", **claims}
    enc = base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=")
    return f"header.{enc}.signature"


def test_redirect_url_comes_from_env_and_sets_callback_path(store, monkeypatch):
    """리다이렉트 URL 은 .env OAUTH_GOOGLE_REDIRECT_URL 그대로 — 콜백 경로·접속 주소·Secure 쿠키가 여기서 정해진다."""
    assert AUTH.split_redirect("http://127.0.0.1:8000/auth/callback") == ("/auth/callback", "http://127.0.0.1:8000", False)
    assert AUTH.split_redirect("https://bo.example.com/oauth/google") == ("/oauth/google", "https://bo.example.com", True)
    assert AUTH.split_redirect("") == ("/auth/google/callback", "", False)
    monkeypatch.setattr(AUTH, "GOOGLE_REDIRECT_URL", "http://127.0.0.1:8000/auth/callback")
    monkeypatch.setattr(AUTH, "PUBLIC_ORIGIN", "http://127.0.0.1:8000")
    assert AUTH.redirect_uri() == "http://127.0.0.1:8000/auth/callback"
    # 로그인 버튼은 리다이렉트 URL 의 호스트에서 시작한다 — localhost 로 열었어도 state 쿠키가 콜백(127.0.0.1)에 실려 오게
    assert 'href="http://127.0.0.1:8000/auth/google"' in TestClient(A.app).get("/login").text


def test_everything_requires_login(store):
    c = TestClient(A.app)
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    r = c.get("/partials/status", headers={"HX-Request": "true"})
    assert r.status_code == 401 and r.headers["hx-redirect"] == "/login"         # HTMX 는 페이지 전체를 옮긴다
    assert c.post("/watchlist", data={"symbol": "X", "market": "US"}, follow_redirects=False).status_code == 303
    assert c.get("/docs").status_code == 404 and c.get("/openapi.json").status_code == 404
    r = c.get("/login")
    assert r.status_code == 200 and "Google 계정으로 로그인" in r.text and "로그아웃" not in r.text
    assert r.headers["x-frame-options"] == "DENY"


def test_google_login_only_for_registered_accounts(store, monkeypatch):
    ACC.add_account(store, "friend@example.com")
    seen = {}

    def fake_exchange(code, verifier):
        seen.update(code=code, verifier=verifier)
        return AUTH.claims_from_id_token(id_token(sub=seen.get("sub", "sub-1")))
    monkeypatch.setattr(AUTH, "exchange_code", fake_exchange)

    def login(client, **params):
        r = client.get("/auth/google", follow_redirects=False)
        url = urlparse(r.headers["location"])
        q = parse_qs(url.query)
        assert url.netloc == "accounts.google.com" and q["code_challenge_method"] == ["S256"]
        assert q["redirect_uri"] == [AUTH.redirect_uri()] and q["scope"] == ["openid email"]
        assert f"Path={AUTH.CALLBACK_PATH}" in r.headers["set-cookie"]               # state 쿠키는 콜백 경로에 실린다
        return client.get(AUTH.CALLBACK_PATH, params={"code": "c0de", "state": q["state"][0], **params},
                          follow_redirects=False)

    c = TestClient(A.app)
    r = login(c)
    assert r.status_code == 303 and r.headers["location"] == "/" and "alert_session" in r.cookies
    assert seen["code"] == "c0de" and len(seen["verifier"]) > 40
    assert "friend@example.com" in c.get("/").text                               # 세션으로 들어간다
    assert ACC.get_by_email(store, "friend@example.com")["google_sub"] == "sub-1"

    c2 = TestClient(A.app)
    r = c2.get(AUTH.CALLBACK_PATH, params={"code": "x", "state": "forged"}, follow_redirects=False)
    assert r.status_code == 403 and "만료" in r.text and "alert_session" not in r.cookies

    seen["sub"] = "someone-else"                                                   # 같은 이메일, 다른 구글 계정
    r = login(TestClient(A.app))
    assert r.status_code == 403 and "다른 계정" in r.text

    ACC.set_active(store, ACC.get_by_email(store, "friend@example.com")["id"], False)
    seen["sub"] = "sub-1"
    r = login(TestClient(A.app))
    assert r.status_code == 403 and "허용된 계정이 아니다" in r.text
    assert login(TestClient(A.app), error="access_denied").status_code == 403


def test_id_token_claims_are_checked(monkeypatch):
    monkeypatch.setattr(AUTH, "GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    assert AUTH.claims_from_id_token(id_token()) == {"email": "friend@example.com", "sub": "sub-1"}
    for bad in ({"aud": "other"}, {"iss": "https://evil.example"}, {"exp": 1}, {"email_verified": False}):
        with pytest.raises(AUTH.AuthError):
            AUTH.claims_from_id_token(id_token(**bad))
    with pytest.raises(AUTH.AuthError):
        AUTH.claims_from_id_token("garbage")


def test_csrf_roles_and_logout(store):
    admin = logged_in(A, store, "admin@example.com", "admin")
    member = logged_in(A, store, "member@example.com", "member")
    no_csrf = TestClient(A.app, cookies={"alert_session": ACC.create_session(store, ACC.get_by_email(store, "admin@example.com")["id"])[0]})
    assert no_csrf.post("/watchlist", data={"symbol": "AAPL", "market": "US"}).status_code == 403
    assert DBM.get_watch_row(store, "AAPL") is None

    assert member.post("/watchlist", data={"symbol": "AAPL", "market": "US"}).status_code == 403
    assert member.get("/accounts").status_code == 403
    page = member.get("/watchlist").text
    assert "관리자만" in page and 'action="/watchlist"' not in page and "계정</a>" not in page
    assert admin.post("/watchlist", data={"symbol": "AAPL", "market": "US", "enabled": "1"}, follow_redirects=False).status_code == 303

    r = admin.post("/accounts", data={"email": "New@Example.com", "role": "member"}, follow_redirects=False)
    assert r.status_code == 303 and ACC.get_by_email(store, "new@example.com")["role"] == "member"
    assert admin.post("/accounts", data={"email": "new@example.com"}).status_code == 400
    me = ACC.get_by_email(store, "admin@example.com")["id"]
    assert admin.post(f"/accounts/{me}/active").status_code == 400                # 자기 자신은 중지 못 한다
    target = ACC.get_by_email(store, "member@example.com")["id"]
    ACC.update_live(store, target, toss_live=True)
    admin.post(f"/accounts/{target}/live-off", follow_redirects=False)
    assert ACC.get(store, target)["toss_live"] is False
    admin.post(f"/accounts/{target}/active", follow_redirects=False)               # 중지 → 세션이 끊긴다
    assert member.get("/", follow_redirects=False).status_code == 303

    r = admin.post("/logout", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert admin.get("/", follow_redirects=False).status_code == 303


def test_my_api_keys_are_write_only(store, monkeypatch):
    c = logged_in(A, store, "me@example.com", "member")
    r = c.post("/account/keys/binance", data={"api_key": "AK-visible-check", "api_secret": "SK-very-secret"},
               follow_redirects=False)
    assert r.status_code == 303
    raw = store.fetchone("SELECT secret FROM alert_account_keys")["secret"]
    assert "SK-very-secret" not in raw and "AK-visible-check" not in raw
    page = c.get("/account").text
    assert "설정됨" in page and "SK-very-secret" not in page and "AK-visible-check" not in page
    assert c.post("/account/keys/binance", data={"api_key": "x", "api_secret": ""}).status_code == 400

    got = {}
    monkeypatch.setattr(A, "_test_keys", lambda p, keys, email: (got.update(keys) or True, "가용 12.00 USDT"))
    assert "가용 12.00 USDT" in c.post("/account/keys/binance/test").text and got["api_secret"] == "SK-very-secret"
    assert "저장된 키가 없다" in c.post("/account/keys/toss/test").text

    c.post("/account/keys/binance/delete", follow_redirects=False)
    assert ACC.key_status(store, ACC.get_by_email(store, "me@example.com")["id"])["binance"] is None
    assert c.post("/account/keys/nope").status_code == 404
