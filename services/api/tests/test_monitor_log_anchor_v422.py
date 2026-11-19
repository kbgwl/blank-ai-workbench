"""monitor 事件与日志文件的字节锚（工具层第四刀）。

这一组判据只回答一个问题：模型收到的每一条行事件，能不能指回日志文件里的
字节区间。能指回，它就能对被丢弃的那段、被截断的那行、想补看的上下文调
``task_output(after=...)``；指不回，它只能整段重读最后 64KB。

三个容易搞错的地方在这里被钉住：

* ``end`` 必须指向**原始整行**的结束位置——文本被 ``MONITOR_MAX_EVENT_CHARS``
  截断不影响它占的字节，否则拿 ``end`` 当 offset 会重复读到被截掉的尾巴；
* 空行不产生事件，但它占的字节必须计进去——否则每遇一个空行，后面所有
  offset 集体偏移；
* offset 是字节数不是字符数。用中文行做判据：按字符算会和日志对不上账。

阈值不压缩：这些边界与真实上限同一条逻辑，没有需要跑几 MB 才碰得到的分支。
"""

from __future__ import annotations

import asyncio

import pytest

from blank_api.services import shell_tasks

TASK_ID = "task-mon-anchor"


@pytest.fixture(autouse=True)
def clean_bucket():
    shell_tasks._TASKS.pop(TASK_ID, None)
    yield
    shell_tasks._TASKS.pop(TASK_ID, None)


def make_record(tmp_path, shell_id: str = "mon-anchor-1") -> shell_tasks.ShellTask:
    """造一个已登记在句柄表里的监控句柄（drain 只认表里的记录）。"""
    record = shell_tasks.ShellTask(
        shell_id=shell_id,
        task_id=TASK_ID,
        command="fake",
        description="判据用",
        timeout_seconds=1.0,
        log_path=tmp_path / f"{shell_id}.log",
        kind="monitor",
    )
    shell_tasks._TASKS.setdefault(TASK_ID, {})[shell_id] = record
    return record


def pump(record: shell_tasks.ShellTask, *chunks: bytes, tag: str = shell_tasks._STDOUT) -> None:
    """按生产路径喂输出：先写盘拿到起点，再带着起点切行。

    走 ``_Sink.write`` 的返回值而不是自己数字节，是因为 tag 切换时写盘会插一行
    分隔符——那几字节也在日志里，切行侧看不到，自己数就会漂。
    """
    sink = shell_tasks._Sink(record)
    splitter = shell_tasks._LineSplitter(record)
    try:
        for chunk in chunks:
            start = sink.write(tag, chunk)
            splitter.feed(tag, chunk, start)
        splitter.flush()
    finally:
        sink.close()


def texts(events: list[dict]) -> list[str]:
    return [event["text"] for event in events]


def test_single_line_carries_offset_and_end(tmp_path) -> None:
    """一条事件要知道自己从第几个字节开始、到第几个字节结束。"""
    record = make_record(tmp_path)
    pump(record, b"hello\n")

    events = list(record.events)
    assert len(events) == 1
    assert events[0]["text"] == "hello"
    assert events[0]["offset"] == 0
    assert events[0]["end"] == 6, "end 是这一行（含换行）之后的第一个字节"
    assert record.produced == 6


def test_offsets_advance_line_by_line_in_bytes(tmp_path) -> None:
    """多行递增，且是字节数不是字符数：中文一行占 9 字节 + 1 个换行。"""
    record = make_record(tmp_path)
    pump(record, "第一行\n第二行\n".encode("utf-8"))

    events = list(record.events)
    assert texts(events) == ["第一行", "第二行"]
    assert [event["offset"] for event in events] == [0, 10], "按字符算会得到 0/4，与日志对不上"
    assert [event["end"] for event in events] == [10, 20]


def test_blank_lines_do_not_shift_later_offsets(tmp_path) -> None:
    """空行不产生事件，但它占的字节要算进后面的 offset。"""
    record = make_record(tmp_path)
    pump(record, b"a\n\n\nb\n")

    events = list(record.events)
    assert texts(events) == ["a", "b"], "空行不该变成事件"
    assert events[0]["offset"] == 0 and events[0]["end"] == 2
    assert events[1]["offset"] == 4, "中间两个空行各占 1 字节，b 必须从第 4 字节开始"
    assert events[1]["end"] == 6


def test_tail_line_without_newline_still_has_an_end(tmp_path) -> None:
    """命令结束时最后一行可能没有换行：end 仍要指向它的末尾，不能等于起点。"""
    record = make_record(tmp_path)
    pump(record, b"tail")

    events = list(record.events)
    assert len(events) == 1
    assert events[0]["offset"] == 0
    assert events[0]["end"] == 4, "没有换行的那一行占 4 字节，end 必须在它末尾之后"
    assert record.produced == 4


def test_truncated_line_keeps_the_original_end(tmp_path) -> None:
    """长行被截断后，end 仍指向原始整行的结束位置。

    否则模型拿 end 当 offset 增量读，会把被截掉的那截尾巴再读一遍。
    """
    record = make_record(tmp_path)
    long_line = "x" * (shell_tasks.MONITOR_MAX_EVENT_CHARS + 50)
    pump(record, (long_line + "\nnext\n").encode("utf-8"))

    first, second = list(record.events)
    assert first["text"].endswith("…"), "投递的文本要能看出被截断了"
    assert len(first["text"]) == shell_tasks.MONITOR_MAX_EVENT_CHARS + 1
    assert first["end"] == len(long_line) + 1, "end 按原始整行算，不按截断后的文本算"
    assert second["offset"] == first["end"]

    body, next_offset, dropped = shell_tasks.read_since(record, first["end"])
    assert body.strip() == "next", "从 end 往后读只该读到下一行，不能重复读尾巴"
    assert next_offset == record.produced
    assert dropped is False


