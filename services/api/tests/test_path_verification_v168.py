"""TOCTOU / 重解析点防线：预检看的是路径，open 有可能已经走到别处。

`resolve_inside` / `resolve_for_write` 到真正 open 之间有一段窗口：目录能被换成指向
工作区外的 junction（Windows）或符号链接（POSIX）。这一批的判据不是"路径字符串还在
工作区里"，而是**句柄打开的到底是哪个对象**——`open_verified` 打开后按句柄取最终路径
复验。所以这里的用例统一先造好真目录（预检会通过），再把目录换成链接，然后
monkeypatch 掉预检，模拟"预检被绕过/发生在换链接之前"，只看句柄层能不能拦住。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from blank_api.api.errors import ApiError
from blank_api.services import workspace
from blank_api.services.tools import patch as patch_module


def _plant_link(link: Path, target: Path) -> None:
    """junction（Windows，非管理员可用）或符号链接（POSIX）。"""
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, text=True, encoding="mbcs", errors="replace",
        )
        assert result.returncode == 0, result.stdout + result.stderr
    else:
        link.symlink_to(target, target_is_directory=True)


def _swap_dir_for_link(root: Path, outside: Path) -> Path:
    """`sub/` 先是真的目录（预检能过），随后换成指向 outside 的链接。"""
    real = root / "sub"
    real.mkdir()
    (real / "a.txt").write_text("INSIDE", encoding="utf-8")
    outside.mkdir()
    (outside / "a.txt").write_text("OUTSIDE-SECRET", encoding="utf-8")
    real.rename(root / "sub-old")
    _plant_link(real, outside)
    return real


def _bypass_precheck(monkeypatch) -> None:
    """只保留"拼接路径"这一半，模拟预检发生在换链接之前。"""
    monkeypatch.setattr(workspace, "resolve_inside", lambda root, relative: Path(root).resolve() / relative)
    monkeypatch.setattr(patch_module, "resolve_for_write", lambda root, relative: Path(root).resolve() / relative)


def test_normal_read_still_works(tmp_path) -> None:
    """对照：没被换过的文件必须照常读到，否则下面几条红说明不了任何事。"""
    root = tmp_path / "ws"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "a.txt").write_bytes(b"hello\n")
    result = workspace.read_text_file(root, "pkg/a.txt")
    assert result["content"] == "hello\n"
    assert result["binary"] is False


def test_read_refuses_a_directory_turned_into_a_link(tmp_path, monkeypatch) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "outside"
    _swap_dir_for_link(root, outside)
    _bypass_precheck(monkeypatch)
    with pytest.raises(ApiError) as info:
        workspace.read_text_file(root, "sub/a.txt")
    assert info.value.code == "path_escape_denied", info.value
    assert "OUTSIDE-SECRET" not in str(info.value)


def test_patch_refuses_to_write_through_the_link(tmp_path, monkeypatch) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "outside"
    _swap_dir_for_link(root, outside)
    _bypass_precheck(monkeypatch)
    patch_text = "*** Begin Patch\n*** Update File: sub/a.txt\n@@\n-INSIDE\n+CHANGED\n*** End Patch"
    with pytest.raises(ApiError) as info:
        patch_module._patch_on_disk(root, "task-1", {"path": "sub/a.txt", "patch": patch_text})
    assert info.value.code == "path_escape_denied", info.value
    assert (outside / "a.txt").read_text(encoding="utf-8") == "OUTSIDE-SECRET"


def test_patch_does_not_leave_a_new_file_outside(tmp_path, monkeypatch) -> None:
    """新建分支：O_EXCL 建出来的那个空文件必须被删掉，外面不能留痕。"""
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "outside"
    _swap_dir_for_link(root, outside)
    _bypass_precheck(monkeypatch)
    patch_text = "*** Begin Patch\n*** Add File: sub/new.txt\n+hello\n*** End Patch"
    with pytest.raises(ApiError) as info:
        patch_module._patch_on_disk(root, "task-1", {"path": "sub/new.txt", "patch": patch_text})
    assert info.value.code == "path_escape_denied", info.value
    assert not (outside / "new.txt").exists()


def test_backup_cannot_be_written_through_the_link(tmp_path, monkeypatch) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    _plant_link(root / ".blank_backups", outside)
    _bypass_precheck(monkeypatch)
    with pytest.raises(ApiError) as info:
        patch_module.write_backup(root, "task-1", "a.txt", b"backup")
    assert info.value.code == "path_escape_denied", info.value
    assert [path for path in outside.rglob("*") if path.is_file()] == []


def test_patch_and_restore_round_trip_inside_the_workspace(tmp_path) -> None:
    """接线后的正常路径：改完能回滚，内容一字不差。"""
    root = tmp_path / "ws"
    root.mkdir()
    (root / "a.txt").write_text("old\n", encoding="utf-8")
    patch_text = "*** Begin Patch\n*** Update File: a.txt\n@@ -1,1 +1,1 @@\n-old\n+new\n*** End Patch"
    result = patch_module._patch_on_disk(root, "task-1", {"path": "a.txt", "patch": patch_text})
    assert (root / "a.txt").read_text(encoding="utf-8") == "new\n"
    assert result["backup_path"].startswith(".blank_backups/task-1/")
    patch_module.restore_backup(root, result["backup_path"])
    assert (root / "a.txt").read_text(encoding="utf-8") == "old\n"
