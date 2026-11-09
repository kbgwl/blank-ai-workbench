"""Blank AI Workbench API service."""

from __future__ import annotations

import os
import re

# NO_PROXY 里写 `[::1]` 是 curl 的写法，httpx 却把它当普通主机名再套一层方括号
# （拼成 `all://*[::1]`），构造 AsyncClient 时就抛 `InvalidURL: Invalid port: ':1]'`——
# 代理一次都没用上，所有模型/通道调用先全炸。入口处把方括号去掉，httpx 会给裸 IPv6
# 自己补回方括号，绕过规则不变。
_BRACKETED_HOST = re.compile(r"^\[([0-9A-Fa-f:.]+)\](?::\d+)?$")


def _unbracket_ipv6_no_proxy(value: str) -> str:
    hosts = []
    for raw in value.split(","):
        host = raw.strip()
        match = _BRACKETED_HOST.match(host)
        if match:
            # 端口一并去掉：httpx 表达不了「某个 IPv6 的某个端口绕过」，
            # 保留整台主机绕过比让整条规则把客户端构造炸掉更接近原意。
            host = match.group(1)
        hosts.append(host)
    return ",".join(hosts)


def _normalize_no_proxy_env() -> None:
    for name in ("NO_PROXY", "no_proxy"):
        raw = os.environ.get(name)
        if not raw:
            continue
        fixed = _unbracket_ipv6_no_proxy(raw)
        if fixed != raw:
            os.environ[name] = fixed


_normalize_no_proxy_env()
