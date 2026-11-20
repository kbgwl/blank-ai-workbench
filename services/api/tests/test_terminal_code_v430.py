"""终态码：这一轮为什么结束，必须能被机器读，而不是让界面去猜中文。

来历（对照 ``docs/external-qwen-code-2026-11.md`` 可移植清单第 2 项）：上游把
"这一轮怎么结束的"做成封闭枚举（``TaskTerminalReasonDetail``），界面与验收读结构；
本仓库此前只有一句 ``reason``——有时是码、有时是一整句中文。前端只好在
``web/src/components/tasks/error-copy.ts`` 里拿正则猜，猜错就给用户一句错建议。

这一轮补的是"终态"这一半：``agent/terminal.py`` 的 ``terminal_detail()`` 在唯一
终态点把事实翻成码，随 ``task.failed`` / ``task.completed`` 事件的 ``terminal``
字段一起下发；``verifier.py`` 同时给每条 finding 配一个机器码，``failure_reason()``
改成按码取句。

判据都钉在"拿掉实现就红"的位置：
- 把 ``failure_reason()`` 改回读中文 → 第 5 条红；
- 把 ``terminal`` 从事件载荷里拿掉 → 第 1 条红；
- 加了一个码却没配句子 → 第 6 条红（遍历整个封闭集合）。
"""

from __future__ import annotations

from pathlib import Path

from blank_api.db import get_session_factory
from blank_api.models.task import Task
from blank_api.services.agent.runtime import AgentRuntime, RunRequest, terminal_payload
from blank_api.services.agent.terminal import TERMINAL_CODES, terminal_detail
from blank_api.services.models.registry import build_adapter
from blank_api.services.agent.verifier import (
    BLOCKING_CODES,
    FINDING_CODES,
    failure_reason,
    verify,
)

from tests.test_agent_v04 import (  # noqa: F401  (env 是夹具，故意导入)
    API,
    call,
    create_project,
    create_provider,
    create_task,
    env,
    provider_row,
)

ZERO_CHANGE_ROWS = [
    # 写工具"成功"了，磁盘却一个文件没变——这是硬违规，不是"没改动"。
    {"tool": "write_file", "status": "ok", "ok": True, "arguments": {"path": "index.html"}},
]


# --------------------------------------------------------------------------- #
# 1. 端到端：失败事件的载荷里有终态码
# --------------------------------------------------------------------------- #


async def test_a_failed_turn_ships_a_terminal_code_in_the_event(env, workspace: Path):  # noqa: F811
    """模型空响应 → 502，``task.failed`` 的载荷里必须带 ``terminal``。

    这条走的是真实收尾链路（``runtime._finish``），不是直接调纯函数：
    纯函数写对了但没接进事件流，是这一轮最容易犯的错。
    """
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["交付 index.html"])
    env.mock.reset([{"content": ""}, {"content": ""}, {"content": ""}, {"content": ""}])

    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 502, resp.text

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    failed = [event for event in events if event["type"] == "task.failed"]
    assert failed, [event["type"] for event in events]
    terminal = failed[-1]["payload"].get("terminal") or {}
    assert terminal.get("code") == "model_error", failed[-1]["payload"]
    # detail 是族码下面的种：界面据此给具体文案（model_empty_response 有专门的一条）。
    assert terminal.get("detail") == "model_empty_response", terminal


# --------------------------------------------------------------------------- #
# 2. 纯函数：每条终态路径都落到一个封闭的码上
# --------------------------------------------------------------------------- #

