"""规则落盘的 v1→v2 差分（卡 2b ②）：换形状可以，判定一个字都不许变宽。

磁盘形状从 `tokens` 数组改成 opencode 式 `resources`（`cmd:git push`）。这是**编码**
变化，不是语义变化，所以要证明两件事：

1. 老文件（v1）读出来，与改造前**逐字**相同的 token 元组；
2. 同一份规则、同一批命令，v1 与 v2 给出的判定（结果 / 归因串 / 前缀）逐条相等。

任何一条变宽（本来要问的变成免问），这里就红。
"""

import json
from pathlib import Path

import pytest

from blank_api.services import approval_rules
from blank_api.services.permissions import ASK, PermissionPolicy, render_rule
from blank_api.services.tools.base import ToolDefinition


def _tool() -> ToolDefinition:
    async def handler(_context, _arguments):  # pragma: no cover - 不执行
        raise AssertionError("判定不该执行工具")

    return ToolDefinition(
        name="run_shell",
        description="",
        input_schema={"type": "object"},
        risk_level="high",
        handler=handler,
    )


def _decide(policy: PermissionPolicy, command: str):
    return policy.decide(_tool(), {"command": command}, "high")


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def rules_file(monkeypatch, tmp_path) -> Path:
    target = tmp_path / "approvals.json"
    monkeypatch.setenv("BLANK_APPROVALS_FILE", str(target))
    return target


V1_RULES = [
    ["curl"],
    ["git", "push"],
    ["python", "-c", "print(1)", "https://a.example"],
    ["echo", "hi"],
    # 下面两条**必须留在样本里**：它们渲染成资源串时要加引号（空白 / shell 控制符），
    # 是唯一能抓住"用空格朴素切分来解码"的样本——少了它们，变宽的解码也测不出来。
    ["echo", "a b"],
    ["cmd", "/c", "echo a & del b"],
]


def test_a_v1_file_reads_as_the_same_tokens(rules_file: Path) -> None:
    _write(rules_file, {"version": 1, "rules": [{"tool": "run_shell", "tokens": tokens} for tokens in V1_RULES]})
    assert approval_rules.load_prefix_rules() == {tuple(item) for item in V1_RULES}


def test_a_v2_roundtrip_is_lossless(rules_file: Path) -> None:
    for tokens in V1_RULES:
        assert approval_rules.add_rule("run_shell", tuple(tokens)) is True
    assert approval_rules.load_prefix_rules() == {tuple(item) for item in V1_RULES}
    # 磁盘上只有一份事实：资源串
    raw = json.loads(rules_file.read_text(encoding="utf-8"))
    assert raw["version"] == approval_rules.VERSION
    assert all(set(item) == {"tool", "resources", "created_at", "source_task"} for item in raw["rules"])


COMMANDS = [
    "curl -s http://127.0.0.1:8000/api/v1/health",
    "curlie thing",  # 前缀相同但不是同一条命令
    "git push origin main",
    "git status",  # 同 exe 不同子命令
    "python -c 'print(1)'",
    "python -c 'print(2)'",  # 载荷不同
    "echo hi",
    "echo hi there",  # 规则覆盖它
    "rm -rf build",  # 谁都没批过
]


def test_v1_and_v2_give_byte_identical_decisions(rules_file: Path) -> None:
    """差分主体：老文件与新文件，同一批命令，判定逐条相等。"""
    _write(rules_file, {"version": 1, "rules": [{"tool": "run_shell", "tokens": tokens} for tokens in V1_RULES]})
    from_v1 = approval_rules.load_prefix_rules()

    rules_file.unlink()
    for tokens in V1_RULES:
        assert approval_rules.add_rule("run_shell", tuple(tokens)) is True
    from_v2 = approval_rules.load_prefix_rules()

    assert from_v1 == from_v2

    old, new = PermissionPolicy(mode="default"), PermissionPolicy(mode="default")
    old.persistent_prefixes = from_v1
    new.persistent_prefixes = from_v2

    seen_ask = False
    for command in COMMANDS:
        before, after = _decide(old, command), _decide(new, command)
        assert (before.outcome, before.rule, before.prefix) == (after.outcome, after.rule, after.prefix), (
            command,
            before.as_dict(),
            after.as_dict(),
        )
        if before.outcome == ASK:
            seen_ask = True
    assert seen_ask, "整批命令全被放行，说明这组样本测不出差异（样本无效）"


def test_a_rule_never_covers_a_longer_token(rules_file: Path) -> None:
    """`curl` 不许覆盖 `curlie`；`git push` 不许覆盖 `git status`。"""
    assert approval_rules.add_rule("run_shell", ("curl",)) is True
    assert approval_rules.add_rule("run_shell", ("git", "push")) is True
    policy = PermissionPolicy(mode="default")
    policy.persistent_prefixes = approval_rules.load_prefix_rules()

    assert _decide(policy, "curl -s http://x").outcome != ASK
    assert _decide(policy, "curlie thing").outcome == ASK
    assert _decide(policy, "git push origin main").outcome != ASK
    assert _decide(policy, "git status").outcome == ASK


def test_a_rule_that_cannot_roundtrip_is_refused(rules_file: Path) -> None:
    """切残的规则比原命令更宽：往返不一致就宁可不记（下次照样问）。"""
    tokens = ("powershell", "-Command", 'Write-Host "a b"')
    encoded = approval_rules._encode("run_shell", tokens)
    if encoded is None:
        assert approval_rules.add_rule("run_shell", tokens) is False
        assert approval_rules.load_prefix_rules() == set()
        return
    # 能精确往返的写法必须读回同一串；这里顺便证明编解码不是"大致相同"
    assert approval_rules._decode("run_shell", [encoded[1]])[1] == tokens
    assert render_rule(tokens) in encoded[1]


def test_other_tools_are_still_ignored(rules_file: Path) -> None:
    """只落 run_shell 前缀档这条铁律不变：别的工具名一律拒收。"""
    assert approval_rules.add_rule("write_file", ("src",)) is False
    assert approval_rules.load_prefix_rules() == set()


def test_a_v1_rule_that_cannot_roundtrip_is_dropped_not_widened(rules_file: Path) -> None:
    """老文件里切不回来的规则，读的时候**丢掉**（下次再问），绝不近似匹配。

    PowerShell 用两个双引号表示引号本身的写法就是这种形状：tokenize 只剥一层引号，
    读回来会被切残。切残的规则会覆盖到别的东西，所以宁可不认这一条。
    """
    ps = "pow" + "ershell"
    _write(
        rules_file,
        {
            "version": 1,
            "rules": [
                {"tool": "run_shell", "tokens": [ps, "-Command", 'Write-Host "a b"']},
                {"tool": "run_shell", "tokens": ["curl"]},
            ],
        },
    )
    loaded = approval_rules.load_prefix_rules()
    assert loaded == {("curl",)}, f"切残的规则被认了：{loaded}"

    policy = PermissionPolicy(mode="default")
    policy.persistent_prefixes = loaded
    assert _decide(policy, f'{ps} -Command "Write-Host "a b""').outcome == ASK