def test_tag_switch_header_does_not_shift_offsets(tmp_path) -> None:
    """stderr 前写盘会插一行分隔符，那几字节也在日志里，offset 必须跟着走。"""
    record = make_record(tmp_path)
    sink = shell_tasks._Sink(record)
    splitter = shell_tasks._LineSplitter(record)
    try:
        for tag, chunk in (
            (shell_tasks._STDOUT, b"out\n"),
            (shell_tasks._STDERR, b"err\n"),
        ):
            start = sink.write(tag, chunk)
            splitter.feed(tag, chunk, start)
        splitter.flush()
    finally:
        sink.close()

    out_event, err_event = list(record.events)
    assert (out_event["tag"], out_event["offset"]) == ("stdout", 0)
    header = len("\n[stderr]\n".encode("utf-8"))
    assert err_event["tag"] == "stderr"
    assert err_event["offset"] == 4 + header, "分隔行占的字节要算进 stderr 事件的起点"
    assert err_event["end"] == record.produced
    assert shell_tasks.read_since(record, err_event["offset"])[0].strip() == "err"


def test_dropped_events_keep_the_covered_range(tmp_path, monkeypatch) -> None:
    """丢掉最旧的几条之后，本轮覆盖的区间仍从被丢掉那段的起点开始。"""
    monkeypatch.setattr(shell_tasks, "MONITOR_MAX_PENDING_EVENTS", 2)
    record = make_record(tmp_path)
    pump(record, b"l1\nl2\nl3\nl4\n")

    assert record.dropped_events == 2
    assert [event["text"] for event in record.events] == ["l3", "l4"]

    notes = shell_tasks.drain_monitor_events(TASK_ID)
    assert len(notes) == 1
    note = notes[0]
    assert note["dropped"] == 2
    assert note["dropped_events"] == 2
    assert note["from_offset"] == 0, "区间左界是被丢掉那段的起点，不是第一条投递事件的起点"
    assert note["to_offset"] == 12
    assert [event["offset"] for event in note["events"]] == [6, 9]


def test_second_drain_starts_where_the_previous_ended(tmp_path, monkeypatch) -> None:
    """两轮投递的区间要首尾相接，dropped 计数每轮归零。"""
    monkeypatch.setattr(shell_tasks, "MONITOR_MAX_PENDING_EVENTS", 2)
    record = make_record(tmp_path)
    pump(record, b"l1\nl2\nl3\n")

    first = shell_tasks.drain_monitor_events(TASK_ID)[0]
    assert first["from_offset"] == 0 and first["to_offset"] == 9
    assert first["dropped"] == 1
    assert record.dropped_events == 0, "上报过就必须归零，不然下一轮会重复报"
    assert record.dropped_events_from is None

    pump(record, b"l4\n")
    second = shell_tasks.drain_monitor_events(TASK_ID)[0]
    assert second["dropped"] == 0
    assert second["from_offset"] == first["to_offset"], "下一轮从上一轮停下的字节继续"
    assert second["to_offset"] == 12


def test_no_events_means_no_note(tmp_path) -> None:
    """没有事件、没有丢弃、也没结束时不产生空通知。"""
    record = make_record(tmp_path)
    pump(record, b"\n\n\n")

    assert list(record.events) == []
    assert shell_tasks.drain_monitor_events(TASK_ID) == []


def test_offsets_line_up_with_incremental_read(tmp_path) -> None:
    """拿到 to_offset 之后增量读，只该多出那之后的内容。"""
    record = make_record(tmp_path)
    pump(record, b"one\ntwo\n")
    note = shell_tasks.drain_monitor_events(TASK_ID)[0]
    assert note["to_offset"] == 8

    pump(record, b"three\n")
    body, next_offset, dropped = shell_tasks.read_since(record, note["to_offset"])
    assert body.strip() == "three"
    assert next_offset == record.produced == 14
    assert dropped is False


def test_events_survive_a_dead_log_handle(tmp_path) -> None:
    """日志文件打不开时，事件仍要投递。

    这是这一刀最容易被写回去的回退：``_Sink.write`` 失败返回 None，如果切行
    被写成「拿到 start 才喂」，写盘一坏就等于把模型的眼睛也关上——比原本
    「日志没有、事件还有」更差。存档缺一份不该连累实时通道。

    判据刻意走 ``_execute`` 的真实路径而不是直接调 ``feed``：只测函数的话，
    ``emit`` 里那个 ``if start is not None`` 的门永远碰不到，改了实现也全绿。
    """
    record = make_record(tmp_path)
    # 让日志目录建不出来：父路径占位成一个普通文件，mkdir 必失败。
    blocked = tmp_path / "blocked"
    blocked.write_text("我不是目录", encoding="utf-8")
    record.log_path = blocked / "mon-anchor-1.log"

    async def runner(emit) -> dict:
        emit(shell_tasks._STDOUT, b"first\n")
        emit(shell_tasks._STDOUT, b"second\n")
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    asyncio.run(shell_tasks._execute(record, runner, None, None))

    events = list(record.events)
    assert texts(events) == ["first", "second"], "写盘失败不该让事件消失"
    assert [event["offset"] for event in events] == [0, 6], "兜底游标仍要单调递增"
    assert record.error, "写盘失败本身仍要如实记在 error 上"