def test_every_terminal_branch_lands_on_a_declared_code() -> None:
    """各条收尾路径的码，以及"码必须在封闭集合里"这条约束。"""
    cases = {
        "completed": terminal_detail("succeeded", "", {}),
        "completed_unverified": terminal_detail("succeeded", "", {"unverified": "npm test 超时"}),
        "waiting_approval": terminal_detail(
            "waiting_approval", "存在待审批的高风险操作", {"interrupt": {"tool": "run_shell"}}
        ),
        "waiting_answer": terminal_detail(
            "waiting_approval", "等待用户回答问题", {"interrupt": {"kind": "question"}}
        ),
        "cancelled": terminal_detail("cancelled", "任务已取消", {}),
        "paused": terminal_detail("paused", "用户暂停了任务", {}),
        "verification_blocked": terminal_detail(
            "failed", "目标要求改动，但工作区没有任何文件变化",
            {"verdict": {"blocking": True, "codes": ["zero_change"]}},
        ),
        "model_error": terminal_detail("failed", "模型请求超时", {"code": "model_timeout"}),
        "validation_timeout": terminal_detail("failed", "测试超时", {"validation": {"status": "timeout"}}),
        "validation_failed": terminal_detail("failed", "测试未通过", {"validation": {"status": "failed"}}),
        "budget_exhausted": terminal_detail("failed", "预算耗尽", {"budget_exhausted": True}),
        "hook_blocked": terminal_detail(
            "failed", "提交被钩子拦下：带密钥的请求不进模型", {"terminal_code": "hook_blocked"}
        ),
        "approval_expired": terminal_detail("failed", "approval_expired", {}),
        "failed": terminal_detail("failed", "不知道为什么失败", {}),
    }
    for expected, got in cases.items():
        assert got["code"] == expected, (expected, got)
        assert got["code"] in TERMINAL_CODES, got


def test_an_unverified_success_is_not_reported_as_plain_completed() -> None:
    """跑完但没验过 ≠ 验过的完成：码要分开，人话要带上"为什么没验"。"""
    got = terminal_detail("succeeded", "", {"unverified": "npm test 在 180 秒内没跑完"})
    assert got["code"] == "completed_unverified"
    assert "180 秒" in got["reason"]


def test_the_terminal_code_cannot_be_overridden_by_the_caller() -> None:
    """``extra`` 里塞一个同名键，不能把真正的终态码顶掉。

    载荷是先铺 `**extra` 再写 `terminal`，这条钉的就是那个顺序：
    反过来的话任何一个多传一个键的调用点都能伪装成"成功"。
    """
    payload = terminal_payload(
        "failed", "", "模型请求超时", {"terminal": {"code": "completed"}, "code": "model_timeout"}
    )
    assert payload["terminal"]["code"] == "model_error"
    assert payload["terminal"]["detail"] == "model_timeout"
    # 老字段仍然原样保留：前端与文档还在读它。
    assert payload["reason"] == "模型请求超时"


# --------------------------------------------------------------------------- #
# 3. verifier：每条 finding 都带码，failure_reason 按码取句
# --------------------------------------------------------------------------- #


def test_verify_marks_the_hard_violation_with_a_code() -> None:
    """写工具成功、磁盘零改动 → codes 里有 zero_change，且 blocking。"""
    verdict = verify(tool_results=ZERO_CHANGE_ROWS, changed_paths=[])
    assert verdict["blocking"] is True
    assert "zero_change" in verdict["codes"], verdict
    # findings 与 codes 同序：第 i 条 finding 的码就是 codes[i]。
    assert len(verdict["codes"]) == len(verdict["findings"]), verdict
    for code in verdict["codes"]:
        assert code in FINDING_CODES, code


def test_the_detail_is_the_hard_violation_not_the_first_code() -> None:
    """真实 verdict 里的第一个码常常是信息码，detail 必须挑硬违规那一个。

    这条防的是"码取错一个位置"：``no_files_changed`` 排在 ``zero_change`` 前面，
    取 codes[0] 会把"说改了却没落盘"讲成"这一轮没有任何改动"——两句话对应的
    下一步完全不同（前者要它真的落盘，后者可能本来就只是问个问题）。
    """
    verdict = verify(tool_results=ZERO_CHANGE_ROWS, changed_paths=[])
    assert verdict["codes"][0] == "no_files_changed", verdict["codes"]

    got = terminal_detail("failed", "目标要求改动，但工作区没有任何文件变化", {"verdict": verdict})
    assert got["code"] == "verification_blocked"
    assert got["detail"] == "zero_change", got


