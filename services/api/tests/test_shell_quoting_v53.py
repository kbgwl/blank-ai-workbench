"""#87：批准前缀在 PowerShell / cmd 下的引号往返实测（对标 Codex ``default.rules``）。

实测装置 ``E:/tmp/m87/probe3.py``（输出 ``E:/tmp/m87/probe3.json``）在本批改动**前**量出三种缺陷：

1. ``powershell`` / ``pwsh`` / ``cmd`` 压根不在载荷识别表里 ⇒ 本机默认壳的命令**永远算不出**
   免问档位（22 条样本里 17 条空规则），卡片只发得出"仅本次允许"；
2. ``IRREVERSIBLE`` 只认 unix 形状 ⇒ ``powershell -Command "Remove-Item -Recurse C:\\target"``
   被判"可逆"，于是它既写得进免问规则，又对用户承诺"可靠快照回滚"——两句都是谎；
3. 两条方向相反的引号错：``python -c "print(1)"`` 的规则会顺带免问换了尾部参数的
   ``python -c "print(1)" https://a.example``（记的比执行的窄，匹配却更宽）；
   ``render_rule`` 用单引号包裹含单引号的载荷会写出 ``'print('x')'`` 这种读不回来的串。
   另外 PowerShell 用两个双引号表示引号本身（记作 ``DQ`` 相邻的形状），只剥一层引号的
   tokenize 会把载荷切残；切残的规则把不同的载荷塌成同一条，批准其一就免问了其二。
   样本见下面的 ``ps-doubled-quote`` 那一格与 :func:`test_ps_doubled_quote_payload_is_cut_and_therefore_refused`。

本文件把这些判据逐条钉住，并要求**记下的串不得比执行的更宽**（撞车表）与
**批准的那条命令本身下次不再挂卡**（否则用户会觉得"批了没用"）同时成立。
"""

import sys
from pathlib import Path

import pytest

from blank_api.services.permissions import (
    ASK,
    SCOPE_PREFIX,
    PermissionPolicy,
    _basename,
    approval_options,
    covered_by_rules,
    is_irreversible,
    memo_rule,
    memoizable_prefix,
    prefix_rule,
    render_rule,
    split_segments,
    tokenize,
)
from blank_api.services.tools.base import ToolDefinition

POWERSHELL = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
CMD = Path(r"C:\Windows\System32\cmd.exe")


def _tool(name: str = "run_shell", risk: str = "high") -> ToolDefinition:
    async def handler(context, arguments):  # pragma: no cover - 只用于构造定义
        raise AssertionError("handler should not be called")

    return ToolDefinition(
        name=name,
        description="d",
        input_schema={"type": "object", "required": ["command"]},
        risk_level=risk,
        handler=handler,
    )


def _decide(policy: PermissionPolicy, command: str, risk: str = "high"):
    return policy.decide(_tool(), {"command": command}, risk)


# --------------------------------------------------------------------------- #
# 1. Windows 壳的内联载荷要认得，且记成整段 argv
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ('powershell -Command "Get-ChildItem"', ("powershell", "-Command", "Get-ChildItem")),
        ("pwsh -Command 'Get-ChildItem'", ("pwsh", "-Command", "Get-ChildItem")),
        # 解释器自己的旗标留在串里：换旗标就是换命令，不免问
        (
            'powershell -NoProfile -Command "Get-ChildItem"',
            ("powershell", "-NoProfile", "-Command", "Get-ChildItem"),
        ),
        # 载荷带空格：必须是一个 token，且渲染出去还读得回来
        ('cmd /c "dir C:\\temp"', ("cmd", "/c", "dir C:\\temp")),
        # 载荷之后的尾巴一并记入（旧实现截断在这里，导致换参数也免问）
        (
            'python -c "print(1)" https://a.example',
            ("python", "-c", "print(1)", "https://a.example"),
        ),
        ("python -u script.py --x", ("python", "-u", "script.py", "--x")),
        ("python -m pytest -q", ("python", "-m", "pytest", "-q")),
        # 仍然不记的：裸解释器、只有旗标、不可读载荷、认不出的引号转义
        ("python", ()),
        ("python -V", ()),
        ("powershell -EncodedCommand JAB4AD0AMQA=", ()),
        ('powershell -Command "Write-Host ""danger"""', ()),
    ],
    ids=[
        "ps-command", "pwsh-single", "ps-flag-before-command", "cmd-quoted-payload",
        "python-inline-with-tail", "python-script-with-args", "python-module-with-args",
        "bare-python", "flags-only", "encoded-command", "ps-doubled-quote",
    ],
)
def test_windows_shell_reaches_the_payload(command: str, expected: tuple) -> None:
    assert memo_rule(command) == expected


