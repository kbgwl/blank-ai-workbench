"""edit_file 一次改多段（对标 Claude Edit 的 ``edits[]``）。

为什么值得单开一组判据：同一份文件连改三处，旧接口只能发三次调用，而每次调用
都是一次完整的模型往返。实测一轮 20 分钟的任务里，同一文件连发三条 edit_file
是最常见的形状。Claude 的 Edit 收 ``edits`` 数组，我们补上之后要钉住四件事：

1. 多段按数组顺序应用，后一段看得到前一段的结果（「先插入再改那一行」成立）。
2. 只写一次盘、只留一个备份：分三次替换不等于要留三份中间态。
3. 任何一段失败都必须整份不动，并且说清是第几段——否则模型会把整份 edits 重发。
4. 单段老写法（old_string/new_string）保持可用，已有调用方不必改。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.editing import _edit_file, _edit_on_disk, editing_tools
from blank_api.services.tools.file_state import FileStateCache


def _write(root: Path, relative: str, text: str) -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(text.encode("utf-8"))
    return target


def _read(cache: FileStateCache, root: Path, relative: str) -> None:
    raw = (root / relative).read_bytes()
    stat = (root / relative).stat()
    cache.record_read(
        relative,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        sha256=hashlib.sha256(raw).hexdigest() if stat.st_size <= 512 * 1024 else "",
    )


def _edits(root: Path, relative: str, pairs: list[tuple[str, str]], *, guard=None):
    return _edit_on_disk(
        root,
        "task-1",
        {
            "path": relative,
            "edits": [
                {"old_string": old, "new_string": new} for old, new in pairs
            ],
        },
        guard=guard,
    )


def _prepared(tmp_path: Path, text: str) -> tuple[FileStateCache, dict]:
    _write(tmp_path, "a.py", text)
    cache = FileStateCache()
    _read(cache, tmp_path, "a.py")
    return cache, cache.guard("a.py")


# --------------------------------------------------------------------------- #
# 顺序应用
# --------------------------------------------------------------------------- #


def test_two_edits_land_in_one_write(tmp_path: Path) -> None:
    cache, guard = _prepared(tmp_path, "a = 1\nb = 2\n")

    result = _edits(
        tmp_path, "a.py", [("a = 1", "a = 10"), ("b = 2", "b = 20")], guard=guard
    )

    assert result["replacements"] == 2
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "a = 10\nb = 20\n"


def test_a_later_edit_sees_what_an_earlier_one_wrote(tmp_path: Path) -> None:
    """先插入一行、再改这一行——第二段的 needle 只在第一段落盘之后才存在。"""
    cache, guard = _prepared(tmp_path, "def run():\n    pass\n")

    result = _edits(
        tmp_path,
        "a.py",
        [
            ("def run():\n    pass\n", "def run():\n    mark()\n    pass\n"),
            ("    mark()", "    mark(done=True)"),
        ],
        guard=guard,
    )

    assert result["replacements"] == 2
    assert "mark(done=True)" in (tmp_path / "a.py").read_text(encoding="utf-8")


def test_replace_all_inside_a_batch_counts_every_occurrence(tmp_path: Path) -> None:
    cache, guard = _prepared(tmp_path, "x = 0\ny = 0\nz = 1\n")

    result = _edit_on_disk(
        tmp_path,
        "task-1",
        {
            "path": "a.py",
            "edits": [
                {"old_string": " = 0", "new_string": " = 9", "replace_all": True},
                {"old_string": "z = 1", "new_string": "z = 2"},
            ],
        },
        guard=guard,
    )

    assert result["replacements"] == 3
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 9\ny = 9\nz = 2\n"


# --------------------------------------------------------------------------- #
# 失败必须整份不动，并说清第几段
# --------------------------------------------------------------------------- #


def test_a_missing_segment_reports_its_index_and_changes_nothing(tmp_path: Path) -> None:
    cache, guard = _prepared(tmp_path, "a = 1\nb = 2\n")

    result = _edits(
        tmp_path, "a.py", [("a = 1", "a = 10"), ("nope = 3", "nope = 4")], guard=guard
    )

    assert result["not_found"] is True
    assert result["index"] == 1
    assert result["total_edits"] == 2
    # 第一段虽然能匹配，但整份不落盘：半成品文件比没改更难收拾。
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "a = 1\nb = 2\n"


def test_an_ambiguous_segment_reports_its_index_and_changes_nothing(tmp_path: Path) -> None:
    cache, guard = _prepared(tmp_path, "a = 1\nsame\nsame\n")

    result = _edits(
        tmp_path, "a.py", [("a = 1", "a = 2"), ("same", "other")], guard=guard
    )

    assert result["ambiguous"] is True
    assert result["index"] == 1
    assert result["occurrences"] == 2
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "a = 1\nsame\nsame\n"


def test_only_one_backup_for_the_whole_batch(tmp_path: Path) -> None:
    cache, guard = _prepared(tmp_path, "a = 1\nb = 2\nc = 3\n")

    result = _edits(
        tmp_path,
        "a.py",
        [("a = 1", "a = 10"), ("b = 2", "b = 20"), ("c = 3", "c = 30")],
        guard=guard,
    )

    backup = tmp_path / result["backup_path"]
    assert backup.read_text(encoding="utf-8") == "a = 1\nb = 2\nc = 3\n"
    assert result["changed_lines"] >= 3


def test_an_empty_batch_is_rejected(tmp_path: Path) -> None:
    cache, guard = _prepared(tmp_path, "a = 1\n")

    result = _edit_on_disk(tmp_path, "task-1", {"path": "a.py", "edits": []}, guard=guard)

    assert result["missing_edits"] is True


def test_edits_win_over_the_single_pair_form(tmp_path: Path) -> None:
    cache, guard = _prepared(tmp_path, "a = 1\nb = 2\n")

    result = _edit_on_disk(
        tmp_path,
        "task-1",
        {
            "path": "a.py",
            "old_string": "a = 1",
            "new_string": "IGNORED",
            "edits": [{"old_string": "b = 2", "new_string": "b = 22"}],
        },
        guard=guard,
    )

    assert result["replacements"] == 1
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "a = 1\nb = 22\n"


# --------------------------------------------------------------------------- #
# 老写法与对外形状
# --------------------------------------------------------------------------- #


def test_the_single_pair_form_still_works(tmp_path: Path) -> None:
    cache, guard = _prepared(tmp_path, "a = 1\n")

    result = _edit_on_disk(
        tmp_path,
        "task-1",
        {"path": "a.py", "old_string": "a = 1", "new_string": "a = 2"},
        guard=guard,
    )

    assert result["replacements"] == 1
    assert "index" not in result


def test_the_error_message_names_the_segment(tmp_path: Path) -> None:
    """handler 层的回执要能让模型只修那一处，而不是把整份 edits 重发一遍。"""
    import asyncio

    cache, guard = _prepared(tmp_path, "a = 1\nb = 2\n")
    tool = next(t for t in editing_tools() if t.name == "edit_file")
    context = ToolContext(
        root=tmp_path,
        task_id="task-1",
        session_factory=None,  # type: ignore[arg-type]
        file_state=cache,
    )

    outcome = asyncio.run(
        _edit_file(
            context,
            {
                "path": "a.py",
                "edits": [
                    {"old_string": "a = 1", "new_string": "a = 2"},
                    {"old_string": "gone", "new_string": "x"},
                ],
            },
        )
    )

    assert outcome.ok is False
    assert "第 2 处" in outcome.message
    assert tool.input_schema["required"] == ["path"]
    assert "edits" in tool.input_schema["properties"]
