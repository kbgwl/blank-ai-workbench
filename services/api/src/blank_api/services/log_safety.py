"""Logging helpers that keep credentials out of logs and responses."""

from __future__ import annotations

import re
from collections.abc import Iterable

_KEY_VALUE = re.compile(r"(?i)(api[_-]?key|authorization|x-api-key|bearer)(\s*[:=]\s*)(\S+)")
_TOKEN_LIKE = re.compile(r"sk-[A-Za-z0-9_\-]{8,}")

# 明文密钥形态：写入工作区时需要升级为高风险，由人工审批决定去留。
_SECRET_SHAPES = (
    re.compile(r"(?i)\b(api[_-]?key|secret|access[_-]?token|password|passwd|credential)s?\b\s*[:=]\s*['\"]?[A-Za-z0-9+/_\-.]{12,}"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


def contains_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in _SECRET_SHAPES)


def redact(text: str, extra_secrets: Iterable[str] = ()) -> str:
    out = text
    for secret in extra_secrets:
        if secret and len(secret) > 4:
            out = out.replace(secret, "****")
    out = _KEY_VALUE.sub(lambda m: f"{m.group(1)}{m.group(2)}****", out)
    return _TOKEN_LIKE.sub("sk-****", out)