@pytest.mark.parametrize(
    "command",
    [
        'powershell -Command "Get-ChildItem"',
        'cmd /c "dir C:\\temp"',
        'python -c "print(1)" https://a.example',
        "python -m pytest -q",
    ],
)
def test_every_memorized_rule_round_trips_verbatim(command: str) -> None:
    """记下来的串必须一字不差读回来——读回来变形就意味着免问范围和批准的不是同一件事。"""
    rule = memo_rule(command)
    assert rule, command
    assert prefix_rule(render_rule(rule)) == rule


def test_render_keeps_single_quoted_payload_readable() -> None:
    """单引号包裹会写出 'print('x')' 这种读不回来的串；换双引号后必须恒等。"""
    rule = memo_rule("""python -c "print('x')\"""")
    assert rule == ("python", "-c", "print('x')")
    assert prefix_rule(render_rule(rule)) == rule
    # 载荷带空格时才是引号说了算的那一格
    spaced = ("python", "-c", "print(1 + 2)")
    assert render_rule(spaced) == 'python -c "print(1 + 2)"'
    assert prefix_rule(render_rule(spaced)) == spaced


def test_rendered_metachar_payload_is_still_one_command() -> None:
    """带 ``;`` 的内联正文裸写出去会被壳拆成两条：渲染必须把它锁在引号里。

    挑的是**载荷里没有空格**那一格（``print(1);print(2)``）：只按"含空白才加引号"
    判定时，这一串会渲染成 ``python -c print(1);print(2)``——卡片上写着一条命令，
    贴回壳里是两条。有空格的载荷靠空白就已经被引号包住了，钉不住这一格。
    """
    command = 'python -c "print(1);print(2)"'
    rule = memo_rule(command)
    assert rule == ("python", "-c", "print(1);print(2)")
    rendered = render_rule(rule)
    assert split_segments(rendered) == [rendered], rendered
    assert prefix_rule(rendered) == rule

    # 带控制符又带空格的常见形状，同样只能是一段
    both = memo_rule('python -c "import sys;print(1)"')
    assert split_segments(render_rule(both)) == [render_rule(both)]
    # 卡片上那一串（suggested_prefix / 留痕）就是这条命令，不是两条
    assert memoizable_prefix("run_shell", command) == rendered


def test_payload_that_cannot_be_rendered_verbatim_is_not_memorized() -> None:
    """载荷里同时有空白和双引号：加引号会写出一条读不回来的串 ⇒ 这条不记。

    读不回来的规则比批准的那条命令更宽（回读会散成更多 token），所以宁可不给档位。
    对照：换个写法（外层双引号）同一件事就能记下来——不是"PowerShell 用不了"，是这一串。
    """
    blocked = """python -c 'print("a b")'"""
    assert memo_rule(blocked) == ()
    assert memoizable_prefix("run_shell", blocked) == ""
    policy = PermissionPolicy()
    verdict = _decide(policy, blocked)
    assert verdict.outcome == ASK and verdict.memo == ""
    assert [item["scope"] for item in approval_options("run_shell", verdict.memo, tool_scope=False)] == ["once"]
    # 同一段代码换一种包裹就能记，且记下的读回来一模一样
    printable = """python -c "print('a b')" """
    rule = memo_rule(printable.strip())
    assert rule and prefix_rule(render_rule(rule)) == rule


