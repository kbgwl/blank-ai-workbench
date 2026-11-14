"""嵌套 Skill 目录批次（对齐上游 Nkn）——可失败判据。

上游 ``Nkn``（插件技能加载器）的语义：

1. **先检查插件技能目录自身有没有 SKILL.md**。有 → 整个目录就是一个
   Skill，立即返回（不扫描子目录）；名字是 ``${pluginName}:${name}``，
   ``name`` 取 frontmatter 的 ``name``，缺省时取目录名。
2. **没有 SKILL.md** → ``readdir`` 扫描子目录，逐个加载。

上游 ``Fkn``（用户技能加载器）**不做根目录 SKILL.md 检查**——只扫描子目录。

当前 kbgai ``discover_skills`` 只调 ``_skill_dirs(plugin_dir)`` 扫描子目录，
从不检查插件技能目录自身有没有 SKILL.md。本测试在实现改动前应当红。
"""

from __future__ import annotations

from pathlib import Path

from blank_api.services.skills_defs import discover_skills


def _env(tmp_path: Path, *, plugins: Path | None = None) -> dict[str, str]:
    """四个用户层目录指到空目录，插件根按需指定。"""
    empty = tmp_path / "_no-user"
    empty.mkdir(exist_ok=True)
    return {
        "BLANK_USER_PLUGINS_DIR": str(plugins or empty / "plugins"),
        "BLANK_USER_COMMANDS_DIR": str(empty / "commands"),
        "BLANK_USER_SKILLS_DIR": str(empty / "skills"),
        "BLANK_USER_AGENTS_DIR": str(empty / "agents"),
        "BLANK_USER_OUTPUT_STYLES_DIR": str(empty / "styles"),
    }


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _plugin(root: Path, name: str) -> Path:
    """建一个插件包，返回插件根路径。"""
    plugin = root / ".blank" / "plugins" / name
    _write(
        plugin / ".claude-plugin" / "plugin.json",
        '{"name": "%s"}' % name,
    )
    return plugin


# ---------------------------------------------------------------------------
# 判据 1：插件技能目录根即技能（对齐上游 Nkn）
# ---------------------------------------------------------------------------


def test_plugin_skills_dir_root_skill_is_discovered(tmp_path: Path) -> None:
    """插件 skills/ 目录自身有 SKILL.md → 整目录就是一个 Skill。"""
    plugin = _plugin(tmp_path, "my-plugin")
    _write(
        plugin / "skills" / "SKILL.md",
        "---\nname: root-skill\ndescription: 根技能\n---\n根正文",
    )

    skills = discover_skills(tmp_path, env=_env(tmp_path))

    # 上游 Nkn：名字是 `pluginName:nameFromFrontmatter`
    assert list(skills.entries) == ["my-plugin:root-skill"]
    entry = skills.entries["my-plugin:root-skill"]
    assert entry.source == "plugin:my-plugin"
    assert entry.instructions == "根正文"
    assert entry.description == "根技能"


def test_plugin_skills_dir_root_skill_defaults_to_dirname(tmp_path: Path) -> None:
    """插件技能目录根 SKILL.md 无 frontmatter name → 名字取目录名。"""
    plugin = _plugin(tmp_path, "deploy-plugin")
    # 目录名是 "skills"，但上游 Nkn 取的是插件名 + 目录名？
    # 实际上上游 Nkn: Re = frontmatter name 或 JQ(e)（e 是 skills 目录路径，
    # JQ 取目录名）。所以缺省名是 "skills"，全名字是 "deploy-plugin:skills"。
    _write(
        plugin / "skills" / "SKILL.md",
        "---\ndescription: 无名字\n---\n正文",
    )

    skills = discover_skills(tmp_path, env=_env(tmp_path))

    assert list(skills.entries) == ["deploy-plugin:skills"]


# ---------------------------------------------------------------------------
# 判据 2：根有 SKILL.md 时不扫描子目录（对齐上游 Nkn 立即返回）
# ---------------------------------------------------------------------------


