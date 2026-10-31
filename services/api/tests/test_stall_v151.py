"""空转断路器的规则测试。

样本全部来自安装版真实库（`%APPDATA%/blank-ai-workbench-desktop/blank.db`，
任务 `fd68d908…`，101 步 / 140 次调用 / 7.13M token），不是编的：

- `apply_patch` 连发 4 次 `{}` → "缺少必填参数：path"
- `run_tests` 挂起审批后连试 3 次
- `'node' 不是内部或外部命令` 3 条（中文 Windows 的 GBK 回执已乱码）
- `read_file` 同参数读 3 次、同一条 shell 命令跑 2 次

断言的重点不是"能拦住"，而是**只在证据足够时拦**：规则误伤一次，就是让一个本来
能干成的任务干不成，那比多花几万 token 更糟。
"""

from __future__ import annotations

import pytest

from blank_api.services.agent.stall import (
    KIND_APPROVAL,
    KIND_BAD_ARG,
    KIND_DENIED,
    KIND_ENV_MISSING,
    KIND_MISSING_ARG,
    KIND_NETWORK,
    KIND_SANDBOX,
    OK_REPEAT_LIMIT,
    SIGNATURE_FAIL_LIMIT,
    StallBreaker,
    classify,
    hint_for,
    missing_command,
    signature,
)

APPLY_PATCH_SCHEMA = {
    "type": "object",
    "required": ["patch"],
    "properties": {"patch": {"type": "string"}, "path": {"type": "string"}},
}

# 真实回执：GBK 落库后中文已损坏，只剩单引号包着的命令名可认。
NODE_MISSING = {
    "ok": False,
    "status": "error",
    "message": "'node' \ufffd\ufffd\ufffd\ufffd\ufffd\ufffd\ufffd\u04b2\ufffd\ufffd\ufffd\ufffd\u0433\ufffd\ufffd\ufffd\ufffd",
}
SANDBOX_DOWN = {
    "ok": False,
    "status": "error",
    "message": "工具执行失败：SandboxUnavailable",
}
APPROVAL = {
    "ok": False,
    "status": "awaiting_approval",
    "message": "工具 run_tests 的风险等级为 medium，未获得审批，已拒绝执行并生成待审批请求。",
}
MISSING_PATH = {"ok": False, "status": "error", "message": "缺少必填参数：path"}
NO_CHANGE = {"ok": False, "status": "error", "message": "补丁未产生任何变更"}
NO_HUNK = {"ok": False, "status": "error", "message": "工具参数错误：补丁缺少 @@ 段落头，无法定位修改位置"}
OK = {"ok": True, "status": "ok", "output": "done"}


class TestClassify:
    def test_missing_arg(self) -> None:
        assert classify(MISSING_PATH) == KIND_MISSING_ARG

    def test_patch_shapes_are_bad_arg(self) -> None:
        assert classify(NO_CHANGE) == KIND_BAD_ARG
        assert classify(NO_HUNK) == KIND_BAD_ARG

    def test_sandbox(self) -> None:
        assert classify(SANDBOX_DOWN) == KIND_SANDBOX

    def test_approval_beats_denied_word(self) -> None:
        # 这条回执里同时有"未获得审批"和"已拒绝执行"，必须先认成"等待审批"：
        # 认成驳回会让模型以为这条路彻底没了，而它其实只是排队。
        assert classify(APPROVAL) == KIND_APPROVAL

    def test_gbk_mojibake_command_missing(self) -> None:
        assert classify(NODE_MISSING) == KIND_ENV_MISSING
        assert missing_command(NODE_MISSING) == "node"

    def test_english_command_missing(self) -> None:
        payload = {"ok": False, "message": "'pnpm' is not recognized as an internal or external command"}
        assert classify(payload) == KIND_ENV_MISSING
        assert missing_command(payload) == "pnpm"

    def test_network_and_path(self) -> None:
        assert classify({"ok": False, "message": "当前安全设置禁止联网，到设置里打开"}) == KIND_NETWORK
        assert classify({"ok": False, "message": "命令访问了工作区以外的路径：E:\\env\\bin"}) == "path_outside"

    def test_success_is_empty(self) -> None:
        assert classify(OK) == ""

    def test_denied_status_without_keywords(self) -> None:
        assert classify({"ok": False, "status": "denied", "message": ""}) == KIND_DENIED