# --------------------------------------------------------------------------- #
# 2. 不可逆：Windows 删除形状必须先进闸，且闸门排在免问匹配之前# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "command",
    [
        r'powershell -Command "Remove-Item -Recurse C:\target"',
        r'pwsh -Command "del C:\Users\me\Desktop\a.txt"',
        r'cmd /c "del /q C:\Windows\temp\x"',
        r"remove-item -Recurse -Force .\build",
        r"reg delete HKLM\Software\Foo",
        r"git push origin main --force",
    ],
)
def test_windows_deletes_count_as_irreversible(command: str) -> None:
    assert is_irreversible(command) is True


@pytest.mark.parametrize(
    "command",
    [
        'powershell -Command "Get-ChildItem"',
        'cmd /c "dir C:\\temp"',
        "pytest -q",
    ],
)
def test_ordinary_windows_commands_stay_reversible(command: str) -> None:
    """闸门不能顺手把普通命令也判死：那会让前缀档位在这台机器上永远发不出来。"""
    assert is_irreversible(command) is False


def test_prefix_rule_never_overrides_the_irreversible_gate() -> None:
    """耦合：``git push`` 这条规则字面覆盖 ``git push … --force``，免问必须先被不可逆闸住。

    这条是本批最危险的一格：先补 Windows 删除形状（判定侧）再收窄免问（记忆侧），
    顺序反过来就会把漏判变成扩权。摘掉 :func:`decide` 里排在匹配之前那道闸，
    或摘掉 :func:`memo_rule` 开头的同一道闸，这一格都会红。
    """
    policy = PermissionPolicy()
    policy.remember("run_shell", {"command": "git push origin main"}, SCOPE_PREFIX)
    assert policy.approved_prefixes, "批准普通推送应当留下一条规则"
    verdict = _decide(policy, "git push origin main --force")
    assert verdict.outcome == ASK, verdict
    assert verdict.rule == "irreversible"

    policy2 = PermissionPolicy()
    policy2.persistent_prefixes = {("git", "push")}
    assert _decide(policy2, "git push origin main --force").outcome == ASK

    # 同一形状换到 PowerShell：载荷里就是递归删除，逐条确认，卡上也不给前缀档
    recursive_delete = r'powershell -Command "Remove-Item -Recurse C:\target"'
    verdict = _decide(policy, recursive_delete)
    assert verdict.rule == "irreversible"
    assert verdict.memo == ""
    options = approval_options("run_shell", verdict.memo, tool_scope=False)
    assert [item["scope"] for item in options] == ["once"]
    # 闸门在源头：任何调用点直接问档位，也不会为不可逆命令发出"总是允许"
    assert memo_rule(recursive_delete) == ()
    assert memoizable_prefix("run_shell", recursive_delete) == ""
    assert memo_rule(r"cmd /c del C:\Users\me\Desktop\a.txt") == ()


# --------------------------------------------------------------------------- #
# 3. 撞车表：记下的串不得比执行的更宽
# --------------------------------------------------------------------------- #
def test_approved_command_auto_approves_itself_and_nothing_wider() -> None:
    """两个方向同时成立：批准的那条下次不再挂卡；换载荷/换参数/加尾巴都要重新问。"""
    cases = [
        (
            'python -c "print(1)"',
            ["python -c 'print(2)'", 'python -c "print(1)" https://a.example', "python -c print(1) extra"],
        ),
        (
            'python -c "print(1)" https://a.example',
            ['python -c "print(1)"', 'python -c "print(1)" https://b.example'],
        ),
        (
            'powershell -Command "Get-ChildItem"',
            [
                'powershell -Command "Get-ChildItem -Recurse C:\\"',
                'powershell -Command "Remove-Item x"',
                'pwsh -Command "Get-ChildItem"',
            ],
        ),
        (
            'cmd /c "dir C:\\temp"',
            ['cmd /c "del C:\\temp"', 'cmd /c "dir C:\\other"'],
        ),
    ]
    for approved, escapes in cases:
        rule = memo_rule(approved)
        assert rule, f"{approved} 算不出规则，本批的档位发不出来"
        # 同一条命令（换个引号写法也算同一条）必须命中
        assert covered_by_rules(approved, [rule]) == render_rule(rule) or covered_by_rules(
            approved, [rule]
        )
        for escape in escapes:
            assert covered_by_rules(escape, [rule]) is None, f"{approved} ⇒ 免问了 {escape}"


