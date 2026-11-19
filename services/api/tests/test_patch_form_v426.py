"""apply_patch 的写盘形态：别把 LF 文件写成 CRLF，也别把 BOM 弄丢。

原来的实现用 ``io.TextIOWrapper(handle, newline=None)`` 读写：读时 CRLF→LF，
写时按 ``os.linesep`` 还原。在 Windows 上这就等于"一律写成 CRLF"——

* 一个 LF 仓库里的文件被打一次补丁，整份变成 CRLF（``.sh`` 直接跑不起来）；
* 备份也跟着变成 CRLF（备份的是归一后的文本，不是磁盘上那份字节）；
* BOM 挂在第 1 行行首，补丁的"第 1 行"永远对不上，工具却报"未变更"——
  一次真的修改被静默吞掉，比报错难查得多。

三条写路径（Edit / Write / Patch）现在共用一份 ``conform_written``：
形态只有一处实现，才不会出现"这条路径对、那条路径错"。
"""

from __future__ import annotations

from pathlib import Path

from blank_api.services.tools.patch import _patch_on_disk

PATCH = (
    "--- a/x.py\n"
    "+++ b/x.py\n"
    "@@ -1,3 +1,3 @@\n"
    " a = 1\n"
    "-b = 2\n"
    "+b = 9\n"
    " c = 3\n"
)


def _put(root: Path, relative: str, raw: bytes) -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    return target


def _patch(root: Path, relative: str, patch: str = PATCH) -> dict:
    return _patch_on_disk(root, "task-1", {"patch": patch, "path": relative})


def test_lf_file_stays_lf_after_a_patch(workspace):
    target = _put(workspace, "lf.py", b"a = 1\nb = 2\nc = 3\n")

    result = _patch(workspace, "lf.py")

    assert target.read_bytes() == b"a = 1\nb = 9\nc = 3\n"
    assert result["changed_lines"] == 2


def test_crlf_file_stays_crlf_after_a_patch(workspace):
    target = _put(workspace, "crlf.py", b"a = 1\r\nb = 2\r\nc = 3\r\n")

    result = _patch(workspace, "crlf.py")

    assert target.read_bytes() == b"a = 1\r\nb = 9\r\nc = 3\r\n"
    assert result["changed_lines"] == 2, "只改了一行，不能报整文件"


def test_bom_is_kept_and_the_patch_actually_applies(workspace):
    """BOM 文件上补丁必须真的生效——原来它会静默变成"未变更"。"""
    target = _put(workspace, "bom.py", "\ufeffa = 1\nb = 2\nc = 3\n".encode("utf-8"))

    result = _patch(workspace, "bom.py")

    assert "unchanged" not in result
    assert target.read_bytes() == "\ufeffa = 1\nb = 9\nc = 3\n".encode("utf-8")


def test_backup_keeps_the_original_bytes(workspace):
    """备份要能原样还原：原来存的是换行归一后的文本，CRLF 文件还原回来是 LF。"""
    _put(workspace, "crlf.py", b"a = 1\r\nb = 2\r\nc = 3\r\n")

    result = _patch(workspace, "crlf.py")

    backup = workspace / result["backup_path"]
    assert backup.read_bytes() == b"a = 1\r\nb = 2\r\nc = 3\r\n"


def test_new_file_is_written_with_lf(workspace):
    _patch(
        workspace,
        "new.py",
        "*** Begin Patch\n*** Add File: new.py\n+a = 1\n+b = 2\n*** End Patch",
    )

    assert b"\r\n" not in (workspace / "new.py").read_bytes()


def test_a_patch_that_changes_nothing_says_so(workspace):
    """真的没变才报未变更——这条和上一条 BOM 判据是一对，防止把"没生效"说成"没变化"。"""
    _put(workspace, "same.py", b"a = 1\nb = 2\nc = 3\n")
    noop = PATCH.replace("-b = 2\n+b = 9\n", "-b = 2\n+b = 2\n")

    result = _patch(workspace, "same.py", noop)

    assert result.get("unchanged") is True
