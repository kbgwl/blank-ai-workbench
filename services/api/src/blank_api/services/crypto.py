"""Symmetric encryption for stored credentials."""

from __future__ import annotations

import base64
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from blank_api.api.errors import ApiError
from blank_api.config import get_settings

_KEY_FILE = Path("data/.blank_secret.key")


def _load_key() -> bytes:
    settings = get_settings()
    if settings.secret_key:
        raw = base64.urlsafe_b64decode(settings.secret_key.encode())
        if len(raw) != 32:
            raise ApiError(code="secret_key_invalid", message="BLANK_SECRET_KEY 长度错误", status_code=500)
        return settings.secret_key.encode()
    if _KEY_FILE.exists():
        return _KEY_FILE.read_bytes().strip()
    _KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    _KEY_FILE.write_bytes(key)
    os.chmod(_KEY_FILE, 0o600)
    return key


def _fernet() -> Fernet:
    return Fernet(_load_key())


def encrypt_secret(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        raise ApiError(
            code="secret_undecryptable",
            message="密钥无法解密，可能主密钥已变更，请重新录入 API Key",
            status_code=500,
        ) from None


def key_hint(plaintext: str) -> str:
    """Non-reversible display hint; never log or return the secret itself."""
    if not plaintext:
        return ""
    tail = plaintext[-4:] if len(plaintext) > 4 else "*"
    return f"****{tail}"