def test_ps_doubled_quote_payload_is_cut_and_therefore_refused() -> None:
    """PowerShell 的 ``""`` 转义只剥一层引号读不出来：载荷被切残 ⇒ 这条不记。

    实物（本批改动前）：截断出来的规则只有三段 ``powershell -Command "Write-Host "``，
    于是不同转义载荷会塌进同一条规则。现在两头都堵：
    源头不记（:func:`memo_rule` 拒掉这种引号形状），匹配侧等长约束也不让截断规则
    覆盖参数更多的命令。
    """
    first = 'powershell -Command "Write-Host ""danger"""'
    second = 'powershell -Command "Write-Host ""evil"""'
    argv_first = tokenize(split_segments(first)[0])
    # 切残的实物：载荷本该是一整段 `Write-Host "danger"`，读回来散成了两段
    assert argv_first == ["powershell", "-Command", "Write-Host ", "danger"]
    assert memo_rule(first) == ()
    assert memo_rule(second) == ()
    truncated = ("powershell", "-Command", "Write-Host ")
    assert covered_by_rules(second, [truncated]) is None
    assert covered_by_rules(first, [truncated]) is None
    policy = PermissionPolicy()
    policy.remember("run_shell", {"command": first}, SCOPE_PREFIX)
    assert not policy.approved_prefixes
    assert _decide(policy, second).outcome == ASK


# --------------------------------------------------------------------------- #
# 4. 真壳对照：记下的规则必须就是壳真正交给执行程序的那一串
# --------------------------------------------------------------------------- #
# 这一节的判据是"本机默认壳怎么拆这一串"，样本写的是 cmd.exe 的语义（`cmd /c`、
# `powershell -Command`、双引号包裹）。Linux runner 上 /bin/sh 语义不同，
# 跑它只会得到假红——**这条判据在 POSIX 壳上仍未证明**，是记在文档里的欠账，不是这里的失败。
WIN_SHELL = pytest.mark.skipif(
    sys.platform != "win32", reason="引号会被壳吃掉：样本按 cmd.exe 语义写，POSIX 壳上另说"
)

_BODY_A = "import sys;print('A');print(len(sys.argv));print('|'.join(sys.argv[1:]))"


def _host_env() -> dict:
    """让 `python` 这个裸名在 cmd 里也解析得到：与后端跑命令时同一个 PATH 思路。"""
    import os

    extra = str(Path(sys.executable).parent)
    return {**os.environ, "PATH": extra + os.pathsep + os.environ.get("PATH", "")}


def _host_report(command: str) -> tuple[str, int, list[str]]:
    """把 ``command`` 交给本机默认壳（与 run_shell 同一个 shell=True 路径）自报收到的参数。

    载荷自己打印标记 A、``len(sys.argv)`` 与 ``"|".join(sys.argv[1:])``，
    所以一次执行同时证明两件事：跑的就是我们记下的那段正文，以及尾巴一个不多一个不少。
    """
    import subprocess

    out = subprocess.run(
        command, capture_output=True, text=True, shell=True, env=_host_env(), timeout=120  # noqa: S602
    )
    assert out.returncode == 0, f"{command} -> rc={out.returncode} {out.stderr}"
    marker, count, *rest = out.stdout.splitlines()
    tail = rest[0].split("|") if rest and rest[0] else []
    return marker, int(count), tail


