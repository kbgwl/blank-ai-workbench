"""沙箱补写分支的字节锚（工具层第五刀）。

沙箱里的命令不流式：``runner`` 不在过程中调输出回调，只在结束时整段返回
``stdout`` / ``stderr``。``_execute`` 于是有一条兜底分支（``record.written == 0``）
把结果补写进日志——否则沙箱里跑的监控，``task_output`` 读到的是空文件。

这条分支此前**没有任何判据**：它是靠「读代码觉得对」维持的。而它同时要维护两件
互相牵连的事，正好是最容易悄悄坏掉的那种：

* 事件 offset 必须和日志字节对得上（对不上，``task_output(after=...)`` 就指错地方）；
* 日志必须可读——补写时插的 ``[stderr]`` 分隔行要和流式路径同形（``\\n[stderr]\\n``）。
  少了前导换行，stdout 不以换行结尾时日志会连成 ``end[stderr]``，那 9 个字符
  看起来就是命令输出的一部分。本组第一条判据就是钉这个（改之前确实是这样）。

判据都跑真实的 ``_execute``：补写分支只在 ``runner`` 不 emit 时才走到，
直接调内部函数测不到「谁把结果交给谁」这一层。
"""

from __future__ import annotations

import asyncio
import pathlib

import pytest

from blank_api.services import shell_tasks

TASK_ID = "task-sandbox-backfill"


@pytest.fixture(autouse=True)
def clean_bucket():
    shell_tasks._TASKS.pop(TASK_ID, None)
    yield
    shell_tasks._TASKS.pop(TASK_ID, None)


async def run_backfill(
    tmp_path: pathlib.Path,
    *,
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
    mask=None,
    shell_id: str = "mon-sandbox-1",
) -> shell_tasks.ShellTask:
    """走生产路径跑一次「沙箱式」命令：runner 不 emit，只在结束时给整段输出。"""
    record = shell_tasks.ShellTask(
        shell_id=shell_id,
        task_id=TASK_ID,
        command="sandbox fake",
        description="判据用",
        timeout_seconds=5.0,
        log_path=tmp_path / f"{shell_id}.log",
        kind="monitor",
    )
    shell_tasks._TASKS.setdefault(TASK_ID, {})[shell_id] = record

    async def runner(_emit):
        return {"stdout": stdout, "stderr": stderr, "exit_code": exit_code}

    await shell_tasks._execute(record, runner, None, mask)
    return record


def events_of(record: shell_tasks.ShellTask) -> list[dict]:
    return [dict(item) for item in record.events]


def test_stderr_separator_is_its_own_line(tmp_path) -> None:
    """stdout 不以换行结尾时，分隔行也不能粘在它旁边。

    改之前：日志是 ``no newline at end[stderr]\\nerr``——用户读日志会以为那串
    方括号是命令输出。补写属于「日志写错了」，不是「格式不好看」：真出故障时
    人要照着这份日志判断哪段是 stdout、哪段是 stderr。
    """
    record = asyncio.run(run_backfill(tmp_path, stdout="no newline at end", stderr="err"))
    body = shell_tasks.read_log(record)

    assert "end[stderr]" not in body, f"分隔行粘在 stdout 尾巴上了：{body!r}"
    assert "\n[stderr]\n" in body
    lines = [line for line in body.split("\n") if line.strip()]
    assert lines[:3] == ["no newline at end", "[stderr]", "err"], lines


def test_stdout_offsets_are_byte_exact(tmp_path) -> None:
    """两行 stdout：第二行从第一行的结束位置开始。"""
    record = asyncio.run(run_backfill(tmp_path, stdout="line one\nline two\n"))
    events = events_of(record)

    assert [(e["offset"], e["end"]) for e in events] == [(0, 9), (9, 18)]
    assert [e["text"] for e in events] == ["line one", "line two"]


def test_stderr_starts_after_the_separator(tmp_path) -> None:
    """stderr 事件必须指向分隔行之后，否则增量读会把 ``[stderr]`` 当输出读回来。"""
    record = asyncio.run(run_backfill(tmp_path, stdout="out\n", stderr="boom\n"))
    stdout_event, stderr_event = events_of(record)

    # "out\n" 4 字节 + "\n[stderr]\n" 10 字节
    assert stderr_event["offset"] == stdout_event["end"] + 10

    got, _, dropped = shell_tasks.read_since(record, stderr_event["offset"])
    assert dropped is False
    assert got.startswith("boom"), got


def test_offsets_are_bytes_not_characters(tmp_path) -> None:
    """中文占 3 字节：按字符算 offset，第二段就找不到。"""
    record = asyncio.run(run_backfill(tmp_path, stdout="中文一行\n", stderr="错误\n"))
    stdout_event, stderr_event = events_of(record)

    text = "中文一行\n".encode("utf-8")
    assert stdout_event["end"] == len(text) == 13, "end 应是字节数"
    assert stderr_event["offset"] == len(text) + len("\n[stderr]\n") == 23

    got, _, _ = shell_tasks.read_since(record, stderr_event["offset"])
    assert got.startswith("错误"), got


