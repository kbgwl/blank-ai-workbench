"""编程纪律（M 批）：破坏性 git 进不可逆闸门、git 补 blame、提示词补编程规则。

证据来自两家的真实系统提示词（抓包原文，不在仓库）：

- Claude：「NEVER run destructive git commands (push --force, reset --hard, checkout .,
  restore ., clean -f, branch -D) unless the user explicitly requests these actions」；
  「NEVER create documentation files (*.md) or README files unless explicitly requested」；
  「Write code that reads like the surrounding code: match its comment density, naming,
  and idiom」；「Run typechecks and **investigate errors** — don't dismiss as "unrelated"」。
- Codex：「Do not attempt to fix unrelated bugs or broken tests. It is not your
  responsibility.」「Fix the problem at the root cause rather than applying surface-level
  patches」「Do not add inline comments within code unless explicitly requested」
  「NEVER add copyright or license headers unless specifically requested」
  「Do not `git commit` your changes or create new git branches unless explicitly
  requested」「Use `git log` and `git blame` to search the history of the codebase」
  「Existing or new changes belong to the user unless you know otherwise, so you preserve
  them」「start as specific as possible to the code you changed, then make your way to
  broader tests」。

三条判据各自能失败：判据函数（正例命中 / 反例不误伤）、策略层（bypass 模式仍问、
不可逆命令记不出前缀免问）、提示词契约（段落存在且排在做事规则之前）。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from blank_api.services.agent.graph import SYSTEM_PROMPT
from blank_api.services.permissions import (
    ASK,
    MODE_BYPASS,
    SCOPE_PREFIX,
    PermissionPolicy,
    is_irreversible,
)
from blank_api.services.tools.base import ToolContext, ToolDefinition
from blank_api.services.tools.git import READ_ONLY_SUBCOMMANDS, _argv, _git, git_tools

# ── 1. 破坏性 git：该拦的拦，正常的 git 一条都不能误伤 ─────────────

DESTRUCTIVE = [
    # Claude 原文点名的六种，外加等价写法。
    "git push --force",
    "git push -f origin main",
    "git push origin main --force",
    "git reset --hard",
    "git reset --hard HEAD~1",
    "git checkout -- .",
    "git checkout .",
    "git checkout -f main",
    "git checkout HEAD -- src/app.py",
    "git restore .",
    "git restore src/app.py",
    "git clean -fd",
    "git clean -fdx",
    "git clean -f",
    "git branch -D feat/x",
    # 加 -C 前缀照样认得出来（否则换个写法就绕过去了）。
    "git -C repo reset --hard",
    "git -C repo clean -f",
]

SAFE = [
    "git status",
    "git status -s",
    "git log --oneline -5",
    "git diff HEAD",
    "git diff -- src/app.py",
    "git add -A",
    'git commit -m "fix: tidy up"',
    # 建分支不是删分支；切换分支不动工作区里已跟踪的改动。
    "git checkout -b feature/x",
    "git checkout main",
    "git switch main",
    # --staged 只动暂存区，可逆。
    "git restore --staged src/app.py",
    # dry-run 不删东西。
    "git clean -n",
    "git clean -nd",
    # -d 只删已合并分支，git 自己会拒。
    "git branch -d merged",
    "git reset --soft HEAD~1",
    "git stash list",
    "git blame -- src/app.py",
    "git -C repo status",
]


@pytest.mark.parametrize("command", DESTRUCTIVE)
def test_destructive_git_is_irreversible(command: str) -> None:
    assert is_irreversible(command) is True, command


@pytest.mark.parametrize("command", SAFE)
def test_ordinary_git_commands_are_not_irreversible(command: str) -> None:
    """反例比正例重要：把正常的 git 操作一起拦掉，以后每一步都要问一遍。"""
    assert is_irreversible(command) is False, command


def _tool(name: str = "run_shell", risk: str = "high") -> ToolDefinition:
    async def handler(context, arguments):  # pragma: no cover - 只用于构造定义
        raise AssertionError("handler should not be called")

    return ToolDefinition(
        name=name,
        description="d",
        input_schema={"type": "object"},
        risk_level=risk,
        handler=handler,
    )


def _decide(policy: PermissionPolicy, command: str):
    return policy.decide(_tool(), {"command": command})


def test_bypass_mode_still_asks_before_destroying_work() -> None:
    policy = PermissionPolicy(mode=MODE_BYPASS)
    for command in ("git reset --hard", "git clean -fd", "git branch -D feat"):
        decision = _decide(policy, command)
        assert decision.outcome == ASK, command
        assert decision.rule == "irreversible", command


def test_prefix_memory_never_covers_destructive_git() -> None:
    policy = PermissionPolicy()
    policy.remember("run_shell", {"command": "git reset --hard"}, SCOPE_PREFIX)

    assert _decide(policy, "git reset --hard").outcome == ASK
    assert policy.approved_prefixes == set()


# ── 2. git blame：Codex 要求「查历史先用 log/blame」 ────────────────


def test_blame_is_a_read_only_subcommand() -> None:
    assert "blame" in READ_ONLY_SUBCOMMANDS
    tool = git_tools()[0]
    assert tool.input_schema["properties"]["subcommand"]["enum"] == list(READ_ONLY_SUBCOMMANDS)


def test_blame_arguments_separate_the_path() -> None:
    assert _argv({"subcommand": "blame", "path": "src/app.py"}) == [
        "git",
        "blame",
        "--",
        "src/app.py",
    ]


async def test_blame_without_path_explains_instead_of_running_git(tmp_path: Path) -> None:
    context = ToolContext(root=tmp_path, task_id="t-git-blame", session_factory=None)
    outcome = await _git(context, {"subcommand": "blame"})

    assert outcome.ok is False
    assert "path" in outcome.message


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    if shutil.which("git") is None:
        pytest.skip("本机没有 git")
    repo = tmp_path / "repo"
    repo.mkdir()

    def run(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=repo, check=True,
            capture_output=True, text=True,
        )

    run("init", "-q")
    run("config", "user.email", "probe@example.com")
    run("config", "user.name", "Probe Author")
    (repo / "app.py").write_text("print('hello blame')\n", encoding="utf-8")
    run("add", "app.py")
    run("commit", "-q", "-m", "init")
    return repo


async def test_blame_reports_who_wrote_the_line(git_repo: Path) -> None:
    context = ToolContext(root=git_repo, task_id="t-git-blame", session_factory=None)
    outcome = await _git(context, {"subcommand": "blame", "path": "app.py"})

    assert outcome.ok is True
    assert "Probe Author" in (outcome.output or "")
    assert "hello blame" in (outcome.output or "")


# ── 3. 提示词契约：编程纪律存在，且排在做事规则之前 ──────────────────


class TestPromptContract:
    def test_coding_discipline_precedes_task_rules(self) -> None:
        assert SYSTEM_PROMPT.index("编程纪律") < SYSTEM_PROMPT.index("做事规则")

    def test_minimal_diff_and_root_cause(self) -> None:
        assert "改根因" in SYSTEM_PROMPT
        assert "Do not attempt to fix unrelated bugs or broken tests" in SYSTEM_PROMPT

    def test_style_and_comments(self) -> None:
        assert "Write code that reads like the surrounding code" in SYSTEM_PROMPT
        assert "注释只写「为什么」" in SYSTEM_PROMPT
        assert "不加版权头" in SYSTEM_PROMPT

    def test_files_and_write_path(self) -> None:
        assert "NEVER create documentation files unless explicitly requested" in SYSTEM_PROMPT
        assert "优先改现有文件" in SYSTEM_PROMPT
        # 写文件只走 apply_patch，shell 重定向绕过了审计与回滚。
        assert "写文件只走 apply_patch" in SYSTEM_PROMPT

    def test_validation_ladder_and_honest_errors(self) -> None:
        assert "验证从最小范围开始" in SYSTEM_PROMPT
        assert "与本次改动无关" in SYSTEM_PROMPT
        assert "真跑一遍看行为" in SYSTEM_PROMPT

    def test_git_hygiene(self) -> None:
        assert "不提交、不建分支、不推送" in SYSTEM_PROMPT
        assert "已有的改动属于用户" in SYSTEM_PROMPT
        assert "git log/blame" in SYSTEM_PROMPT
