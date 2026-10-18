"""Symmetric encryption for stored credentials."""

from __future__ import annotations

import base64
import os
import shutil
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from blank_api.api.errors import ApiError
from blank_api.config import get_settings

# 开发态默认落在 cwd 下；桌面壳用 BLANK_SECRET_KEY_FILE 指到用户数据目录。
_LEGACY_KEY_FILE = Path("data/.blank_secret.key")


def _key_path() -> Path:
    settings = get_settings()
    if settings.secret_key_file:
        return Path(settings.secret_key_file)
    return _LEGACY_KEY_FILE


def _migrate_legacy(target: Path) -> None:
    """目标位置还没有主密钥时，把老位置（包内 data/）的那份接过来。

    数据库在用户数据目录、主密钥在包内是历史遗留的错配：换包后 Key 就解不开了。
    这里做一次无痛搬迁，老用户重装后不用重新录 API Key。
    """
    try:
        if target.exists() or _LEGACY_KEY_FILE.resolve() == target.resolve():
            return
        if not _LEGACY_KEY_FILE.exists():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_LEGACY_KEY_FILE, target)
        os.chmod(target, 0o600)
    except OSError:
        # 搬迁失败不致命，下面会重新生成一份，用户重录 Key 即可
        pass


def _load_key() -> bytes:
    settings = get_settings()
    if settings.secret_key:
        raw = base64.urlsafe_b64decode(settings.secret_key.encode())
        if len(raw) != 32:
            raise ApiError(code="secret_key_invalid", message="BLANK_SECRET_KEY 长度错误", status_code=500)
        return settings.secret_key.encode()
    path = _key_path()
    _migrate_legacy(path)
    if path.exists():
        return path.read_bytes().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    path.write_bytes(key)
    os.chmod(path, 0o600)
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