class TestSignature:
    def test_key_order_does_not_matter(self) -> None:
        assert signature("read_file", {"path": "a", "limit": 10}) == signature(
            "read_file", {"limit": 10, "path": "a"}
        )

    def test_timeout_is_not_intent(self) -> None:
        # 同一条命令换超时时间还是同一件事：真实账本里 2 次 `pnpm -C web test`
        # 只有 timeout 不同，按原样算会被当成两次正常调用。
        assert signature("run_shell", {"command": "x", "timeout_seconds": 60}) == signature(
            "run_shell", {"command": "x", "timeout_seconds": 240}
        )

    def test_limit_is_intent(self) -> None:
        # 但 limit 决定模型看到多少内容，换了就是另一件事，不能合并。
        assert signature("read_file", {"path": "a", "limit": 10}) != signature(
            "read_file", {"path": "a", "limit": 200}
        )

    def test_different_tools_differ(self) -> None:
        assert signature("read_file", {}) != signature("list_files", {})


class TestMissingArgLoop:
    """真实账本里代价最高的一条：`apply_patch` 连发 4 次空参数。"""

    def test_first_failure_is_not_blocked(self) -> None:
        breaker = StallBreaker()
        verdict = breaker.verdict("apply_patch", {}, APPLY_PATCH_SCHEMA)
        assert verdict.block is False

    def test_second_call_gets_the_schema(self) -> None:
        breaker = StallBreaker()
        breaker.record("apply_patch", {}, MISSING_PATH)
        verdict = breaker.verdict("apply_patch", {}, APPLY_PATCH_SCHEMA)
        # 第一次失败之后不拦，但要告诉它这个工具到底要什么——原样再发一次是它当时
        # 唯一能做的事，因为回执里的信息量和上一次完全相同。
        assert verdict.block is False
        assert "patch" in verdict.hint
        assert "string" in verdict.hint

    def test_third_identical_call_is_blocked(self) -> None:
        breaker = StallBreaker()
        for _ in range(SIGNATURE_FAIL_LIMIT):
            breaker.record("apply_patch", {}, MISSING_PATH)
        verdict = breaker.verdict("apply_patch", {}, APPLY_PATCH_SCHEMA)
        assert verdict.block is True
        assert verdict.kind == KIND_MISSING_ARG

    def test_changed_arguments_are_not_blocked(self) -> None:
        """拦的是"同一件事"，不是"这个工具"。参数变了就说明它在改，放行。"""
        breaker = StallBreaker()
        for _ in range(5):
            breaker.record("apply_patch", {}, MISSING_PATH)
        verdict = breaker.verdict(
            "apply_patch", {"patch": "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b"}, APPLY_PATCH_SCHEMA
        )
        assert verdict.block is False


class TestApprovalLoop:
    def test_pending_approval_blocks_repeat(self) -> None:
        breaker = StallBreaker()
        breaker.record("run_tests", {}, APPROVAL)
        verdict = breaker.verdict("run_tests", {})
        assert verdict.block is True
        assert verdict.kind == KIND_APPROVAL
        assert "审批" in verdict.hint

    def test_approval_clears_after_success(self) -> None:
        breaker = StallBreaker()
        breaker.record("run_tests", {}, APPROVAL)
        breaker.record("run_tests", {}, OK)
        assert breaker.verdict("run_tests", {}).block is False


class TestEnvironmentFailures:
    def test_sandbox_blocked_after_one_failure(self) -> None:
        breaker = StallBreaker()
        breaker.record("run_shell", {"command": "dir"}, SANDBOX_DOWN)
        # 沙箱起不来不是参数问题：换命令也一样。所以连第一条之后的**别的**命令也拦。
        verdict = breaker.verdict("run_shell", {"command": "node --version"})
        assert verdict.block is True
        assert verdict.kind == KIND_SANDBOX

    def test_sandbox_does_not_block_file_tools(self) -> None:
        breaker = StallBreaker()
        breaker.record("run_shell", {"command": "dir"}, SANDBOX_DOWN)
        assert breaker.verdict("read_file", {"path": "a.txt"}).block is False

    def test_missing_command_blocks_only_commands_using_it(self) -> None:
        breaker = StallBreaker()
        breaker.record("run_shell", {"command": "node --check x.js"}, NODE_MISSING)
        # 用到 node 的另一条命令：同一个失败，拦。
        assert breaker.verdict("run_shell", {"command": "pnpm exec node -v"}).block is True
        # 不用 node 的命令照跑：缺的是那条命令，不是整个工具。
        assert breaker.verdict("run_shell", {"command": "dir /b"}).block is False

    def test_command_name_with_extension(self) -> None:
        breaker = StallBreaker()
        breaker.record("run_shell", {"command": "node -v"}, NODE_MISSING)
        assert breaker.verdict("run_shell", {"command": "E:\\env\\node\\node.exe -v"}).block is True

    def test_success_clears_missing_command(self) -> None:
        breaker = StallBreaker()
        breaker.record("run_shell", {"command": "node -v"}, NODE_MISSING)
        breaker.record("run_shell", {"command": "set PATH=x;node -v"}, OK)
        assert breaker.verdict("run_shell", {"command": "node -v"}).block is False


