"""백오피스 계정 — 허용 이메일(로그인), 로그인 세션, 계정별 API 키(암호화), live 설정.

로그인은 Google OAuth 이고 여기 있는 활성 이메일만 들어온다(backoffice/auth.py). 계정마다 토스·Binance·텔레그램 키를
AES-GCM 으로 암호화해 두고(alertbot/crypto.py), live 모드 워커가 live_accounts 로 풀어서 그 계정 계좌에 주문하고 그 계정 텔레그램으로 알린다.
.env 의 공용 키(시세·가상매매·공개 채널)와는 섞지 않는다.
"""

import hashlib
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone

from . import crypto, db
from .config import SESSION_HOURS

log = logging.getLogger("scalper")

ROLES = ("admin", "member")
# 공급자 → 필드. 키를 지우면 그 공급자로 하는 live 가 멈추고, 텔레그램 키가 없으면 어떤 live 도 켤 수 없다(알림 없는 실매매 금지).
PROVIDERS = {
    "toss": ("client_id", "client_secret"),
    "binance": ("api_key", "api_secret"),
    "telegram": ("bot_token", "chat_id"),
}
LIVE_FIELDS = ("toss_live", "binance_live", "amount_scale", "binance_capital")

UPSERT_KEY = {
    "mysql": "INSERT INTO alert_account_keys (account_id, provider, secret, updated_at) VALUES (%s, %s, %s, %s) AS new "
             "ON DUPLICATE KEY UPDATE secret=new.secret, updated_at=new.updated_at",
    "sqlite": "INSERT INTO alert_account_keys (account_id, provider, secret, updated_at) VALUES (%s, %s, %s, %s) "
              "ON CONFLICT(account_id, provider) DO UPDATE SET secret=excluded.secret, updated_at=excluded.updated_at",
}


def _account(row):
    if row is None:
        return None
    row = dict(row)
    for k in ("amount_scale", "binance_capital"):
        row[k] = float(row[k] or 0)
    for k in ("active", "toss_live", "binance_live"):
        row[k] = bool(row[k])
    return row


# -- 계정 ----------------------------------------------------------------------

def get(store, account_id: int):
    return _account(store.fetchone("SELECT * FROM alert_accounts WHERE id = %s", (int(account_id),)))


def get_by_email(store, email: str):
    return _account(store.fetchone("SELECT * FROM alert_accounts WHERE email = %s", ((email or "").strip().lower(),)))


def list_accounts(store) -> list:
    return [_account(r) for r in store.fetchall("SELECT * FROM alert_accounts ORDER BY role, email")]


def add_account(store, email: str, role: str = "member") -> int:
    email = (email or "").strip().lower()
    if "@" not in email or " " in email or len(email) > 255:
        raise ValueError("이메일 형식이 아니다")
    if role not in ROLES:
        raise ValueError("권한은 admin 또는 member")
    if get_by_email(store, email):
        raise ValueError("이미 등록된 이메일")
    now = db._now()
    cur = store.execute("INSERT INTO alert_accounts (email, role, active, created_at, updated_at) VALUES (%s, %s, 1, %s, %s)",
                        (email, role, now, now))
    log.info("계정 추가: %s (%s)", email, role)
    return int(cur.lastrowid)


def ensure_admin(store, email: str):
    """ALERT_ADMIN_EMAIL 계정을 보장한다 — 없으면 만들고, 중지·강등돼 있으면 되돌린다. 관리자가 스스로를 잠글 수 없게 한다."""
    if not email:
        return None
    row = get_by_email(store, email)
    if row is None:
        return add_account(store, email, "admin")
    if row["role"] != "admin" or not row["active"]:
        store.execute("UPDATE alert_accounts SET role = 'admin', active = 1, updated_at = %s WHERE id = %s", (db._now(), row["id"]))
        log.warning("관리자 계정 복구: %s", email)
    return row["id"]


def set_active(store, account_id: int, active: bool):
    store.execute("UPDATE alert_accounts SET active = %s, updated_at = %s WHERE id = %s", (int(bool(active)), db._now(), int(account_id)))
    if not active:                                  # 중지하면 로그인 세션도 바로 끊는다
        store.execute("DELETE FROM alert_sessions WHERE account_id = %s", (int(account_id),))


def set_role(store, account_id: int, role: str):
    if role not in ROLES:
        raise ValueError("권한은 admin 또는 member")
    store.execute("UPDATE alert_accounts SET role = %s, updated_at = %s WHERE id = %s", (role, db._now(), int(account_id)))


def update_live(store, account_id: int, **fields):
    """live 스위치·금액 배율·Binance 자본. 워커의 자동 차단(연속 실패)도 이걸로 스위치를 끈다."""
    bad = set(fields) - set(LIVE_FIELDS)
    if bad:
        raise ValueError(f"알 수 없는 live 설정: {sorted(bad)}")
    if not fields:
        return
    values = {k: (int(bool(v)) if k in ("toss_live", "binance_live") else float(v)) for k, v in fields.items()}
    values["updated_at"] = db._now()
    sets = ", ".join(f"{k} = %s" for k in values)
    store.execute(f"UPDATE alert_accounts SET {sets} WHERE id = %s", (*values.values(), int(account_id)))


def record_login(store, account_id: int, sub: str):
    """첫 로그인의 Google 계정 고유 ID(sub)를 고정한다. 이후 같은 이메일이라도 sub 가 다르면 auth 가 거부한다."""
    store.execute("UPDATE alert_accounts SET google_sub = COALESCE(google_sub, %s), last_login_at = %s WHERE id = %s",
                  (sub, db._now(), int(account_id)))