def test_plugin_skills_root_skill_skips_subdirs(tmp_path: Path) -> None:
    """插件技能目录根有 SKILL.md → 子目录技能不再被加载（Nkn 立即返回）。"""
    plugin = _plugin(tmp_path, "my-plugin")
    _write(
        plugin / "skills" / "SKILL.md",
        "---\nname: root\ndescription: 根\n---\n根正文",
    )
    _write(
        plugin / "skills" / "sub-skill" / "SKILL.md",
        "---\nname: sub\ndescription: 子\n---\n子正文",
    )

    skills = discover_skills(tmp_path, env=_env(tmp_path))

    # 上游 Nkn 有根 SKILL.md 时立即返回，不扫描子目录
    assert list(skills.entries) == ["my-plugin:root"]
    assert "my-plugin:sub" not in skills.entries


# ---------------------------------------------------------------------------
# 判据 3：无根 SKILL.md 时正常扫描子目录（现有行为不回退）
# ---------------------------------------------------------------------------


def test_plugin_skills_no_root_skill_scans_subdirs(tmp_path: Path) -> None:
    """插件技能目录根无 SKILL.md → 子目录技能正常加载（现有行为）。"""
    plugin = _plugin(tmp_path, "my-plugin")
    _write(
        plugin / "skills" / "sub-one" / "SKILL.md",
        "---\nname: one\ndescription: 第一个\n---\n正文一",
    )
    _write(
        plugin / "skills" / "sub-two" / "SKILL.md",
        "---\nname: two\ndescription: 第二个\n---\n正文二",
    )

    skills = discover_skills(tmp_path, env=_env(tmp_path))

    assert list(skills.entries) == ["my-plugin:sub-one", "my-plugin:sub-two"]
    assert skills.entries["my-plugin:sub-one"].instructions == "正文一"
    assert skills.entries["my-plugin:sub-two"].instructions == "正文二"


# ---------------------------------------------------------------------------
# 判据 4：用户层不检查根 SKILL.md（对齐上游 Fkn）
# ---------------------------------------------------------------------------


def test_user_skills_dir_root_skill_not_discovered(tmp_path: Path) -> None:
    """用户技能目录自身有 SKILL.md → 不被当作技能（Fkn 不检查根）。"""
    user_skills = tmp_path / "_user_skills"
    _write(
        user_skills / "SKILL.md",
        "---\nname: root\ndescription: 根\n---\n根正文",
    )
    env = _env(tmp_path)
    env["BLANK_USER_SKILLS_DIR"] = str(user_skills)

    skills = discover_skills(tmp_path, env=env)

    # 上游 Fkn 只扫描子目录，不检查根 SKILL.md
    assert list(skills.entries) == []


def test_project_skills_dir_root_skill_not_discovered(tmp_path: Path) -> None:
    """项目技能目录自身有 SKILL.md → 不被当作技能（只扫描子目录）。"""
    project_skills = tmp_path / ".blank" / "skills"
    _write(
        project_skills / "SKILL.md",
        "---\nname: root\ndescription: 根\n---\n根正文",
    )

    skills = discover_skills(tmp_path, env=_env(tmp_path))

    assert list(skills.entries) == []


# ---------------------------------------------------------------------------
# 判据 5：多个 skills 目录混合
# ---------------------------------------------------------------------------


def test_plugin_multiple_skills_dirs_mixed(tmp_path: Path) -> None:
    """插件有多个 skills 目录：一个根技能、一个子目录技能，各自独立加载。"""
    plugin = _plugin(tmp_path, "mix-plugin")
    _write(
        plugin / "skills" / "SKILL.md",
        "---\nname: root\ndescription: 根\n---\n根正文",
    )
    # 另一个 skills 目录（manifest 里声明）没有根 SKILL.md
    _write(
        plugin / "extra-skills" / "sub" / "SKILL.md",
        "---\nname: sub\ndescription: 子\n---\n子正文",
    )
    # 声明第二个 skills 目录（路径必须以 ./ 开头）
    _write(
        plugin / ".claude-plugin" / "plugin.json",
        '{"name": "mix-plugin", "skills": ["./skills", "./extra-skills"]}',
    )

    skills = discover_skills(tmp_path, env=_env(tmp_path))

    # skills/ 目录根有 SKILL.md → 整目录是技能
    assert "mix-plugin:root" in skills.entries
    # extra-skills/ 目录根无 SKILL.md → 扫描子目录
    assert "mix-plugin:sub" in skills.entries
