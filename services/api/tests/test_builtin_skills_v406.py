"""随包内置技能（对齐 Codex 的 core skills）：必须有内容、能被发现、能被覆盖。

背景：技能机制（清单进提示、正文按需加载、allowed-tools 收窄、paths 条件激活）
早就移植完了，但**随包一条技能都没有**——运行时目录里只有用户/项目自己写的，
所以技能页看起来只有几条记忆技能。这组测试钉住"随包内容"这件事本身。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import blank_api.services.skills_defs as skills_defs
from blank_api.services.skills_defs import (
    builtin_skills_dir,
    discover_library,
    discover_skills,
)

# 根 conftest 为了不让"随包内容"干扰其它用例，把内置目录换成了空目录。
# 这个文件测的正是随包内容本身，所以换回真实实现。
_REAL_BUILTIN = skills_defs.builtin_skills_dir


@pytest.fixture(autouse=True)
def _load_real_builtin(monkeypatch):
    monkeypatch.setattr(skills_defs, "builtin_skills_dir", _REAL_BUILTIN)


def _write_skill(root: Path, name: str, body: str) -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(body, encoding="utf-8")
    return directory


def test_builtin_dir_ships_real_skills() -> None:
    """随包技能不能是空目录：一个都没有就等于这个能力不存在。"""
    directory = builtin_skills_dir()
    assert directory.is_dir(), f"内置技能目录不存在：{directory}"
    names = {item.name for item in directory.iterdir() if (item / "SKILL.md").is_file()}
    # 不钉死具体名单（会加会删），只钉住底线与几条核心能力。
    assert len(names) >= 8, f"内置技能太少：{sorted(names)}"
    for required in ("code-review", "root-cause-debug", "verify-with-tests"):
        assert required in names, f"缺少核心技能 {required}"


def test_builtin_skills_parse_without_errors() -> None:
    """12 份内置技能全部可解析，且都写了 description / when_to_use。"""
    discovery = discover_library()
    builtin = {name: item for name, item in discovery.entries.items() if item.source == "builtin"}
    assert len(builtin) >= 8
    assert discovery.errors == [], discovery.errors
    for name, item in builtin.items():
        assert item.description, f"{name} 缺 description（模型靠它判断要不要用）"
        assert item.when_to_use, f"{name} 缺 when_to_use"
        assert item.instructions.strip(), f"{name} 正文为空"


def test_builtin_allowed_tools_all_exist(tmp_path: Path) -> None:
    """allowed-tools 写错一个名字，那一条技能就会把工具表收窄成空的。

    这是最容易犯且最难察觉的错：界面上技能还在，模型却没了工具。
    """
    from blank_api.services.tools import build_default_tools

    known = {tool.name for tool in build_default_tools()}
    discovery = discover_library()
    for name, item in discovery.entries.items():
        unknown = [tool for tool in item.allowed_tools if tool not in known]
        assert not unknown, f"{name} 引用了不存在的工具：{unknown}"


def test_project_skill_overrides_builtin(tmp_path: Path) -> None:
    """项目里自己写的同名技能必须盖掉随包那份（与 Claude 的层级一致）。"""
    project = tmp_path / "repo"
    _write_skill(
        project / ".blank" / "skills",
        "code-review",
        "---\nname: 我的审查\ndescription: 项目自己的审查口径\n---\n正文\n",
    )
    discovery = discover_skills(project)
    assert discovery.entries["code-review"].source == "project"
    assert discovery.entries["code-review"].description == "项目自己的审查口径"
    # 没被覆盖的内置技能仍然在
    assert discovery.entries["root-cause-debug"].source == "builtin"


def test_library_aggregates_every_workspace(tmp_path: Path) -> None:
    """多个工作区各有一份项目技能时，列表要把它们都算进来。

    只取"第一个工作区"会让另一个项目里写的技能凭空消失——用户在技能页
    找不到它，会以为没保存成功。
    """
    first = tmp_path / "a"
    second = tmp_path / "b"
    _write_skill(first / ".blank" / "skills", "db-migrate", "---\ndescription: A 项目的迁移流程\n---\n正文\n")
    _write_skill(second / ".blank" / "skills", "release", "---\ndescription: B 项目的发布流程\n---\n正文\n")
    discovery = discover_library([first, second])
    assert discovery.entries["db-migrate"].source == "project"
    assert discovery.entries["release"].source == "project"
    # 内置层不因扫了项目而消失
    assert discovery.entries["code-review"].source == "builtin"


def test_library_skips_plugin_layer() -> None:
    """技能页的目录不带插件：插件发现会创建 data 目录，只读接口不该有副作用。"""
    discovery = discover_library()
    assert all(not item.source.startswith("plugin:") for item in discovery.entries.values())