class TestSameKindLimit:
    def test_three_bad_patches_are_warned_not_blocked(self) -> None:
        """同类连错只提示、不封工具——封了就没法证明自己改对了。"""
        breaker = StallBreaker()
        breaker.record("apply_patch", {"patch": "a"}, NO_CHANGE)
        breaker.record("apply_patch", {"patch": "b"}, NO_CHANGE)
        breaker.record("apply_patch", {"patch": "c"}, NO_CHANGE)
        verdict = breaker.verdict("apply_patch", {"patch": "d"}, APPLY_PATCH_SCHEMA)
        assert verdict.block is False
        assert verdict.kind == KIND_BAD_ARG
        assert "连错 3 次" in verdict.reason
        assert "@@" in verdict.hint

    def test_same_signature_still_blocked_after_repeats(self) -> None:
        """换了参数放行，但同一份补丁原样重发仍被拦。"""
        breaker = StallBreaker()
        for _ in range(3):
            breaker.record("apply_patch", {"patch": "same"}, NO_CHANGE)
        assert breaker.verdict("apply_patch", {"patch": "same"}, APPLY_PATCH_SCHEMA).block is True

    def test_success_resets_the_count(self) -> None:
        breaker = StallBreaker()
        breaker.record("apply_patch", {"patch": "a"}, NO_CHANGE)
        breaker.record("apply_patch", {"patch": "b"}, NO_CHANGE)
        breaker.record("apply_patch", {"patch": "c"}, OK)
        breaker.record("apply_patch", {"patch": "d"}, NO_CHANGE)
        # 成功过一次说明工具本身没坏，计数要从头算，不能接着拦。
        assert breaker.verdict("apply_patch", {"patch": "e"}, APPLY_PATCH_SCHEMA).block is False

    def test_other_tools_unaffected(self) -> None:
        breaker = StallBreaker()
        for index in range(4):
            breaker.record("run_shell", {"command": f"cmd{index}"}, SANDBOX_DOWN)
        assert breaker.verdict("read_file", {"path": "a"}).block is False


class TestRepeatSuccess:
    def test_fourth_identical_success_is_blocked(self) -> None:
        breaker = StallBreaker()
        for _ in range(OK_REPEAT_LIMIT):
            breaker.record("read_file", {"path": "a.tsx"}, OK)
        verdict = breaker.verdict("read_file", {"path": "a.tsx"})
        assert verdict.block is True
        assert verdict.kind == "repeat_ok"

    def test_third_call_still_allowed(self) -> None:
        # 读 → 改 → 再读确认是合法流程。真实账本里 read_file 同参数读了 3 次，
        # 其中一次就是改完之后复读，拦在这里等于拆掉一个有意义的动作。
        breaker = StallBreaker()
        breaker.record("read_file", {"path": "a.tsx"}, OK)
        breaker.record("read_file", {"path": "a.tsx"}, OK)
        assert breaker.verdict("read_file", {"path": "a.tsx"}).block is False

    def test_different_path_not_affected(self) -> None:
        breaker = StallBreaker()
        for _ in range(4):
            breaker.record("read_file", {"path": "a.tsx"}, OK)
        assert breaker.verdict("read_file", {"path": "b.tsx"}).block is False


class TestHints:
    @pytest.mark.parametrize(
        "kind",
        [KIND_MISSING_ARG, KIND_BAD_ARG, KIND_APPROVAL, KIND_DENIED, KIND_ENV_MISSING, KIND_SANDBOX, KIND_NETWORK],
    )
    def test_every_actionable_kind_has_a_next_step(self, kind: str) -> None:
        assert hint_for(kind, "run_shell")

    def test_hint_never_says_just_retry(self) -> None:
        # "请重试"是最没用的提示：重试正是被判定的那个动作。
        for kind in (KIND_MISSING_ARG, KIND_BAD_ARG, KIND_ENV_MISSING, KIND_SANDBOX):
            assert "请重试" not in hint_for(kind, "run_shell", APPLY_PATCH_SCHEMA)

    def test_schema_hint_shows_required_fields(self) -> None:
        hint = hint_for(KIND_MISSING_ARG, "apply_patch", APPLY_PATCH_SCHEMA)
        assert "patch" in hint and "string" in hint

    def test_schema_hint_without_schema_still_says_something(self) -> None:
        assert hint_for(KIND_MISSING_ARG, "mystery_tool", None)


class TestStats:
    def test_stats_lists_broken_environment(self) -> None:
        breaker = StallBreaker()
        breaker.record("run_shell", {"command": "node -v"}, NODE_MISSING)
        breaker.record("run_shell", {"command": "dir"}, SANDBOX_DOWN)
        stats = breaker.stats()
        assert "node" in stats["missing_commands"]
        assert any(item.startswith("run_shell:") for item in stats["broken"])
