"""NO_PROXY 带方括号 IPv6 时，客户端构造不能整条炸掉（v0.11.2 全量回归发现）。"""

from __future__ import annotations

import os
import subprocess
import sys

from blank_api import _unbracket_ipv6_no_proxy

PROBE = """
import os
import blank_api  # noqa: F401  入口处归一化
from blank_api.services.models.base import pooled_client

client = pooled_client(5.0)
patterns = sorted(getattr(key, "pattern", str(key)) for key in client._mounts)
assert "all://[::1]" in patterns, patterns
assert os.environ["NO_PROXY"] == "localhost,::1,127.0.0.1", os.environ["NO_PROXY"]
print("OK", patterns)
"""


def test_bracketed_ipv6_entries_are_unbracketed() -> None:
    assert _unbracket_ipv6_no_proxy("[::1]") == "::1"
    assert _unbracket_ipv6_no_proxy("localhost,[::1],127.0.0.0/8") == "localhost,::1,127.0.0.0/8"
    assert _unbracket_ipv6_no_proxy("[::1]:8080") == "::1"
    assert _unbracket_ipv6_no_proxy("[2001:db8::1]") == "2001:db8::1"


def test_every_other_form_is_left_alone() -> None:
    for value in ("", "::1", "localhost", ".example.com", "127.0.0.0/8", "http://[::1]:8080", "*"):
        assert _unbracket_ipv6_no_proxy(value) == value


def test_client_builds_when_no_proxy_has_bracketed_ipv6() -> None:
    # 真环境子进程：不靠本进程已 import 过的模块状态，独立证明"带方括号也不炸"。
    env = {
        key: value
        for key, value in os.environ.items()
        if key.lower() not in {"no_proxy", "http_proxy", "https_proxy", "all_proxy"}
    }
    env["NO_PROXY"] = "localhost,[::1],127.0.0.1"
    completed = subprocess.run(
        [sys.executable, "-c", PROBE], capture_output=True, text=True, env=env, timeout=120
    )
    assert completed.returncode == 0, completed.stderr
    assert "OK" in completed.stdout
