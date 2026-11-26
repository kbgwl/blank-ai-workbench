"""工作区边界：选 E 盘清理 C 盘、没选目录检查电脑，都不能靠猜。

真机问题：用户选了 E 盘目录，让 agent 清理 C 盘；agent 既不能假装看过，
也不能把工作区当整机。两个场景和纯聊天提示都要说清：
- 工作区只覆盖当前目录；工作区外的盘要用户授权（允许列表/切换工作区）。
- 清理先只读盘点、列清单、等明确同意；系统目录/用户目录/缓存不自动删。
- 没选目录时无法检查电脑；单个目录也只覆盖该目录。
"""

from __future__ import annotations

from blank_api.services.agent.chat import CHAT_ONLY_SYSTEM_RULE
from blank_api.services.agent.graph import compose_system_prompt


def test_both_scenes_explain_workspace_scope_and_safe_cleanup() -> None:
    for scene in ("coding", "daily"):
        prompt = compose_system_prompt(None, scene=scene)
        assert "工作区只覆盖当前目录" in prompt, scene
        assert "允许列表" in prompt, scene
        assert "清理磁盘先只读盘点" in prompt, scene
        assert "检查电脑问题" in prompt, scene
        assert "单目录 60 秒" in prompt, scene
        assert "不要为了汇总" in prompt, scene


def test_chat_only_explains_no_workspace_limits() -> None:
    assert "检查电脑/清理磁盘" in CHAT_ONLY_SYSTEM_RULE
    assert "单个目录只覆盖该目录" in CHAT_ONLY_SYSTEM_RULE
    assert "允许列表" in CHAT_ONLY_SYSTEM_RULE
