"""技能安装器：边界比功能重要。

这是一个"从外部拿内容写进本机磁盘"的入口，所以每条限制都要有测试：
名字白名单、只能落在技能根内、体积与数量上限、zip 只解普通文件、
同名不覆盖、落盘前先解析一遍、来源戳。
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from blank_api.services import skill_install as si
from blank_api.services.skill_install import (
    SkillInstallError,
    install_skill,
    read_source_stamp,
    uninstall_skill,
)

GOOD = """---
name: 演示技能
description: 用来测安装流程
---
# 正文
按步骤做。
"""


@pytest.fixture(autouse=True)
def _user_root(monkeypatch, tmp_path):
    root = tmp_path / "user-skills"
    monkeypatch.setenv("BLANK_USER_SKILLS_DIR", str(root))
    return root


def _source(tmp_path: Path, name: str = "demo-skill", body: str = GOOD) -> Path:
    directory = tmp_path / "src" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(body, encoding="utf-8")
    return directory


def test_installs_a_local_directory_into_user_scope(tmp_path: Path, _user_root) -> None:
    source = _source(tmp_path)
    (source / "scripts").mkdir()
    (source / "scripts" / "helper.py").write_text("print('ok')\n", encoding="utf-8")

    result = install_skill(str(source), scope="user")

    assert result["name"] == "demo-skill"
    target = _user_root / "demo-skill"
    assert (target / "SKILL.md").is_file()
    assert (target / "scripts" / "helper.py").is_file()
    stamp = read_source_stamp(target)
    assert stamp["source"] == str(source)
    assert stamp["files"] >= 2


def test_rejects_bad_names_instead_of_sanitizing_into_something_else(tmp_path: Path, _user_root) -> None:
    source = _source(tmp_path)
    for bad in ("../escape", "a/b", "Upper Case", "x" * 80):
        with pytest.raises(SkillInstallError) as exc:
            install_skill(str(source), scope="user", name=bad)
        assert exc.value.code in ("skill_name_invalid", "skill_path_escape")
    assert not (_user_root.parent / "escape").exists()


def test_refuses_to_overwrite_without_explicit_flag(tmp_path: Path, _user_root) -> None:
    source = _source(tmp_path)
    install_skill(str(source), scope="user", name="demo")
    with pytest.raises(SkillInstallError) as exc:
        install_skill(str(source), scope="user", name="demo")
    assert exc.value.code == "skill_exists"
    assert exc.value.status_code == 409


def test_overwrite_replaces_the_whole_directory(tmp_path: Path, _user_root) -> None:
    source = _source(tmp_path)
    install_skill(str(source), scope="user", name="demo")
    # 旧版本独有的文件不能留下来：半新半旧的技能比装错更难查。
    (source / "old-only.txt").write_text("v1", encoding="utf-8")
    install_skill(str(source), scope="user", name="demo", overwrite=True)
    assert (_user_root / "demo" / "old-only.txt").is_file()

    (source / "old-only.txt").unlink()
    install_skill(str(source), scope="user", name="demo", overwrite=True)
    assert not (_user_root / "demo" / "old-only.txt").exists()


def test_zip_with_path_traversal_is_rejected(tmp_path: Path, _user_root) -> None:
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("SKILL.md", GOOD)
        zf.writestr("../escaped.txt", "x")
    with pytest.raises(SkillInstallError) as exc:
        install_skill(str(archive), scope="user", name="evil")
    assert exc.value.code == "skill_archive_unsafe"
    assert not (_user_root.parent / "escaped.txt").exists()


def test_zip_symlink_is_rejected(tmp_path: Path, _user_root) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("SKILL.md", GOOD)
        info = zipfile.ZipInfo("link")
        # unix mode 0o120777 = 符号链接，zip 用 external_attr 高 16 位存它。
        info.external_attr = (0o120777 << 16)
        zf.writestr(info, "/etc/passwd")
    archive = tmp_path / "link.zip"
    archive.write_bytes(buffer.getvalue())
    with pytest.raises(SkillInstallError) as exc:
        install_skill(str(archive), scope="user", name="linked")
    assert exc.value.code == "skill_archive_symlink"


def test_source_without_skill_md_is_rejected(tmp_path: Path, _user_root) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "README.md").write_text("not a skill", encoding="utf-8")
    with pytest.raises(SkillInstallError) as exc:
        install_skill(str(bare), scope="user", name="bare")
    assert exc.value.code == "skill_entry_missing"


def test_unparsable_skill_md_is_rejected(tmp_path: Path, _user_root) -> None:
    source = _source(tmp_path, body="---\nname: x\n---\n")  # 有 frontmatter 没正文
    with pytest.raises(SkillInstallError) as exc:
        install_skill(str(source), scope="user", name="empty")
    assert exc.value.code in ("skill_body_empty", "skill_unparsable")
    assert not (_user_root / "empty").exists()


def test_project_scope_needs_a_workspace(tmp_path: Path) -> None:
    source = _source(tmp_path)
    with pytest.raises(SkillInstallError) as exc:
        install_skill(str(source), scope="project", project_root="")
    assert exc.value.code == "skill_project_root_missing"


def test_project_scope_writes_under_dot_blank(tmp_path: Path) -> None:
    source = _source(tmp_path)
    workspace = tmp_path / "repo"
    workspace.mkdir()
    install_skill(str(source), scope="project", project_root=workspace, name="demo")
    assert (workspace / ".blank" / "skills" / "demo" / "SKILL.md").is_file()


def test_uninstall_only_removes_real_skill_dirs(tmp_path: Path, _user_root) -> None:
    source = _source(tmp_path)
    install_skill(str(source), scope="user", name="demo")
    uninstall_skill("demo", scope="user")
    assert not (_user_root / "demo").exists()

    # 没有 SKILL.md 的目录不许删：防止手滑把别的目录清掉。
    plain = _user_root / "not-a-skill"
    plain.mkdir(parents=True)
    with pytest.raises(SkillInstallError) as exc:
        uninstall_skill("not-a-skill", scope="user")
    assert exc.value.code == "skill_not_removable"
    assert plain.is_dir()


def test_size_caps_are_enforced(tmp_path: Path, _user_root, monkeypatch) -> None:
    source = _source(tmp_path)
    (source / "big.bin").write_bytes(b"0" * 4096)
    monkeypatch.setattr(si, "MAX_FILE_BYTES", 1024)
    with pytest.raises(SkillInstallError) as exc:
        install_skill(str(source), scope="user", name="big")
    assert exc.value.code == "skill_file_too_large"
