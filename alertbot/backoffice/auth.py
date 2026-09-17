"""백오피스 로그인 — Google OAuth (Authorization Code + PKCE + state).

흐름: /auth/google 이 state·code_verifier 를 짧은 쿠키에 담고 구글로 보낸다 → 구글이 .env OAUTH_GOOGLE_REDIRECT_URL(콜백)로 돌려보내면
state 를 쿠키와 대조하고 code 를 토큰 엔드포인트에서 id_token 으로 바꾼다 → 이메일이 alert_accounts 의 활성 계정이면 세션을 만든다.
id_token 은 TLS 로 구글 토큰 엔드포인트에서 직접 받은 것이라 서명 대신 aud·iss·exp·email_verified 를 검사한다 (OpenID Connect Core 3.1.3.7).
세션·CSRF 확인과 권한 검사는 DB 가 필요해서 app.py 의 의존성(require_session·require_csrf·require_admin)에 있다.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import urlencode, urlsplit

import requests

from ..config import GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URL

SESSION_COOKIE = "alert_session"
OAUTH_COOKIE = "alert_oauth"
OAUTH_TTL_SEC = 600
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
ISSUERS = ("https://accounts.google.com", "accounts.google.com")
START_PATH = "/auth/google"
DEFAULT_CALLBACK_PATH = "/auth/google/callback"     # OAUTH_GOOGLE_REDIRECT_URL 이 비었을 때(테스트). run_backoffice 는 비어 있으면 뜨지 않는다


def split_redirect(url: str) -> tuple:
    """리다이렉트 URL → (콜백 경로, 브라우저가 접속하는 origin, https 여부)."""
    u = urlsplit(url)
    return u.path or DEFAULT_CALLBACK_PATH, f"{u.scheme}://{u.netloc}" if u.netloc else "", u.scheme == "https"


CALLBACK_PATH, PUBLIC_ORIGIN, SECURE_COOKIE = split_redirect(GOOGLE_REDIRECT_URL)
PUBLIC_PATHS = ("/login", START_PATH, CALLBACK_PATH)


class AuthError(Exception):
    """로그인 실패 사유 — 화면에 그대로 보여 준다. 토큰·코드 값은 넣지 않는다."""


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def redirect_uri() -> str:
    """.env OAUTH_GOOGLE_REDIRECT_URL 그대로. 요청 Host 헤더로 만들지 않는다 — Google 콘솔에 등록한 주소와 정확히 같아야 한다."""
    return GOOGLE_REDIRECT_URL


def login_url() -> str:
    """로그인 버튼 주소 — 리다이렉트 URL 과 같은 호스트에서 시작한다. state 쿠키는 호스트별이라 localhost 에서 시작해
    127.0.0.1 콜백으로 돌아오면 쿠키가 실려 오지 않는다."""
    return PUBLIC_ORIGIN + START_PATH


def start_login() -> tuple:
    """(구글 인증 URL, OAUTH_COOKIE 값). 쿠키에는 state 와 PKCE code_verifier 가 들어간다 (HttpOnly, 10분)."""
    state, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    params = {"client_id": GOOGLE_CLIENT_ID, "redirect_uri": redirect_uri(), "response_type": "code",
              "scope": "openid email", "state": state, "prompt": "select_account",
              "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256"}
    return f"{AUTH_URL}?{urlencode(params)}", f"{state}.{verifier}"


def verifier_for(cookie_value: str, state: str) -> str:
    """콜백의 state 가 우리가 보낸 것인지 확인하고 code_verifier 를 돌려준다."""
    saved_state, _, verifier = (cookie_value or "").partition(".")
    if not saved_state or not verifier or not state or not hmac.compare_digest(saved_state, state):
        raise AuthError("로그인 요청이 만료됐거나 일치하지 않는다 — 다시 로그인할 것")
    return verifier


def claims_from_id_token(id_token: str, now: float = None) -> dict:
    try:
        payload = id_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (AttributeError, IndexError, ValueError):
        raise AuthError("구글 응답 형식 오류") from None
    now = time.time() if now is None else now
    if claims.get("aud") != GOOGLE_CLIENT_ID or claims.get("iss") not in ISSUERS:
        raise AuthError("구글 토큰의 발급 대상이 이 백오피스가 아니다")
    if float(claims.get("exp") or 0) < now:
        raise AuthError("구글 토큰이 만료됐다")
    if claims.get("email_verified") not in (True, "true") or not claims.get("email") or not claims.get("sub"):
        raise AuthError("구글 계정 이메일이 확인되지 않았다")
    return {"email": str(claims["email"]).lower(), "sub": str(claims["sub"])}


def exchange_code(code: str, verifier: str) -> dict:
    """code → {email, sub}. 실패하면 AuthError. 클라이언트 비밀은 요청 본문에만 실리고 로그·메시지에 남기지 않는다."""
    try:
        r = requests.post(TOKEN_URL, data={"code": code, "client_id": GOOGLE_CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET,
                                           "redirect_uri": redirect_uri(), "grant_type": "authorization_code",
                                           "code_verifier": verifier}, timeout=10)
    except requests.RequestException as e:
        raise AuthError(f"구글 토큰 엔드포인트 연결 실패 ({type(e).__name__})") from None
    if r.status_code != 200:
        try:
            reason = f" {r.json().get('error')}" if r.json().get("error") else ""
        except ValueError:
            reason = ""
        raise AuthError(f"구글 토큰 교환 실패 (HTTP {r.status_code}{reason})")
    return claims_from_id_token(r.json().get("id_token") or "")