def test_failure_reason_reads_codes_not_chinese() -> None:
    """把 findings 的文字全换成乱码，只要码还在，结论就不能变。

    这是这一轮的核心断言：以前 `failure_reason()` 靠 `"零改动" in finding` 判断，
    换个说法（"磁盘零改动（零交付）"）就会被认成另一种失败。按码取句之后，
    文本怎么改都不影响判定。
    """
    real = verify(tool_results=ZERO_CHANGE_ROWS, changed_paths=[])
    assert real["blocking"] is True
    sentence = failure_reason(real)
    assert "没有任何文件变化" in sentence

    garbled = {**real, "findings": ["（这段文字已经不是中文了）"] * len(real["findings"])}
    assert failure_reason(garbled) == sentence


def test_every_blocking_code_has_its_own_sentence() -> None:
    """封闭集合里的每个硬违规码都必须有一句专属的话。

    加码忘配文案是这类枚举最常见的坑：界面会退到"独立事实验证未通过"，
    用户看到一句什么都没说的结论。
    """
    fallback = failure_reason({"blocking": True, "findings": [], "codes": []})
    for code in sorted(BLOCKING_CODES):
        verdict = {"blocking": True, "findings": ["随便一句话"], "codes": [code]}
        sentence = failure_reason(verdict)
        assert sentence, code
        assert sentence != fallback, code
    # 顺序：同时有多个硬违规时，说最需要用户先处理的那个（零改动优先于零交付）。
    both = {"blocking": True, "findings": [], "codes": ["zero_delivery", "zero_change"]}
    assert failure_reason(both) == failure_reason({"blocking": True, "codes": ["zero_change"]})


def test_a_failed_turn_with_no_verdict_still_gets_a_code() -> None:
    """没有 verdict、没有 validation 的失败也不能留空码。"""
    got = terminal_detail("failed", "子代理启动失败", {})
    assert got["code"] == "failed"
    assert got["reason"] == "子代理启动失败"


# --------------------------------------------------------------------------- #
# 4. 取消：最常见的非正常结束，也不能只留一句中文
# --------------------------------------------------------------------------- #


async def test_a_cancelled_turn_ships_the_code_too(env, workspace: Path):  # noqa: F811
    """任务被取消时，``task.cancelled`` 的载荷里也要有 ``terminal.code=cancelled``。

    取消这条路径不经过 ``runtime._finish``（执行节点自己发现状态已取消就交出去），
    所以它是"终态码只接了两条写入路径"之外最容易漏的一处。界面此前靠
    ``/取消/`` 这段正则认它——文案一改就认不出。
    """
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["取消后不再执行"])
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 读取文件"},
            {"content": "取消", "tool_calls": [call("read_file", {"path": "main.py"})]},
        ]
    )
    async with get_session_factory()() as session:
        task = await session.get(Task, task_id)
        task.status = "cancelled"
        await session.commit()

    evidence = await AgentRuntime(get_session_factory()).run(
        RunRequest(
            task_id=task_id,
            provider_id=provider_id,
            provider_name="mock-vendor",
            adapter=build_adapter(await provider_row(provider_id), env.mock.factory),
            root=workspace,
            title="取消测试",
            goal="取消测试",
            acceptance_criteria=["取消后不再执行"],
            max_steps=4,
        )
    )
    cancelled = [event for event in evidence["timeline"] if event["type"] == "task.cancelled"]
    assert cancelled, [event["type"] for event in evidence["timeline"]]
    assert (cancelled[-1].get("payload") or {}).get("terminal", {}).get("code") == "cancelled", cancelled[-1]