def test_stderr_only_starts_at_zero(tmp_path) -> None:
    """只有 stderr 时没有分隔行，起点必须是 0 而不是 10。"""
    record = asyncio.run(run_backfill(tmp_path, stderr="only stderr\n"))
    events = events_of(record)

    assert [(e["tag"], e["offset"], e["end"]) for e in events] == [("stderr", 0, 12)]

    got, _, _ = shell_tasks.read_since(record, 0)
    assert got.startswith("only stderr"), got


def test_every_offset_replays_its_own_line(tmp_path) -> None:
    """逐条对账：拿每条事件的 offset 回读，开头必须就是它自己的文本。

    这是整组判据的母句——offset 存在的唯一理由是「从这儿往后读能读到这一行」。
    """
    record = asyncio.run(
        run_backfill(tmp_path, stdout="alpha\nbeta\ngamma\n", stderr="warning\nfatal\n")
    )
    events = events_of(record)
    assert len(events) == 5

    for event in events:
        got, _, dropped = shell_tasks.read_since(record, event["offset"])
        assert not dropped, event
        assert got.startswith(event["text"]), f"{event['text']!r} 的 offset 指偏了：{got[:40]!r}"


def test_incremental_read_after_last_event_only_yields_new_bytes(tmp_path) -> None:
    """从最后一条事件的 end 往后读，不该再看到事件文本本身。"""
    record = asyncio.run(run_backfill(tmp_path, stdout="one\ntwo\n"))
    last = events_of(record)[-1]

    got, next_offset, dropped = shell_tasks.read_since(record, last["end"])
    assert dropped is False
    assert "one" not in got and "two" not in got, got
    assert "退出码" in got, got
    assert next_offset == record.produced


def test_flushed_line_without_newline_keeps_its_own_offset(tmp_path) -> None:
    """结尾没有换行的那一行，offset 必须指向它自己。

    它在 ``flush`` 时才被切出来，而 flush 里若拿 ``record.produced``（此刻等于
    日志末尾）当起点，offset 就指到文件尽头——回读拿到的是空串或退出码那行。
    前一条判据只看文本，看不出这个偏移，所以单独钉一次。
    """
    record = asyncio.run(run_backfill(tmp_path, stdout="no newline at end"))
    event = events_of(record)[0]

    assert event["offset"] == 0, "flush 出来的行起点应是它自己写入的位置"
    got, _, _ = shell_tasks.read_since(record, event["offset"])
    assert got.startswith("no newline at end"), got


def test_empty_output_produces_no_events(tmp_path) -> None:
    """两边都空时不产生事件，也不能出现空文本事件。"""
    record = asyncio.run(run_backfill(tmp_path, exit_code=3))
    assert events_of(record) == []
    assert "退出码 3" in shell_tasks.read_log(record)


def test_mask_is_applied_before_offsets_are_measured(tmp_path) -> None:
    """打码后的文本才是落盘内容，offset 必须按打码后的字节算。

    反过来（按打码前算）会让事件指向日志里不存在的区间——日志里那一段已经被
    替换成 ``***``，长度都不一样。
    """
    record = asyncio.run(
        run_backfill(
            tmp_path,
            stdout="token=abcdef\nnext\n",
            mask=lambda text: text.replace("abcdef", "***"),
        )
    )
    events = events_of(record)
    assert events[0]["text"] == "token=***"
    assert events[0]["end"] == len("token=***\n".encode("utf-8"))

    got, _, _ = shell_tasks.read_since(record, events[1]["offset"])
    assert got.startswith("next"), got


def test_events_are_still_delivered_when_log_cannot_be_opened(tmp_path) -> None:
    """日志目录建不出来时，事件照投，offset 用兜底游标。

    存档缺一份不该连累实时通道——沙箱环境下日志路径不可写并非不可能，
    这时模型仍然要能看到命令说了什么。
    """
    blocked = tmp_path / "blocked"
    blocked.write_text("我不是目录", encoding="utf-8")
    record = shell_tasks.ShellTask(
        shell_id="mon-sandbox-dead",
        task_id=TASK_ID,
        command="sandbox fake",
        description="判据用",
        timeout_seconds=5.0,
        log_path=blocked / "mon-sandbox-dead.log",
        kind="monitor",
    )
    shell_tasks._TASKS.setdefault(TASK_ID, {})["mon-sandbox-dead"] = record

    async def runner(_emit):
        return {"stdout": "first\nsecond\n", "stderr": "", "exit_code": 0}

    asyncio.run(shell_tasks._execute(record, runner, None, None))

    events = events_of(record)
    assert [e["text"] for e in events] == ["first", "second"], "写盘失败不该让事件消失"
    assert [e["offset"] for e in events] == [0, 6], "兜底游标仍要单调递增"
    assert record.error, "写盘失败本身仍要如实记在 error 上"
