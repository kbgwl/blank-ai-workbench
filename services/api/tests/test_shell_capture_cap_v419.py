"""命令输出的内存上限（工具层第一刀）。

旧形状：`subprocess.run(capture_output=True)` 先把命令吐的**全部**读进内存，
再剪到 8000 字交给模型。实测一条 200MB 输出，结果字典里就躺着 200,000,000 个字符——
内存不是"跟模型预算有关"，而是"跟被执行的命令有关"。一条 `type` 大文件就能把进程顶爆。

这一版改成写临时文件再按上限读，钉住四件事：
1. 超过上限的捕获被截断，且**说得出总共多少字节**；
2. 正常输出一个字都不动（包括中文与换行）；
3. 超时也能交回已经跑出来的那部分（不是只有一句"超时了"）；
4. 截断点切在多字节字符中间时不许退化成乱码。
"""

from __future__ import annotations

import sys
from pathlib import Path

from blank_api.services.tools.shell import (
    MAX_CAPTURE_BYTES,
    MAX_OUTPUT_CHARS,
    _run_blocking,
)


def _python(code: str) -> list[str]:
    return [sys.executable, "-u", "-c", code]


def test_a_huge_output_is_capped_and_reported(tmp_path: Path) -> None:
    """5MB 输出：进内存的只有上限那么多，但总共输出了多少必须说清楚。"""
    total = 5_000_000
    result = _run_blocking(
        _python(f"import sys; sys.stdout.write('x' * {total})"), tmp_path, 120.0, shell=False
    )
    assert result["exit_code"] == 0
    assert len(result["stdout"]) <= MAX_CAPTURE_BYTES, "捕获没有上限，内存跟着命令走"
    assert result["stdout_truncated"] is True
    assert result["stdout_bytes"] >= total, (
        f"只说截断不说总数，模型没法判断还差多少：{result.get('stdout_bytes')}"
    )


def test_a_normal_output_is_untouched(tmp_path: Path) -> None:
    result = _run_blocking(
        _python("print('第一行'); print('second line')"), tmp_path, 60.0, shell=False
    )
    assert result["stdout"].replace("\r\n", "\n") == "第一行\nsecond line\n"
    assert result["stdout_truncated"] is False
    # 字节数说的是**文件里原样的字节**（Windows 下每行多一个 \r），不是字符数
    assert result["stdout_bytes"] == len(result["stdout"].encode("utf-8"))


def test_a_timeout_still_hands_back_what_ran(tmp_path: Path) -> None:
    """超时不是"什么都没发生"：已经打印出来的那部分要交回去。"""
    result = _run_blocking(
        _python("import sys, time; print('started'); sys.stdout.flush(); time.sleep(30)"),
        tmp_path,
        1.5,
        shell=False,
    )
    assert result["timed_out"] is True and result["exit_code"] is None
    assert "started" in result["stdout"], f"超时把已跑出的输出丢了：{result['stdout']!r}"


def test_the_cap_does_not_split_a_chinese_character(tmp_path: Path) -> None:
    """截断点落在一个汉字中间时，不许退到 gbk 解出乱码。"""
    # 每字符 3 字节；制造一个必然切在字符中间的字节数
    code = (
        "import sys; "
        "sys.stdout.write('中' * 1_000_000)"
    )
    result = _run_blocking(_python(code), tmp_path, 120.0, shell=False)
    text = result["stdout"]
    assert text, "截断后一个字都没有"
    assert "\ufffd" not in text, "截断点切碎了多字节字符"
    assert set(text) == {"中"}, f"出现了解码乱码：{text[:20]!r}"


def test_the_trim_still_applies_above_the_model_budget(tmp_path: Path) -> None:
    """捕获上限远大于模型预算：两层各司其职，别把上限当预算用。"""
    assert MAX_CAPTURE_BYTES > MAX_OUTPUT_CHARS * 10
