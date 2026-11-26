# -*- coding: utf-8 -*-
"""开局环境预检：探测、渲染与提示注入。

真实台账（2026-11-23 写项目计划书）：模型为找一个能装包的 Python 连试十条命令。
预检的目标就是把这些「探测空转」从任务里拿掉：一次问清，写进系统提示。
"""
from __future__ import annotations

from blank_api.services.agent import env_profile as ep
from blank_api.services.agent.graph import initial_messages


def _fake_run(argv, *, cwd, env):
    code = argv[-1]
    if "import sys" in code:
        if argv[0].endswith("py.exe") or argv[0] == "py":
            return 0, "3.12.10\n25.0.1\ndocx"
        return 0, "3.12.14\n\n"
    if argv[:2] == ["node", "--version"]:
        return 0, "v20.11.1"
    if argv[:2] == ["git", "--version"]:
        return 0, "git version 2.47.1.windows.1"
    return 127, ""


def _fake_which(name, env):
    return {
        "python": "python.exe",
        "py": "py.exe",
        "node": "node",
        "git": "git",
    }.get(name, "")


def test_collect_reports_versions_and_pip(monkeypatch):
    monkeypatch.setattr(ep, "_run", _fake_run)
    monkeypatch.setattr(ep, "_which", _fake_which)
    monkeypatch.setattr("blank_api.services.shell_env.detect_venv", lambda root: "")

    info = ep._collect("C:/ws", {"PATH": "C:/bin"})

    assert info["python"] == {"version": "3.12.14", "pip": "", "libs": []}
    assert info["py3"] == {"version": "3.12.10", "pip": "25.0.1", "libs": ["docx"]}
    assert info["venv"] == {}
    assert info["node"] == "20.11.1"
    assert info["git"] == "2.47.1.windows.1"
    assert info["uv"] == ""


def test_render_block_points_to_pip_capable_interpreter(monkeypatch):
    monkeypatch.setattr(ep, "_run", _fake_run)
    monkeypatch.setattr(ep, "_which", _fake_which)
    monkeypatch.setattr("blank_api.services.shell_env.detect_venv", lambda root: "")

    block = ep.render_block(ep._collect("C:/ws", {"PATH": "C:/bin"}))

    assert "py -3: 3.12.10（pip 25.0.1 可用）" in block
    assert "python: 3.12.14（没有 pip" in block
    assert "文档库已装: py -3: python-docx" in block
    assert "装包失败两次就停" in block
    assert "不要再" in block and "逐条探测" in block
    # 预检结果会进模型上下文：只报版本，不报主机路径。
    assert "C:/bin" not in block


def test_render_block_without_any_pip_says_do_not_install(monkeypatch):
    monkeypatch.setattr(ep, "_run", lambda argv, **kwargs: (0, "3.12.14\n"))
    monkeypatch.setattr(ep, "_which", lambda name, env: "python.exe" if name == "python" else "")
    monkeypatch.setattr("blank_api.services.shell_env.detect_venv", lambda root: "")

    block = ep.render_block(ep._collect("C:/ws", {"PATH": ""}))
    assert "所有解释器都没有 pip" in block


def test_profile_caches_per_workspace_and_path(monkeypatch):
    calls = {"count": 0}

    def fake_collect(root, env):
        calls["count"] += 1
        return {"os": "test"}

    monkeypatch.setattr(ep, "_collect", fake_collect)
    ep._cache.clear()

    ep.profile("C:/ws", {"PATH": "A"}, now=100.0)
    ep.profile("C:/ws", {"PATH": "A"}, now=101.0)
    assert calls["count"] == 1
    ep.profile("C:/ws", {"PATH": "A"}, now=100.0 + ep.CACHE_TTL_SECONDS + 1)
    assert calls["count"] == 2
    ep.profile("C:/ws", {"PATH": "B"}, now=100.0 + ep.CACHE_TTL_SECONDS + 2)
    assert calls["count"] == 3


def test_environment_block_lands_in_the_system_message(monkeypatch):
    messages = initial_messages(
        "写一份计划书",
        [],
        environment_block="- 系统: Windows 11；shell: cmd.exe\n- py -3: 3.12.10（pip 25.0.1 可用）",
    )
    system = messages[0]["content"]
    assert "本机环境（开局实测" in system
    assert "py -3: 3.12.10" in system
    assert "不要再用 where" in system
