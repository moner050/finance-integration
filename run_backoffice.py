"""백오피스 진입점.

실행:  python run_backoffice.py   →  http://127.0.0.1:8000  (ALERT_BACKOFFICE_HOST / PORT 로 변경)
       보통은 python run.py 가 엔진·Binance 워커와 함께 띄우고 지킨다.
Google 로그인 설정(OAUTH_GOOGLE_CLIENT_ID/SECRET/REDIRECT_URL), 첫 관리자(ALERT_ADMIN_EMAIL), 키 암호화 마스터 키(ALERT_MASTER_KEY)가
하나라도 없으면 뜨지 않는다 — 인증 없는 백오피스는 열지 않는다.
"""

from urllib.parse import urlsplit

import uvicorn

from alertbot import lifecycle
from alertbot.backoffice.auth import START_PATH
from alertbot.config import (ADMIN_EMAIL, BACKOFFICE_HOST, BACKOFFICE_PORT, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET,
                             GOOGLE_REDIRECT_URL, MASTER_KEY, setup_logging)
from alertbot.crypto import CryptoError, encrypt

if __name__ == "__main__":
    setup_logging(None)
    missing = [k for k, v in (("OAUTH_GOOGLE_CLIENT_ID", GOOGLE_CLIENT_ID), ("OAUTH_GOOGLE_CLIENT_SECRET", GOOGLE_CLIENT_SECRET),
                              ("OAUTH_GOOGLE_REDIRECT_URL", GOOGLE_REDIRECT_URL),
                              ("ALERT_ADMIN_EMAIL", ADMIN_EMAIL), ("ALERT_MASTER_KEY", MASTER_KEY)) if not v]
    if missing:
        raise SystemExit(f".env 에 {', '.join(missing)} 가 없다 — 인증 없이는 백오피스를 띄우지 않는다")
    redirect = urlsplit(GOOGLE_REDIRECT_URL)
    if redirect.scheme not in ("http", "https") or not redirect.netloc or redirect.path in ("", "/", "/login", "/logout", START_PATH):
        raise SystemExit("OAUTH_GOOGLE_REDIRECT_URL 은 http(s)://호스트:포트/콜백경로 형식이어야 한다 (예: http://127.0.0.1:8000/auth/callback). "
                         f"콜백경로로 /, /login, /logout, {START_PATH} 는 쓸 수 없다")
    try:
        encrypt("check", "startup")                  # 마스터 키 형식 확인
    except CryptoError as e:
        raise SystemExit(str(e)) from e
    print(f"Google 로그인 리다이렉트 URL: {GOOGLE_REDIRECT_URL} — Google 콘솔의 승인된 리디렉션 URI 와 같아야 하고, "
          f"백오피스는 {redirect.scheme}://{redirect.netloc} 로 접속한다")
    server = uvicorn.Server(uvicorn.Config("alertbot.backoffice.app:app", host=BACKOFFICE_HOST, port=BACKOFFICE_PORT,
                                           log_level="info", timeout_graceful_shutdown=5))
    lifecycle.install(on_stop=lambda: setattr(server, "should_exit", True))     # run.py 가 띄웠으면 멈춤 요청에 서버를 내린다
    server.run()
