"""后台日志的长尾：溢出之后还要读得到最新那段。

这一组判据只回答一个问题：一条跑很久、输出很多的后台命令，
结束时能不能同时看到「它当初在干什么」和「它最后报了什么错」。

旧实现写满 2MB 就停笔，于是服务类命令的报错（永远在最后）一个字节都没落盘，
`task_output` 返回的全是启动日志——这就是这组用例存在的理由。

阈值在这里被压小（200 / 300 字节）再测，逻辑与真实上限完全同一条：
真实上限跑起来要写几 MB 才能碰到边界，用例会慢，而且掩盖不了任何东西。
"""

from __future__ import annotations

import pytest

from blank_api.services import shell_tasks

HEAD = 200
TAIL = 300
KEEP = 150


@pytest.fixture(autouse=True)
def small_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shell_tasks, "HEAD_LOG_BYTES", HEAD)
    monkeypatch.setattr(shell_tasks, "TAIL_LOG_BYTES", TAIL)
    monkeypatch.setattr(shell_tasks, "TAIL_KEEP_BYTES", KEEP)


def make_record(tmp_path) -> shell_tasks.ShellTask:
    return shell_tasks.ShellTask(
        shell_id="shell-log-1",
        task_id="task-log-1",
        command="fake",
        description="",
        timeout_seconds=1.0,
        log_path=tmp_path / "shell-log-1.log",
    )


def feed(record: shell_tasks.ShellTask, *chunks: bytes) -> None:
    sink = shell_tasks._Sink(record)
    try:
        for chunk in chunks:
            sink.write(shell_tasks._STDOUT, chunk)
    finally:
        sink.close()


def test_latest_output_survives_after_head_is_full(tmp_path) -> None:
    """头段写满之后，后面的输出还能读到——这是整组判据的核心。

    旧实现：写满即 truncated，`write` 直接 return，后面所有内容丢失。
    """
    record = make_record(tmp_path)
    feed(record, b"HEAD-START", b"A" * 400, b"TAIL-ERROR-MARKER")

    body = shell_tasks.read_log(record)
    assert "TAIL-ERROR-MARKER" in body, "溢出之后的最新输出必须还能读回来"
    assert "HEAD-START" in body, "最早那段也要留：它说明命令当时在干什么"


def test_head_stops_at_its_own_limit(tmp_path) -> None:
    """头段必须封笔。继续往主文件写会让「最早一段」变成「前 3MB」。"""
    record = make_record(tmp_path)
    feed(record, b"x" * (HEAD + 500))
    assert shell_tasks._file_size(record.log_path) == HEAD
    assert shell_tasks._file_size(record.tail_path) > 0


def test_disk_usage_stays_bounded(tmp_path) -> None:
    """写到十倍上限也不该把磁盘写满：上界 = 头段 + 尾段（含一行省略提示）。"""
    record = make_record(tmp_path)
    for _ in range(10):
        feed(record, b"y" * (TAIL + 10))
    total = shell_tasks._file_size(record.log_path) + shell_tasks._file_size(record.tail_path)
    assert total <= HEAD + TAIL + len(shell_tasks._TRUNCATED_MARK), f"实际占用 {total}"


def test_compaction_records_how_much_it_dropped(tmp_path) -> None:
    """尾段压缩过就必须在状态里记下「少了多少字节」，不能静默丢。"""
    record = make_record(tmp_path)
    for _ in range(6):
        feed(record, b"z" * (TAIL // 2 + 20))
    assert record.omitted > 0
    assert record.truncated is True
    assert "中间省略" in shell_tasks.read_log(record)


def test_incremental_read_returns_only_new_bytes(tmp_path) -> None:
    """增量读：拿到 offset 之后再读，只该多出第二批内容。"""
    record = make_record(tmp_path)
    feed(record, b"line-1\n")
    offset = record.produced
    feed(record, b"line-2\n")

    text, next_offset, dropped = shell_tasks.read_since(record, offset)
    assert "line-2" in text
    assert "line-1" not in text, "增量读不许把已经读过的那段再发一遍"
    assert next_offset == record.produced
    assert dropped is False


def test_incremental_read_reports_when_the_window_is_gone(tmp_path) -> None:
    """offset 落后于整个缓冲区时，必须如实说「中间丢了」，而不是假装连续。"""
    record = make_record(tmp_path)
    stale = 0
    for _ in range(8):
        feed(record, b"w" * (TAIL // 2 + 20))

    _text, next_offset, dropped = shell_tasks.read_since(record, stale)
    assert dropped is True
    assert next_offset == record.produced


def test_incremental_offset_never_goes_backwards(tmp_path) -> None:
    """压缩会让文件变小，但 offset 只增不减；否则游标会重复吐内容。"""
    record = make_record(tmp_path)
    feed(record, b"a" * (HEAD + 10))
    first = record.produced
    for _ in range(6):
        feed(record, b"b" * (TAIL // 2 + 20))
    assert record.produced > first


def test_head_cut_lands_on_a_character_boundary(tmp_path) -> None:
    """头段切点必须落在完整字符上，不能留半个汉字。

    用三个汉字（9 字节）去撞 200 字节的边界：切点若按字节硬切，
    末尾会留下 1–2 个续字节，读回时变成替换符。
    """
    record = make_record(tmp_path)
    # 195 字节 ASCII + 不断的中文，让 200 字节这个位置正好落在汉字中间
    feed(record, b"a" * 195)
    feed(record, "汉字汉字".encode("utf-8"))

    raw = record.log_path.read_bytes()
    assert len(raw) <= HEAD
    # 多字节字符被切成半截时 decode(errors="strict") 会抛，用 strict 才验得出问题
    raw.decode("utf-8")
    assert "\ufffd" not in shell_tasks.read_log(record)


def test_cancel_marker_goes_to_the_tail_segment(tmp_path) -> None:
    """头段封笔后，补写的标记必须进尾段；否则读回时它跑到「最早一段」后面。"""
    record = make_record(tmp_path)
    feed(record, b"x" * (HEAD + 50))
    before = shell_tasks._file_size(record.log_path)

    marker = shell_tasks._Sink(record)
    try:
        marker.text("\n[blank] 已取消\n")
    finally:
        marker.close()

    assert shell_tasks._file_size(record.log_path) == before, "标记不许再写进已封笔的头段"
    assert "已取消" in shell_tasks.read_log(record)


def test_notification_tail_only_carries_the_end(tmp_path) -> None:
    """结束通知只要最后那句：带上头部会把 1200 字的额度花在启动日志上。"""
    record = make_record(tmp_path)
    feed(record, b"HEAD-START", b"x" * 400, b"THE-END")

    body = shell_tasks.read_log(record, limit=200, with_head=False)
    assert "THE-END" in body
    assert "HEAD-START" not in body


def test_partial_tail_file_is_still_readable(tmp_path) -> None:
    """进程被杀在写盘中间：文件里留半截字符也不该让读回整体失败。"""
    record = make_record(tmp_path)
    feed(record, b"x" * (HEAD + 10), b"normal line\n")
    with open(record.tail_path, "ab") as handle:
        handle.write("尾巴".encode("utf-8")[:3])  # 故意写半个汉字

    body = shell_tasks.read_log(record)
    assert isinstance(body, str)
    assert "normal line" in body
