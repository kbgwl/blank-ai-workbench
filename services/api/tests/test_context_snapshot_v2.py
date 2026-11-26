"""NEXT-DEV-v2 任务一：文件状态快照 + 低于水位也做确定性瘦身。

两件事必须同时成立：
1. 旧 read_file 结果能换成「看快照 / 需要时重读」的指针，原文不丢（磁盘还在）；
2. 大窗口下水位永远到不了时，旧写参数仍会被折叠，并且记账写明是 micro:below。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from blank_api.services.agent.context import ContextCompactor, _microcompact
from blank_api.services.tools.file_state import FileStateCache


def _read_call(call_id: str, path: str) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": "read_file", "arguments": json.dumps({"path": path})},
            }
        ],
    }


def test_file_state_snapshot_lists_lines_op_and_survives_restore(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("a = 1\nb = 2\n", encoding="utf-8")
    stat = target.stat()
    cache = FileStateCache()
    cache.record_read(
        "a.py",
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        sha256="abcdef0123456789" * 4,
        lines=2,
    )

    snapshot = cache.render_snapshot()
    assert "a.py" in snapshot
    assert "2 行" in snapshot
    assert "最近读取" in snapshot
    assert "sha abcdef01" in snapshot

    exported = cache.export()["a.py"]
    assert exported["lines"] == 2 and exported["last_op"] == "read"
    restored = FileStateCache()
    restored.restore(cache.export())
    assert restored.render_snapshot() == snapshot

    cache.record_write(tmp_path, "a.py")
    assert "最近写入" in cache.render_snapshot()
    assert cache.export()["a.py"]["lines"] == 2


def test_snapshot_carries_content_summary_and_write_counts(tmp_path: Path) -> None:
    """快照不只说"多大"，还要能让模型认出内容，并给出本次写入的累计行数。"""
    target = tmp_path / "a.py"
    target.write_text("def greet():\n    return 1\n", encoding="utf-8")
    cache = FileStateCache()
    cache.record_write(tmp_path, "a.py", added=3, removed=1)

    snapshot = cache.render_snapshot()

    assert "摘要：def greet():" in snapshot
    assert "本次 1 次写入 +3/-1" in snapshot

    cache.record_write(tmp_path, "a.py", added=1, removed=0)
    assert "本次 2 次写入 +4/-1" in cache.render_snapshot()
    # 摘要与计数也要过检查点，否则恢复后模型又回到"只看见一个大小"。
    restored = FileStateCache()
    restored.restore(cache.export())
    assert restored.render_snapshot() == cache.render_snapshot()


def test_content_summary_is_one_line_and_bounded() -> None:
    from blank_api.services.tools.file_state import content_summary

    assert content_summary("\n\n  hello   world \nsecond") == "hello world"
    assert content_summary("x" * 100).endswith("…")
    assert len(content_summary("x" * 100)) <= 60
    assert content_summary("\n\n") == ""


def test_snapshot_caps_the_list_and_says_how_many_are_hidden(tmp_path: Path) -> None:
    cache = FileStateCache()
    for index in range(3):
        target = tmp_path / f"f{index}.py"
        target.write_text("x = 1\n", encoding="utf-8")
        cache.record_write(tmp_path, target.name)

    snapshot = cache.render_snapshot(limit=2)

    assert snapshot.count("\n- ") == 2
    assert "另有 1 个文件未列出" in snapshot


def test_snapshot_hides_files_removed_from_disk(tmp_path: Path) -> None:
    """shell 删掉的文件不能继续出现在「当前状态」里。"""
    cache = FileStateCache()
    target = tmp_path / "gone.py"
    target.write_text("x = 1\n", encoding="utf-8")
    cache.record_write(tmp_path, "gone.py")
    target.unlink()

    assert cache.render_snapshot(root=tmp_path) == ""


def test_microcompact_folds_old_read_result_to_a_snapshot_pointer() -> None:
    messages = [
        _read_call("c1", "a.py"),
        {"role": "tool", "tool_call_id": "c1", "content": "x" * 5_000},
        *({"role": "user", "content": "继续"} for _ in range(9)),
    ]

    trimmed, stats = _microcompact(messages, snapshot_paths={"a.py"})

    assert stats.reads_folded == 1
    folded = trimmed[1]["content"]
    assert "a.py" in folded and "read_file" in folded
    assert "x" * 100 not in folded


@pytest.mark.asyncio
async def test_compactor_microcompacts_below_the_water_line() -> None:
    """1M 窗口上 0.82 的水位到不了；旧写参数仍要折，否则每轮重发全文。"""
    compactor = ContextCompactor(context_window=1_000_000, trigger_ratio=0.82)
    big = "x" * 5_000
    writes = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": f"c{index}",
                    "type": "function",
                    "function": {
                        "name": "write_file",
                        "arguments": json.dumps(
                            {"path": f"f{index}.py", "content": big}, ensure_ascii=False
                        ),
                    },
                }
            ],
        }
        for index in range(10)
    ]
    messages = [
        {"role": "system", "content": "规则"},
        {"role": "user", "content": "目标"},
        *writes,
        *({"role": "tool", "tool_call_id": f"c{index}", "content": "已写入"} for index in range(10)),
        *({"role": "user", "content": "继续"} for _ in range(8)),
    ]

    built = await compactor.build(messages, None)

    report = compactor.last_report
    assert report.micro.args_shrunk > 0
    assert report.triggered_by == "micro:below"
    assert len(built) == len(messages)
    first_args = json.loads(built[2]["tool_calls"][0]["function"]["arguments"])
    assert first_args["content"].startswith("[较早的工具参数已折叠")
