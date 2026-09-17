"""계정별 API 키 암호화 — AES-256-GCM.

DB 에는 암호문만 들어간다. 마스터 키는 .env ALERT_MASTER_KEY 에만 있어 DB 가 통째로 유출돼도 키를 풀 수 없다.
aad(추가 인증 데이터)에 계정·공급자를 묶어, 암호문 행을 다른 계정이나 공급자로 옮기면 복호화가 실패한다.

    python -m alertbot.crypto genkey     새 마스터 키 출력 (.env ALERT_MASTER_KEY 에 넣는다)

형식: "v1." + base64url(nonce 12바이트 ‖ 암호문 ‖ 태그 16바이트)
"""

import base64
import os
import sys

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import config

PREFIX = "v1."


class CryptoError(Exception):
    """키 없음·형식 오류·변조. 메시지에 평문이나 키를 넣지 않는다."""


def generate_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _cipher() -> AESGCM:
    raw = (config.MASTER_KEY or "").strip()
    if not raw:
        raise CryptoError("ALERT_MASTER_KEY 가 없다 — python -m alertbot.crypto genkey 로 만들어 .env 에 넣을 것")
    try:
        key = _b64decode(raw)
    except ValueError:
        raise CryptoError("ALERT_MASTER_KEY 형식 오류 (base64url 32바이트)") from None
    if len(key) != 32:
        raise CryptoError("ALERT_MASTER_KEY 는 32바이트여야 한다")
    return AESGCM(key)


def encrypt(plaintext: str, aad: str) -> str:
    nonce = os.urandom(12)
    sealed = _cipher().encrypt(nonce, plaintext.encode("utf-8"), aad.encode("utf-8"))
    return PREFIX + base64.urlsafe_b64encode(nonce + sealed).decode().rstrip("=")


def decrypt(token: str, aad: str) -> str:
    cipher = _cipher()
    if not token or not token.startswith(PREFIX):
        raise CryptoError("암호문 형식 오류")
    try:
        blob = _b64decode(token[len(PREFIX):])
        return cipher.decrypt(blob[:12], blob[12:], aad.encode("utf-8")).decode("utf-8")
    except (InvalidTag, ValueError):
        raise CryptoError("복호화 실패 — 마스터 키가 다르거나 암호문이 바뀌었다") from None


if __name__ == "__main__":
    if sys.argv[1:] != ["genkey"]:
        raise SystemExit("usage: python -m alertbot.crypto genkey")
    print(generate_key())
