"""P1-6：guardian 独立审查。

两条硬规则必须成立：审查失败一律 fail-open（不阻塞干活），审查意见只能收紧
（不可逆动作永远交给人）。剩下的才是"它真的能拦下一条危险命令"。
"""

import asyncio

import pytest

from blank_api.services.guardian import Guardian, GuardianVerdict, parse_verdict
from blank_api.services.models.base import AdapterConfig, ModelAdapter, ModelResponse, Usage

from tests.test_agent_v04 import (
    API,
    call,
    create_project,
    create_provider,
    create_task,
    env,  # noqa: F401  (复用阶段3 的夹具)
)


class FakeAdapter(ModelAdapter):
    def __init__(self, text: str = "", *, delay: float = 0.0, explode: bool = False) -> None:
        super().__init__(
            AdapterConfig(
                name="fake", kind="openai_compatible", base_url="https://x", api_key="", model="fake-guard"
            )
        )
        self.text = text
        self.delay = delay
        self.explode = explode
        self.calls = 0

    async def invoke(self, messages, **kwargs) -> ModelResponse:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.explode:
            raise RuntimeError("审查模型挂了")
        return ModelResponse(text=self.text, usage=Usage(prompt_tokens=42, completion_tokens=7), model="fake-guard")

    def stream(self, messages, **kwargs):  # pragma: no cover - 审查不走流式
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# 1. 解析
# --------------------------------------------------------------------------- #
def test_parse_verdict_reads_json() -> None:
    assert parse_verdict('{"verdict": "deny", "reasons": ["删除不可逆"]}') == ("deny", ["删除不可逆"])
    assert parse_verdict('{"verdict": "approve"}') == ("allow", [])
    assert parse_verdict('前缀 {"decision": "block", "reason": "force push"}') == ("deny", ["force push"])


def test_parse_verdict_handles_prose_and_garbage() -> None:
    verdict, reasons = parse_verdict("这个命令会 DELETE 数据库，必须 deny")
    assert verdict == "deny" and reasons
    assert parse_verdict("看起来没问题") == ("abstain", [])
    assert parse_verdict("") == ("abstain", [])


# --------------------------------------------------------------------------- #
# 2. 行为
# --------------------------------------------------------------------------- #
async def test_disabled_guardian_abstains_without_calling_the_model() -> None:
    adapter = FakeAdapter('{"verdict": "deny"}')
    guardian = Guardian.from_env(adapter, raw="off")
    verdict = await guardian.review("run_shell", {"command": "rm -rf /"})
    assert verdict.verdict == "abstain"
    assert adapter.calls == 0
    assert Guardian.from_env(adapter, raw="").enabled is False


async def test_guardian_denies_and_carries_reasons() -> None:
    guardian = Guardian.from_env(FakeAdapter('{"verdict": "deny", "reasons": ["会删除用户数据"]}'), raw="veto")
    verdict = await guardian.review("run_shell", {"command": "rm -rf ~"}, risk="high", reversible=False)
    assert verdict.denied
    assert verdict.reasons == ["会删除用户数据"]
    assert verdict.model == "fake-guard"
    assert verdict.prompt_tokens == 42
    payload = verdict.as_dict()
    assert payload["verdict"] == "deny" and payload["completion_tokens"] == 7


async def test_guardian_cannot_approve_irreversible_actions() -> None:
    guardian = Guardian.from_env(FakeAdapter('{"verdict": "allow"}'), raw="auto")
    verdict = await guardian.review("run_shell", {"command": "mkfs.ext4 /dev/sda1"}, reversible=False)
    # 模型说放行也无效：不可逆动作只能由人决定
    assert verdict.verdict == "abstain"
    assert any("不可逆" in reason for reason in verdict.reasons)


async def test_guardian_auto_mode_only_overrides_reversible_actions() -> None:
    guardian = Guardian.from_env(FakeAdapter('{"verdict": "allow"}'), raw="auto")
    verdict = await guardian.review("run_shell", {"command": "git status"}, reversible=True)
    assert guardian.can_auto_approve(verdict) is True
    veto_only = Guardian.from_env(FakeAdapter('{"verdict": "allow"}'), raw="veto")
    assert veto_only.can_auto_approve(verdict) is False


async def test_guardian_timeout_and_crash_fail_open() -> None:
    slow = Guardian(FakeAdapter('{"verdict": "deny"}', delay=0.3), mode="veto", timeout=0.05)
    timed_out = await slow.review("run_shell", {"command": "ls"})
    assert timed_out.verdict == "abstain" and timed_out.timed_out

    broken = Guardian(FakeAdapter(explode=True), mode="veto", timeout=1.0)
    crashed = await broken.review("run_shell", {"command": "ls"})
    assert crashed.verdict == "abstain" and crashed.error == "RuntimeError"


def test_modes_are_frozen_and_unknown_falls_back_to_veto() -> None:
    assert Guardian(None, mode="god").mode == "veto"
    assert GuardianVerdict().as_dict()["verdict"] == "abstain"


# --------------------------------------------------------------------------- #
# 3. 端到端：审查拦下危险命令，任务继续而不是卡住
# --------------------------------------------------------------------------- #
async def test_guardian_blocks_a_dangerous_command_without_asking_the_user(
    env, workspace, monkeypatch
):  # noqa: F811
    monkeypatch.setenv("BLANK_GUARDIAN", "veto")
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["清理构建产物"])
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 清理构建目录"},
            {
                "content": "删除构建目录",
                "tool_calls": [call("run_shell", {"command": "rm -rf build"})],
            },
            # guardian 的调用会消耗下一个 mock 步骤
            {"content": '{"verdict": "deny", "reasons": ["未经确认的递归删除"]}'},
            {"content": "改用只读方式核查", "tool_calls": [call("read_file", {"path": "README.md"})]},
            {"content": "已完成核查"},
            {"content": "已完成"},
        ]
    )
    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 审查在审批之前生效：不该把用户叫起来点一次
    assert body["task"]["status"] != "waiting_approval", body["task"]
    assert body["evidence"]["pending_approvals"] == []

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    reviewed = [event for event in events if event["type"] == "guardian.reviewed"]
    assert reviewed and reviewed[0]["payload"]["verdict"] == "deny"
    assert reviewed[0]["payload"]["tool"] == "run_shell"
    assert "approval.required" not in [event["type"] for event in events]

    calls = await env.tool_calls(task_id)
    shell = [item for item in calls if item.name == "run_shell"][0]
    assert shell.status == "denied"
    assert "独立审查未通过" in shell.result_summary