def accounts_version(store) -> str:
    """계정·키가 바뀌었는지만 알면 된다 (워커 핫리로드). 두 표의 최종 수정 시각과 행 수."""
    a = store.fetchone("SELECT COALESCE(MAX(updated_at), '') AS u, COUNT(*) AS n FROM alert_accounts")
    k = store.fetchone("SELECT COALESCE(MAX(updated_at), '') AS u, COUNT(*) AS n FROM alert_account_keys")
    return f"{a['u']}:{a['n']}:{k['u']}:{k['n']}"


# -- API 키 (암호화) --------------------------------------------------------------

def _aad(account_id: int, provider: str) -> str:
    return f"alert:{int(account_id)}:{provider}"


def save_keys(store, account_id: int, provider: str, fields: dict):
    if provider not in PROVIDERS:
        raise ValueError(f"알 수 없는 공급자: {provider}")
    clean = {f: str(fields.get(f) or "").strip() for f in PROVIDERS[provider]}
    missing = [f for f, v in clean.items() if not v]
    if missing:
        raise ValueError(f"{provider} 키 항목이 비어 있다: {', '.join(missing)}")
    token = crypto.encrypt(json.dumps(clean), _aad(account_id, provider))
    store.execute(UPSERT_KEY[store.dialect], (int(account_id), provider, token, db._now()))
    log.info("계정 %s %s 키 저장", account_id, provider)       # 값은 절대 남기지 않는다


def load_keys(store, account_id: int, provider: str):
    """복호화한 필드 dict. 없으면 None. 마스터 키가 다르거나 변조됐으면 crypto.CryptoError."""
    row = store.fetchone("SELECT secret FROM alert_account_keys WHERE account_id = %s AND provider = %s", (int(account_id), provider))
    if row is None:
        return None
    return json.loads(crypto.decrypt(row["secret"], _aad(account_id, provider)))


def delete_keys(store, account_id: int, provider: str):
    store.execute("DELETE FROM alert_account_keys WHERE account_id = %s AND provider = %s", (int(account_id), provider))
    off = {"toss": {"toss_live": False}, "binance": {"binance_live": False},
           "telegram": {"toss_live": False, "binance_live": False}}.get(provider, {})
    update_live(store, account_id, **off)
    log.info("계정 %s %s 키 삭제", account_id, provider)


def key_status(store, account_id: int = None) -> dict:
    """{provider: 수정 시각|None}. account_id 가 없으면 {계정: {provider: 시각}} (관리자 계정 표)."""
    rows = store.fetchall("SELECT account_id, provider, updated_at FROM alert_account_keys"
                          + (" WHERE account_id = %s" if account_id is not None else ""),
                          (int(account_id),) if account_id is not None else ())
    out = {}
    for r in rows:
        out.setdefault(int(r["account_id"]), {})[r["provider"]] = r["updated_at"]
    if account_id is not None:
        got = out.get(int(account_id), {})
        return {p: got.get(p) for p in PROVIDERS}
    return out


def live_accounts(store, include_ids=()) -> list:
    """live 스위치가 하나라도 켜진 활성 계정과 복호화한 키. 워커가 계정별 실행기·트레이더를 만든다.

    include_ids 는 스위치·활성과 무관하게 더 넣을 계정 — 열린 live 포지션을 끝까지 감시해야 하는 계정 (Binance).
    키를 풀지 못한 공급자는 None 이고 error 에 사유가 남는다 (그 계정만 건너뛴다).
    """
    ids = [int(i) for i in include_ids]
    sql = "SELECT * FROM alert_accounts WHERE (active = 1 AND (toss_live = 1 OR binance_live = 1))"
    if ids:
        sql += f" OR id IN ({', '.join(['%s'] * len(ids))})"
    out = []
    for acc in store.fetchall(sql + " ORDER BY id", tuple(ids)):
        acc = _account(acc)
        item = {k: acc[k] for k in ("id", "email", "active", "toss_live", "binance_live", "amount_scale", "binance_capital")}
        item["error"] = None
        for provider in PROVIDERS:
            try:
                item[provider] = load_keys(store, acc["id"], provider)
            except (crypto.CryptoError, ValueError) as e:
                item[provider], item["error"] = None, f"{provider} 키 복호화 실패: {e}"
        out.append(item)
    return out


# -- 로그인 세션 ------------------------------------------------------------------

def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(store, account_id: int, hours: float = SESSION_HOURS) -> tuple:
    """(쿠키 토큰, CSRF 토큰). DB 에는 토큰의 sha256 만 남는다."""
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    store.execute("INSERT INTO alert_sessions (token_hash, account_id, csrf, created_at, expires_at) VALUES (%s, %s, %s, %s, %s)",
                  (_hash(token), int(account_id), csrf, now.isoformat(timespec="microseconds"),
                   (now + timedelta(hours=hours)).isoformat(timespec="microseconds")))
    return token, csrf


def session_account(store, token: str):
    """유효한 세션의 계정(+csrf). 만료됐거나 계정이 중지됐으면 None."""
    if not token:
        return None
    row = store.fetchone("SELECT a.*, s.csrf AS session_csrf FROM alert_sessions s JOIN alert_accounts a ON a.id = s.account_id "
                         "WHERE s.token_hash = %s AND s.expires_at > %s AND a.active = 1", (_hash(token), db._now()))
    return _account(row)


def delete_session(store, token: str):
    if token:
        store.execute("DELETE FROM alert_sessions WHERE token_hash = %s", (_hash(token),))


def purge_expired(store):
    store.execute("DELETE FROM alert_sessions WHERE expires_at <= %s", (db._now(),))
