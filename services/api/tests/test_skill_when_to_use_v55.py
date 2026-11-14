"""Skill frontmatter ``when_to_use`` 生效（对齐上游 ``lJs`` + ``ZNn``）——可失败判据。

上游 ``lme``（Skill 定义构建器）处理 ``when_to_use``：

1. ``$e=O.when_to_use!=null?String(O.when_to_use):void 0`` —— 非 null 时转字符串，
   null 时为 undefined。非字符串类型也转（``String()``）。
2. 返回的 skill 定义携带 ``whenToUse:$e``。
3. ``ZNn(e)`` 把 ``[name, description, whenToUse]`` 过滤掉空值后用空格拼接，
   这是系统提示里 Skill 清单的文本来源。
4. ``whenToUse`` 也是 Skill 进清单的独立判据（有 ``whenToUse`` 即使没 description
   也进清单）。

当前 kbgai ``skills_defs.py`` 把 ``when_to_use`` 放进 ``UNSUPPORTED_FIELDS``
（只进 ``ignored`` 审计、不生效）。本测试在实现改动前应当红。
"""

from __future__ import annotations

from pathlib import Path

from blank_api.services.agent.graph import initial_messages
from blank_api.services.skills_defs import discover_skills, load_skill
from blank_api.services.tools.skills import SkillCatalog


def _env(tmp_path: Path) -> dict[str, str]:
    """四个用户层目录指到空目录，插件根指到空。"""
    empty = tmp_path / "_no-user"
    empty.mkdir(exist_ok=True)
    return {
        "BLANK_USER_PLUGINS_DIR": str(empty / "plugins"),
        "BLANK_USER_COMMANDS_DIR": str(empty / "commands"),
        "BLANK_USER_SKILLS_DIR": str(empty / "skills"),
        "BLANK_USER_AGENTS_DIR": str(empty / "agents"),
        "BLANK_USER_OUTPUT_STYLES_DIR": str(empty / "styles"),
    }


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 判据 1：when_to_use 被解析进 SkillDefinition
# ---------------------------------------------------------------------------


def test_when_to_use_parsed_into_definition(tmp_path: Path) -> None:
    """Skill frontmatter 声明 when_to_use → 解析进 SkillDefinition.when_to_use。"""
    skill_dir = tmp_path / ".blank" / "skills" / "deploy"
    _write(
        skill_dir / "SKILL.md",
        "---\n"
        "description: 上线流程\n"
        "when_to_use: 部署生产环境时\n"
        "---\n"
        "正文",
    )

    skills = discover_skills(tmp_path, env=_env(tmp_path))

    assert "deploy" in skills.entries
    entry = skills.entries["deploy"]
    assert entry.when_to_use == "部署生产环境时"


def test_when_to_use_not_in_ignored(tmp_path: Path) -> None:
    """when_to_use 不再进 ignored 审计——它是生效字段，不是未支持字段。"""
    skill_dir = tmp_path / ".blank" / "skills" / "deploy"
    _write(
        skill_dir / "SKILL.md",
        "---\n"
        "description: 上线流程\n"
        "when_to_use: 部署生产环境时\n"
        "---\n"
        "正文",
    )

    skills = discover_skills(tmp_path, env=_env(tmp_path))
    entry = skills.entries["deploy"]

    assert "when_to_use" not in entry.ignored


def test_no_when_to_use_defaults_empty(tmp_path: Path) -> None:
    """Skill 没声明 when_to_use → when_to_use 为空串。"""
    skill_dir = tmp_path / ".blank" / "skills" / "no-when"
    _write(
        skill_dir / "SKILL.md",
        "---\ndescription: 无 when_to_use\n---\n正文",
    )

    skills = discover_skills(tmp_path, env=_env(tmp_path))

    assert "no-when" in skills.entries
    entry = skills.entries["no-when"]
    assert entry.when_to_use == ""


