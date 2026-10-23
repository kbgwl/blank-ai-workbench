"""Logging helpers that keep credentials out of logs and responses."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

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


def mask(text: str) -> str:
    """先按"键=值"的形状打码（保留字段名，让人看得出自己在批什么），
    再把 redact 认不出的密钥形态整段抹掉。

    只做 redact 是不够的：`password = "..."` 这类形态 `contains_secret` 认得出、
    `redact` 却不改，边界上留了空子就等于没设边界。
    """
    out = redact(text)
    for pattern in _SECRET_SHAPES:
        out = pattern.sub("****", out)
    return out


def mask_deep(value: Any) -> Any:
    """递归打码任意 JSON 形状（事件载荷、审批卡的参数、工具摘要都从这里过）。"""
    if isinstance(value, str):
        return mask(value) if contains_secret(value) else value
    if isinstance(value, dict):
        return {key: mask_deep(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [mask_deep(item) for item in value]
    return value


def redact(text: str, extra_secrets: Iterable[str] = ()) -> str:
    out = text
    for secret in extra_secrets:
        if secret and len(secret) > 4:
            out = out.replace(secret, "****")
    out = _KEY_VALUE.sub(lambda m: f"{m.group(1)}{m.group(2)}****", out)
    return _TOKEN_LIKE.sub("sk-****", out)
