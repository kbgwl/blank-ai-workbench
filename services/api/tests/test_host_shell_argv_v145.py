"""#145：本机档的 argv 必须按「这层壳」的规则拼，而不是 POSIX 规则。

这是真模型跑评测时撞出来的：样例 T01 的验证命令是 `python -m pytest -q`，
平台把 `python` 换成本机虚拟环境解释器的绝对路径，于是命令变成
`E:\\projects\\...\\python.exe -m pytest -q`。执行结果返回

    [stderr] 文件名、目录名或卷标语法不正确。

而同一个工作区里 T03 的 `npm test` 跑得好好的。差别只有一条：**反斜杠路径需要引用**。
`shlex.join` 按 POSIX 规则给它加的是单引号，`cmd.exe` 不认单引号，把整条命令当成一个
文件名。这是"一半能跑、一半报文件名错"的根源，也是最容易被误读成"用户环境坏了"的那种红。

判据分两层：
1. `join_for_shell` 的规则本身：Windows 上等于 `subprocess.list2cmdline`，POSIX 上等于 `shlex.join`。
2. 真跑一次：绝对解释器路径 + `-c` 打印一句话，必须拿回那句话。

第 2 条在 Windows 上就是对着 cmd.exe 验的；在 Linux 上走 `shlex` 分支同样成立——
两端都跑，才能保证改其中一端时另一端不静默退化。
"""

from __future__ import annotations

import shlex
import subprocess
import sys

import pytest

from blank_api.services.sandbox.policy import SandboxPolicy
from blank_api.services.sandbox.runner import LocalSandbox, join_for_shell


def test_quoting_follows_the_shell_that_will_run_it() -> None:
    """带空格的绝对路径在 Windows 上要被双引号包住，而不是单引号。"""
    argv = [r"C:\Program Files\Python\python.exe", "-m", "pytest", "-q"]
    rendered = join_for_shell(argv)
    if sys.platform == "win32":
        assert rendered == subprocess.list2cmdline(argv)
        assert '"' in rendered, rendered
        assert "'" not in rendered, f"cmd 不认单引号：{rendered}"
    else:
        assert rendered == shlex.join(argv)


def test_a_plain_string_is_passed_through() -> None:
    """调用方已经自己拼好的串（用户手写的命令）不动它——改了就等于替用户改了命令。"""
    assert join_for_shell("npm test") == "npm test"
    assert join_for_shell('python -c "print(1)"') == 'python -c "print(1)"'


async def test_the_host_tier_runs_an_absolute_interpreter_path(tmp_path) -> None:
    """真跑：绝对路径的解释器 + 一个要打印出来的参数。"""
    sandbox = LocalSandbox(SandboxPolicy(enabled=True, allow_local=True))
    result = await sandbox.run(
        [sys.executable, "-c", "print('argv-survived')"],
        workspace=str(tmp_path),
        timeout_seconds=60,
    )
    assert result.exit_code == 0, (result.exit_code, result.stdout, result.stderr)
    assert "argv-survived" in result.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="单引号只有 cmd.exe 不认，POSIX 壳认")
async def test_the_old_posix_join_really_did_break_on_the_host_shell(tmp_path) -> None:
    """对照格：换回 `shlex.join` 就必须红。

    这条不是"证明我们写对了"，而是证明**原来那种写法在这层壳上确实跑不通**——
    没有它，别人会怀疑上面那条是在验一个不存在的问题。
    """
    sandbox = LocalSandbox(SandboxPolicy(enabled=True, allow_local=True))
    result = await sandbox.run(
        shlex.join([sys.executable, "-c", "print('argv-survived')"]),
        workspace=str(tmp_path),
        timeout_seconds=60,
    )
    assert result.exit_code != 0, "POSIX 单引号居然能被 cmd 正确解析，这条对照格失效了"
    assert "argv-survived" not in result.stdout
