"""#69：内联解释器代码必须挂卡，不能按 medium 走 `risk:auto`。

`assess_risk` 过去只认破坏性命令与联网；`python -c "open('x','w').write('…')"`
算 medium，默认审批模式直接执行，卡片一次都不出现。这条与 #54 的批准记忆无关——
它不是"记住了所以放行"，而是根本没问。

判定口径：
1. 解释器后跟内联代码旗标（`-c` / `-e` / `-Command` / `cmd /c` / `bash -lc`）⇒ high；
2. 旗标出现在第一个非旗标参数之后就只是脚本参数（`python script.py -c foo`）⇒ 仍是 medium；
3. `-m 模块` 是模块入口，不是内联正文 ⇒ medium；
4. 复合命令任一段命中即 high（不能只看第一段）。
"""

import pytest

from blank_api.services.permissions import ALLOW, ASK, PermissionPolicy
from blank_api.services.tools.shell import assess_risk, shell_tools

INLINE = [
    ('python -c "print(1)"', "python-c"),
    ("python -I -u -c 'print(1)'", "python-flags-before-body"),
    ("py -c 'print(1)'", "py-c"),
    ('node -e "console.log(1)"', "node-e"),
    ("node -pe '1+1'", "node-combined-flags"),
    ("perl -e 'print 1'", "perl-e"),
    ("ruby -e 'puts 1'", "ruby-e"),
    ("pwsh -Command 'Get-ChildItem'", "pwsh-command"),
    ("powershell -NoProfile -EncodedCommand ZQBjAGgAbwA=", "powershell-encoded"),
    ("bash -lc 'ls'", "bash-combined-lc"),
    ("cmd /c dir", "cmd-c"),
    ('C:\\Python312\\python.exe -c "print(1)"', "python-abs-path"),
]

PLAIN = [
    ("python -m pytest -q", "python-module"),
    ("python script.py -c foo", "flag-after-payload"),
    ("node script.js --check", "node-script"),
    ("py -V", "python-version"),
    ("pytest -q", "pytest"),
    ("git status -s", "git-status"),
    ("npm run build", "npm-run"),
]


@pytest.mark.parametrize(("command", "case"), INLINE, ids=[case for _, case in INLINE])
def test_inline_commands_are_high(command: str, case: str) -> None:
    assert assess_risk(command) == "high", case


@pytest.mark.parametrize(("command", "case"), PLAIN, ids=[case for _, case in PLAIN])
def test_non_inline_commands_stay_medium(command: str, case: str) -> None:
    assert assess_risk(command) == "medium", case


def test_compound_command_is_high_if_any_segment_is_inline() -> None:
    assert assess_risk("pytest -q && python -c 'print(1)'") == "high"
    assert assess_risk("pytest -q && node -e 'console.log(1)'") == "high"
    # 两段都不是内联代码时仍是 medium
    assert assess_risk("pytest -q && npm run build") == "medium"


def _run_shell_decision(command: str):
    tool = next(item for item in shell_tools() if item.name == "run_shell")
    return PermissionPolicy().decide(tool, {"command": command})


def test_inline_write_is_asked_instead_of_auto_approved() -> None:
    # 改动前的行为：medium ⇒ "risk:auto" 直接放行，这张卡根本不会出现。
    verdict = _run_shell_decision("python -c \"open('secrets.txt','w').write('x')\"")
    assert verdict.outcome == ASK
    assert verdict.risk == "high"
    assert verdict.memo == "python -c open('secrets.txt','w').write('x')"


def test_plain_script_still_auto_approves() -> None:
    # 收紧只针对内联正文；普通脚本调用维持原来的 medium/risk:auto。
    verdict = _run_shell_decision("python script.py")
    assert verdict.outcome == ALLOW
    assert verdict.rule == "risk:auto"
