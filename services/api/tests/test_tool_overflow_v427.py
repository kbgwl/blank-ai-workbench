"""超预算的结果要「挪」，不要「删」。

我们的裁剪是把中段**丢掉**：`run_shell` 只留头 6,000 + 尾 8,000 字符，中间折成一句
"已省略 N 字符，请缩小范围重跑"。问题是 `git diff`、测试日志、构建输出——恰恰是
编程主战场——中段通常就是模型要改的那几行。它只能重跑同一条命令，再花一遍上下文。

参照 Claude Code 系 harness 的做法（tool-call offloading）：整份存盘，回执给头尾 +
路径，需要时自己读。这里钉四件事：

1. **真的裁了才写盘**：没裁的结果本来就在上下文里，再存一份是垃圾。
2. **回执里必须有路径**，且路径能直接喂给 read_file（工作区相对路径）。
3. **写盘失败不能让工具调用失败**：落盘是加法，不是前提。
4. 只留最近若干份：这些是证据，不是账本。

第 5 条是这轮踩出来的：写手一开始把 `Path` 用在了没导入的模块里，异常被
`except Exception` 吞掉，功能**静默失效**——所以判据必须落到"文件真的存在"，
不能只断言"回执里有路径"。
"""

from __future__ import annotations

import types

from blank_api.services.agent.graph import _overflow_writer
from blank_api.services.agent.payload import render


def _session(root):
    return types.SimpleNamespace(context=types.SimpleNamespace(root=root))


def test_a_clipped_result_is_written_to_disk_and_the_path_is_in_the_receipt(workspace):
    output = "".join(f"line {index:05d} " + "x" * 40 + "\n" for index in range(2_000))

    text, stats = render(
        "run_shell",
        {"ok": True, "status": "ok", "output": output, "data": {"sandbox": {}}},
        step=7,
        overflow=_overflow_writer(_session(workspace), 7, "run_shell"),
    )

    path = stats["overflow_path"]
    assert path and path.startswith(".blank/tool-results/"), path
    stored = workspace / path
    assert stored.is_file(), "回执说有路径，盘上却没有——等于又骗了模型一次"
    assert stored.read_text(encoding="utf-8") == output, "存下来的不是完整输出"
    assert path in text


def test_an_unclipped_result_does_not_write_anything(workspace):
    text, stats = render(
        "run_shell",
        {"ok": True, "status": "ok", "output": "很短的结果"},
        step=2,
        overflow=_overflow_writer(_session(workspace), 2, "run_shell"),
    )

    assert "overflow_path" not in stats
    assert not (workspace / ".blank/tool-results").exists()
    assert "完整输出已存盘" not in text


def test_no_workspace_means_no_overflow_but_the_result_still_renders():
    output = "y" * 40_000

    text, stats = render("run_shell", {"ok": True, "status": "ok", "output": output}, step=1)

    assert "overflow_path" not in stats
    assert "省略" in text, "没有落盘时仍然要说清裁掉了多少"


def test_only_the_recent_overflow_files_are_kept(workspace):
    from blank_api.services.agent.graph import OVERFLOW_KEEP

    writer = _overflow_writer(_session(workspace), 1, "run_shell")
    for index in range(OVERFLOW_KEEP + 5):
        writer("run_shell", f"内容 {index} " * 100)

    kept = list((workspace / ".blank/tool-results").glob("*.txt"))
    assert len(kept) <= OVERFLOW_KEEP, f"存了 {len(kept)} 份，越攒越多"


def test_a_broken_workspace_does_not_break_the_tool(workspace, monkeypatch):
    """落盘失败是「少一份证据」，不是「这次调用失败」。"""
    writer = _overflow_writer(_session(workspace), 1, "run_shell")
    monkeypatch.setattr("pathlib.Path.mkdir", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("磁盘满了")))

    text, stats = render(
        "run_shell",
        {"ok": True, "status": "ok", "output": "z" * 40_000},
        step=1,
        overflow=writer,
    )

    assert "overflow_path" not in stats
    assert "省略" in text
