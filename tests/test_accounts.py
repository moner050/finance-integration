"""계정·세션·암호화 키 — 메모리 SQLite 와 테스트용 마스터 키."""
import pytest

from alertbot import accounts as ACC
from alertbot import config, crypto
from alertbot import db as DBM


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setattr(config, "MASTER_KEY", crypto.generate_key())
    return DBM.DB.sqlite().init_schema()


def test_crypto_roundtrip_binds_aad_and_detects_tamper(monkeypatch):
    monkeypatch.setattr(config, "MASTER_KEY", crypto.generate_key())
    token = crypto.encrypt("비밀 값", "alert:1:toss")
    assert token.startswith("v1.") and "비밀" not in token
    assert crypto.decrypt(token, "alert:1:toss") == "비밀 값"
    assert crypto.encrypt("비밀 값", "alert:1:toss") != token                 # 매번 새 nonce
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt(token, "alert:2:toss")                                   # 다른 계정 행으로 옮긴 암호문
    tampered = token[:-2] + ("AA" if token[-2:] != "AA" else "BB")
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt(tampered, "alert:1:toss")
    monkeypatch.setattr(config, "MASTER_KEY", crypto.generate_key())         # 다른 마스터 키
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt(token, "alert:1:toss")
    monkeypatch.setattr(config, "MASTER_KEY", "")
    with pytest.raises(crypto.CryptoError, match="ALERT_MASTER_KEY"):
        crypto.encrypt("x", "a")


def test_accounts_admin_bootstrap_and_roles(store):
    admin = ACC.ensure_admin(store, "Boss@Example.com")
    assert ACC.get(store, admin)["role"] == "admin" and ACC.get_by_email(store, "boss@example.com")["id"] == admin
    member = ACC.add_account(store, "friend@example.com")
    with pytest.raises(ValueError, match="이미"):
        ACC.add_account(store, "FRIEND@example.com")
    with pytest.raises(ValueError):
        ACC.add_account(store, "not-an-email")
    ACC.set_role(store, admin, "member")
    ACC.set_active(store, admin, False)
    assert ACC.ensure_admin(store, "boss@example.com") == admin                # 강등·중지돼도 되돌린다
    assert ACC.get(store, admin)["role"] == "admin" and ACC.get(store, admin)["active"] is True
    assert [a["email"] for a in ACC.list_accounts(store)] == ["boss@example.com", "friend@example.com"]
    ACC.record_login(store, member, "sub-1")
    ACC.record_login(store, member, "sub-2")                                    # 첫 sub 로 고정
    assert ACC.get(store, member)["google_sub"] == "sub-1" and ACC.get(store, member)["last_login_at"]


def test_keys_are_encrypted_at_rest_and_gate_live(store, monkeypatch):
    acc = ACC.add_account(store, "me@example.com")
    v0 = ACC.accounts_version(store)
    ACC.save_keys(store, acc, "toss", {"client_id": "cid-123", "client_secret": "top-secret"})
    ACC.save_keys(store, acc, "telegram", {"bot_token": "1:AA", "chat_id": "42"})
    raw = store.fetchall("SELECT secret FROM alert_account_keys")
    assert all("top-secret" not in r["secret"] and "cid-123" not in r["secret"] for r in raw)
    assert ACC.load_keys(store, acc, "toss") == {"client_id": "cid-123", "client_secret": "top-secret"}
    assert ACC.load_keys(store, acc, "binance") is None
    assert ACC.key_status(store, acc)["toss"] and ACC.key_status(store, acc)["binance"] is None
    assert ACC.accounts_version(store) != v0
    with pytest.raises(ValueError, match="client_secret"):
        ACC.save_keys(store, acc, "toss", {"client_id": "x", "client_secret": " "})

    assert ACC.live_accounts(store) == []                                       # 스위치가 꺼져 있으면 워커 대상 아님
    ACC.update_live(store, acc, toss_live=True, amount_scale=0.5)
    [item] = ACC.live_accounts(store)
    assert item["toss"]["client_secret"] == "top-secret" and item["telegram"]["chat_id"] == "42"
    assert item["toss_live"] is True and item["amount_scale"] == 0.5 and item["error"] is None
    with pytest.raises(ValueError):
        ACC.update_live(store, acc, role="admin")

    ACC.delete_keys(store, acc, "telegram")                                     # 알림 키가 없으면 live 전부 꺼진다
    assert ACC.get(store, acc)["toss_live"] is False and ACC.live_accounts(store) == []

    # 마스터 키가 바뀌면 그 계정만 오류로 표시되고 예외는 새지 않는다
    ACC.update_live(store, acc, toss_live=True)
    monkeypatch.setattr(config, "MASTER_KEY", crypto.generate_key())
    [item] = ACC.live_accounts(store)
    assert item["toss"] is None and "복호화 실패" in item["error"]


def test_sessions_expire_and_follow_account_state(store):
    acc = ACC.add_account(store, "me@example.com")
    token, csrf = ACC.create_session(store, acc)
    raw = store.fetchone("SELECT token_hash FROM alert_sessions")
    assert raw["token_hash"] != token and len(raw["token_hash"]) == 64
    got = ACC.session_account(store, token)
    assert got["email"] == "me@example.com" and got["session_csrf"] == csrf
    assert ACC.session_account(store, "wrong") is None and ACC.session_account(store, "") is None
    ACC.set_active(store, acc, False)                                           # 중지하면 세션이 바로 사라진다
    assert ACC.session_account(store, token) is None
    ACC.set_active(store, acc, True)
    old, _ = ACC.create_session(store, acc, hours=-1)                           # 이미 만료
    assert ACC.session_account(store, old) is None
    ACC.purge_expired(store)
    assert store.fetchone("SELECT COUNT(*) AS n FROM alert_sessions")["n"] == 0
    token, _ = ACC.create_session(store, acc)
    ACC.delete_session(store, token)
    assert ACC.session_account(store, token) is None