def test_when_to_use_non_string_is_stringified(tmp_path: Path) -> None:
    """上游 String(O.when_to_use)：非字符串类型也转成字符串。"""
    skill_dir = tmp_path / ".blank" / "skills" / "num-when"
    _write(
        skill_dir / "SKILL.md",
        "---\ndescription: 数字 when_to_use\nwhen_to_use: 123\n---\n正文",
    )

    skills = discover_skills(tmp_path, env=_env(tmp_path))
    entry = skills.entries["num-when"]

    assert entry.when_to_use == "123"


# ---------------------------------------------------------------------------
# 判据 2：when_to_use 从 SkillDefinition 传到 as_entry
# ---------------------------------------------------------------------------


def test_when_to_use_carried_in_as_entry(tmp_path: Path) -> None:
    """SkillDefinition.as_entry() 携带 when_to_use。"""
    skill_dir = tmp_path / ".blank" / "skills" / "entry-when"
    _write(
        skill_dir / "SKILL.md",
        "---\ndescription: 带 when_to_use\nwhen_to_use: 部署时\n---\n正文",
    )

    skills = discover_skills(tmp_path, env=_env(tmp_path))
    entry = skills.entries["entry-when"]
    entry_dict = entry.as_entry()

    assert "when_to_use" in entry_dict
    assert entry_dict["when_to_use"] == "部署时"


# ---------------------------------------------------------------------------
# 判据 3：when_to_use 进 SkillCatalog listing
# ---------------------------------------------------------------------------


def test_when_to_use_in_catalog_listing() -> None:
    """SkillCatalog.listing() 返回的条目包含 when_to_use。"""
    catalog = SkillCatalog(
        [
            {
                "name": "deploy",
                "instructions": "正文",
                "description": "上线流程",
                "when_to_use": "部署生产环境时",
            }
        ]
    )

    listing = catalog.listing()
    assert len(listing) == 1
    assert listing[0]["when_to_use"] == "部署生产环境时"


# ---------------------------------------------------------------------------
# 判据 4：when_to_use 进系统提示
# ---------------------------------------------------------------------------


def test_when_to_use_in_system_prompt_with_description() -> None:
    """description + when_to_use 同时存在时，两者都进系统提示清单。"""
    content = initial_messages(
        "上线",
        [],
        skill_catalog=[
            {
                "name": "deploy",
                "description": "上线流程",
                "when_to_use": "部署生产环境时",
            }
        ],
    )[0]["content"]

    assert "可用 Skill" in content
    assert "- deploy：上线流程" in content
    assert "部署生产环境时" in content


def test_when_to_use_in_system_prompt_without_description() -> None:
    """只有 when_to_use 没有 description 时，when_to_use 仍进系统提示。"""
    content = initial_messages(
        "上线",
        [],
        skill_catalog=[
            {
                "name": "deploy",
                "description": "",
                "when_to_use": "部署生产环境时",
            }
        ],
    )[0]["content"]

    assert "可用 Skill" in content
    assert "- deploy" in content
    assert "部署生产环境时" in content


def test_no_when_to_use_does_not_appear_in_prompt() -> None:
    """没有 when_to_use 时，系统提示清单不出现多余内容。"""
    content = initial_messages(
        "上线",
        [],
        skill_catalog=[{"name": "deploy", "description": "上线流程"}],
    )[0]["content"]

    assert "可用 Skill" in content
    assert "- deploy：上线流程" in content


# ---------------------------------------------------------------------------
# 判据 5：load_skill 单文件入口也解析 when_to_use
# ---------------------------------------------------------------------------


def test_load_skill_parses_when_to_use(tmp_path: Path) -> None:
    """load_skill（单文件入口）同样解析 when_to_use。"""
    skill_dir = tmp_path / "single-skill"
    _write(
        skill_dir / "SKILL.md",
        "---\ndescription: 单文件\nwhen_to_use: 单文件部署时\n---\n正文",
    )

    definition, errors, warnings = load_skill(
        skill_dir / "SKILL.md",
        source="project",
        project_root=tmp_path,
    )

    assert definition is not None
    assert definition.when_to_use == "单文件部署时"
