"""백오피스 테스트 공용 — 로그인한 TestClient (세션 쿠키 + CSRF 헤더)."""
from fastapi.testclient import TestClient

from alertbot import accounts as ACC


def logged_in(app_module, store, email: str = "admin@example.com", role: str = "admin") -> TestClient:
    acc = ACC.get_by_email(store, email)
    account_id = acc["id"] if acc else ACC.add_account(store, email, role)
    token, csrf = ACC.create_session(store, account_id)
    return TestClient(app_module.app, cookies={"alert_session": token}, headers={"X-CSRF-Token": csrf})