@WIN_SHELL
def test_real_host_argv_equals_the_memorized_rule() -> None:
    """批准的命令换个尾巴，壳收到的参数就不同：规则必须整段带走，否则免问范围算错。

    本机 ``run_shell`` 走的是 ``shell=True``（cmd.exe），所以"命令字符串怎么被拆"不是
    我们自己能定的——判据只能是 cmd 实际交给执行程序的那一份 argv。
    """
    exe = Path(sys.executable).as_posix()
    with_tail = f'"{exe}" -c "{_BODY_A}" tail1 tail2'
    rule = memo_rule(with_tail)
    assert rule, with_tail
    marker, count, tail = _host_report(with_tail)
    assert marker == "A", "跑的不是记在规则里的那段正文"
    assert rule[0] == _basename(sys.executable).lower()
    assert rule[1] == "-c" and rule[2] == _BODY_A, rule
    # 壳收到三个参数：`-c`（python 自己顶替 argv[0]）+ 两个尾巴，一个不多一个不少
    assert count == 3, count
    assert tail == ["tail1", "tail2"], tail
    assert list(rule[3:]) == tail, (rule, tail)
    assert len(rule) == 2 + count, (rule, count)

    # 对照格（必须真的不同）：同一载荷去掉尾巴，壳收到的参数就少两个
    no_tail = f'"{exe}" -c "{_BODY_A}"'
    assert _host_report(no_tail)[1] == 1
    assert memo_rule(no_tail) == ("python", "-c", _BODY_A)
    # 所以批准带尾巴的那条不该免问不带尾巴的那条，反之亦然
    assert covered_by_rules(no_tail, [rule]) is None
    assert covered_by_rules(with_tail, [memo_rule(no_tail)]) is None

    # 换尾巴也不行：换参数就是换命令
    other_tail = f'"{exe}" -c "{_BODY_A}" tail1 tail3'
    assert covered_by_rules(other_tail, [rule]) is None


def test_rendered_rule_reexecutes_identically() -> None:
    """卡片/账本上的那一串重新跑一遍，必须与用户批准的那条产生同一个结果。

    引号包裹方式改了（单引号 → 双引号、带空格才加引号）就必须在真壳上重新证明一次：
    写出去的那串若被 cmd 拆成别的样子，"总是允许 X"里的 X 就不是用户批的东西。
    """
    import subprocess

    samples = [
        'cmd /c "echo hello"',
        'python -c "print(1)"',
        'python -c "print(1 + 2)"',
        "python -c \"print('x')\"",
        # 载荷里带 shell 控制符：这一串正是本批在真 cmd 上量出来的走形处
        'python -c "import sys;print(len(sys.argv))" t1',
        'powershell -NoProfile -Command "Write-Host ok"',
        # 只有控制符、没有空白的载荷：把包裹条件退回"只按空白判断"，这两条当场就红。
        # 实测（E:/tmp/m87/separator_probe.py，真 cmd.exe）：`|` 裸写 ⇒ 退出码 255，
        # `&` 裸写 ⇒ 退出码 0 且**多跑了一条命令**。上面几条都不敏感（它们的 token 本来就带空白），
        # 没有这两条，这个测试对"引号只管空白"的老写法是抓不到的。
        'python -c "print(\'a|b\')"',
        'python -c "print(\'a&b\')"',
    ]
    for command in samples:
        rule = memo_rule(command)
        assert rule, command
        rendered = render_rule(rule)
        original = subprocess.run(
            command, capture_output=True, text=True, shell=True, env=_host_env(), timeout=120  # noqa: S602
        )
        replay = subprocess.run(
            rendered, capture_output=True, text=True, shell=True, env=_host_env(), timeout=120  # noqa: S602
        )
        assert original.returncode == 0, (command, original.stderr)
        assert replay.returncode == original.returncode, (command, rendered, replay.stderr)
        assert replay.stdout == original.stdout, (command, rendered, original.stdout, replay.stdout)
        assert original.stdout.strip(), (command, "空输出证明不了同一件事，换一条会打印东西的样本")
