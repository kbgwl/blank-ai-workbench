# -*- coding: utf-8 -*-
"""国内包镜像默认：子进程里的 pip / uv / npm 走境内源。

这是"适配国内应用"的一层：任务里一旦要装包，默认源在境外时第一步就可能超时，
模型会把超时读成"环境坏了"。只补缺省值，用户显式配置过的镜像不动。
"""
from __future__ import annotations

from blank_api.services.shell_env import (
    DOMESTIC_MIRRORS,
    apply_domestic_mirrors,
    capture,
    domestic_mirrors_enabled,
)


def test_missing_mirrors_are_added_without_mutating_input():
    source = {"PATH": "C:/bin"}
    out = apply_domestic_mirrors(source, enabled=True)

    assert out["PIP_INDEX_URL"] == DOMESTIC_MIRRORS["PIP_INDEX_URL"]
    assert out["UV_INDEX_URL"] == DOMESTIC_MIRRORS["UV_INDEX_URL"]
    assert out["npm_config_registry"] == DOMESTIC_MIRRORS["npm_config_registry"]
    assert source == {"PATH": "C:/bin"}


def test_explicit_mirror_wins():
    out = apply_domestic_mirrors(
        {"PIP_INDEX_URL": "https://mirror.example/simple"}, enabled=True
    )
    assert out["PIP_INDEX_URL"] == "https://mirror.example/simple"


def test_disable_switch(monkeypatch):
    assert apply_domestic_mirrors({"PATH": "x"}, enabled=False) == {"PATH": "x"}
    assert domestic_mirrors_enabled({"BLANK_DOMESTIC_MIRRORS": "0"}) is False
    assert domestic_mirrors_enabled({"BLANK_DOMESTIC_MIRRORS": "off"}) is False
    assert domestic_mirrors_enabled({}) is True


def test_capture_puts_mirrors_into_the_child_environment(tmp_path):
    snapshot = capture(tmp_path, env={"PATH": "C:/bin", "COMSPEC": "cmd.exe"})
    env = snapshot.env_for()

    assert env["PIP_INDEX_URL"].startswith("https://pypi.tuna.tsinghua.edu.cn")
    assert env["npm_config_registry"].startswith("https://registry.npmmirror.com")


def test_capture_keeps_user_selected_mirror(tmp_path):
    snapshot = capture(
        tmp_path,
        env={
            "PATH": "C:/bin",
            "COMSPEC": "cmd.exe",
            "PIP_INDEX_URL": "https://mirror.example/simple",
        },
    )
    assert snapshot.env_for()["PIP_INDEX_URL"] == "https://mirror.example/simple"
