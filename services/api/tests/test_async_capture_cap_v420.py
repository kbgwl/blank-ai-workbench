"""异步捕获路径的上限（工具层第二刀）。

上一刀修的是前台 `_run_blocking`（管道 → 临时文件）。异步那条还有三种形状会整段进内存：
`communicate()`（前台带取消）、`communicate()`（其余）、以及流式路径里 `_pump` 往 list 里
无限追加。这一版全部收口：非流式写临时文件，流式只留前 `MAX_CAPTURE_BYTES`，
完整输出本来就已经由 `on_chunk` 落盘了。

钉四件事：
1. 大输出：进内存的有上限，且说得清原始总量；
2. 流式：回调拿到**全量**，返回值只留前一段（两件事不能混成一件）；
3. 超时：带回已跑出的输出；
4. 取消与超时分开报（"用户改主意" 不许写成 "这条命令有问题"）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from blank_api.services.tools.shell import MAX_CAPTURE_BYTES, run_process


def _python(code: str) -> list[str]:
    return [sys.executable, "-u", "-c", code]


async def test_a_huge_output_is_capped_but_totalled(tmp_path: Path) -> None:
    total = 5_000_000
    result = await run_process(
        _python(f"import sys; sys.stdout.write('y' * {total})"), tmp_path, timeout=120.0
    )
    assert result["exit_code"] == 0
    assert len(result["stdout"]) <= MAX_CAPTURE_BYTES, "异步路径又把整段读进内存了"
    assert result["stdout_truncated"] is True
    assert result["stdout_bytes"] >= total, result.get("stdout_bytes")


async def test_the_stream_callback_sees_everything_while_memory_is_capped(tmp_path: Path) -> None:
    """流式：回调必须拿到全量（后台日志要完整），返回值只留前一段。"""
    total = 4_000_000
    seen: list[int] = []

    def sink(_tag: str, chunk: bytes) -> None:
        seen.append(len(chunk))

    result = await run_process(
        _python(f"import sys; sys.stdout.write('z' * {total})"),
        tmp_path,
        timeout=120.0,
        on_chunk=sink,
    )
    assert sum(seen) == total, f"回调只收到 {sum(seen)}/{total} 字节，日志会缺内容"
    assert len(result["stdout"]) <= MAX_CAPTURE_BYTES
    assert result["stdout_bytes"] == total


async def test_a_timeout_hands_back_what_ran(tmp_path: Path) -> None:
    result = await run_process(
        _python("import sys, time; print('started'); sys.stdout.flush(); time.sleep(30)"),
        tmp_path,
        timeout=1.5,
    )
    assert result["timed_out"] is True and result["cancelled"] is False
    assert "started" in result["stdout"], f"超时丢了已跑出的输出：{result['stdout']!r}"


async def test_cancel_is_not_reported_as_timeout(tmp_path: Path) -> None:
    """用户按停止 ≠ 命令有问题：两者的归因不能混。"""
    cancel = asyncio.Event()

    async def stop_soon() -> None:
        await asyncio.sleep(0.5)
        cancel.set()

    asyncio.create_task(stop_soon())
    result = await run_process(
        _python("import time; time.sleep(30)"), tmp_path, timeout=20.0, cancel_event=cancel
    )
    assert result["cancelled"] is True
    assert result["timed_out"] is False, "取消被写成了超时，模型会以为方案本身有问题"


async def test_a_normal_command_still_reads_exactly(tmp_path: Path) -> None:
    result = await run_process(_python("print('abc')"), tmp_path, timeout=60.0)
    assert result["stdout"].strip() == "abc"
    assert result["stdout_truncated"] is False
    assert result["stdout_bytes"] == len(result["stdout"].encode("utf-8"))
