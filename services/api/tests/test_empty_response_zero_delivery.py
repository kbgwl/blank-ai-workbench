"""空响应与零交付：不许把「模型什么都没说」当成一次成功交付。

真机取证（2026-11-16，评测实例 :8066，任务 9eac5bed）：
  seq23  model.stream_fallback  code=model_stream_empty
  seq24  coder completion_tokens=8192 / reasoning_tokens=8192，正文与 tool_calls 皆空
  seq35  verification.verdict incomplete / passed=true / blocking=false
         delivery_says_done=true（交付写「完成：工作区仍为空…功能尚未实现」）
  seq36  task.completed status=succeeded，交付物 0 个文件

三处独立性：空响应在 harness 层就该拦住，不该走到验证；即便走到了，Verifier
也不能对「零改动且自述没实现」放行。每条判据都钉在「拿掉实现就红」的位置。
"""

from __future__ import annotations

from pathlib import Path

from blank_api.services.agent.runtime import resolve_status
from blank_api.services.agent.verifier import verify

from tests.test_agent_v04 import (  # noqa: F401  (env 是夹具，故意导入)
    API,
    create_project,
    create_provider,
    create_task,
    env,
)

REAL_ZERO_DELIVERY = (
    "完成：工作区仍为空，三个文件均未创建，功能尚未实现\n"
    "需要你：先创建 index.html、styles.css、app.js，再实际打开页面逐项验证"
)
REAL_SKIPPED = {
    "command": "",
    "passed": None,
    "status": "skipped",
    "exit_code": None,
    "skipped_reason": "本次没有改动任何文件，无需执行测试",
}
EDIT_PLAN = {"steps": [{"kind": "edit", "title": "创建三个文件"}], "revision": 0}
DONE_WHEN = {"done_when": ["三个文件存在且双击 index.html 可运行"]}


# --------------------------------------------------------------------------- #
# 1. harness：空响应 = 重试一次，仍空 = 任务失败
# --------------------------------------------------------------------------- #


async def test_empty_model_response_fails_the_task_with_a_code(env, workspace: Path):  # noqa: F811
    """模型连续返回空正文且无工具调用：任务必须 failed，code=model_empty_response。

    现网缺陷是它一路走到 summarize，交付「工作区仍为空」还判 succeeded。
    """
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["交付 index.html"])
    env.mock.reset([{"content": ""}, {"content": ""}, {"content": ""}, {"content": ""}])

    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 502, resp.text
    assert resp.json().get("error", {}).get("code") == "model_empty_response", resp.text

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    types = [event["type"] for event in events]
    # 空响应必须留下可读事实，且只重试一次：两次调用、两条事件。
    assert types.count("model.empty_response") == 2, types
    assert "model.empty_response.retry" in types, types
    assert "task.completed" not in types, types
    failed = [event for event in events if event["type"] == "task.failed"]
    assert failed, types
    assert failed[-1]["payload"].get("code") == "model_empty_response", failed[-1]

    task = (await env.client.get(f"{API}/tasks/{task_id}")).json()
    assert task["status"] == "failed", task


async def test_empty_response_retry_recovers_and_the_task_finishes(env, workspace: Path):  # noqa: F811
    """第一次空、第二次有正文：只重试一次就继续，任务照常完成。

    这一条防「一有空就判死」：空响应多数是瞬时现象，重试需要一次机会。
    """
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["读 README"])
    env.mock.reset(
        [
            {"content": ""},
            {"content": "执行计划：\n1. 读 README"},
            {
                "content": "读一下",
                "tool_calls": [
                    {
                        "id": "call_readme",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path": "README.md"}'},
                    }
                ],
            },
            {"content": "README 是 demo project。"},
        ]
    )

    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    assert resp.json()["task"]["status"] == "succeeded", resp.json()["task"]

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    types = [event["type"] for event in events]
    assert types.count("model.empty_response") == 1, types
    assert types.count("task.failed") == 0, types

    # 重试那次请求要带一条明确的系统提示，且原始对话没有被替换掉。
    # 只认「上一条回复是空的」这个专指短语：单字「空」在系统提示里本来就常见，
    # 拿它当证据会让「重试什么提示都不加」也一样绿。
    retry_messages = env.mock.requests[1]["messages"]
    assert retry_messages[-1].get("role") == "system", retry_messages[-1]
    assert "上一条回复是空的" in str(retry_messages[-1].get("content") or ""), retry_messages[-1]
    assert any("读 README" in str(item.get("content") or "") for item in retry_messages), retry_messages


# --------------------------------------------------------------------------- #
# 2. Verifier：零交付不能判 passed
# --------------------------------------------------------------------------- #


def test_an_admitted_zero_delivery_is_blocking():
    """「完成：…尚未实现」是四行格式里的自述未完成，必须 blocking。"""
    verdict = verify(
        goal_spec=DONE_WHEN,
        plan=EDIT_PLAN,
        tool_results=[],
        validation=REAL_SKIPPED,
        changed_paths=[],
        report=REAL_ZERO_DELIVERY,
    )
    assert verdict["checks"]["delivery_says_done"] is False, verdict
    assert verdict["blocking"] is True, verdict
    assert verdict["verdict"] == "failed", verdict
    assert resolve_status({"validation": REAL_SKIPPED, "verdict": verdict}) == "failed"


def test_zero_changes_against_an_edit_plan_is_blocking_even_with_neutral_text():
    """交付文本不认错，也要拿事实说话：有验收标准 + 计划要求改 + 磁盘零改动 = 失败。"""
    verdict = verify(
        goal_spec=DONE_WHEN,
        plan=EDIT_PLAN,
        tool_results=[],
        validation=REAL_SKIPPED,
        changed_paths=[],
        report="完成：已按要求检查工作区\n需要你：打开页面自行确认",
    )
    assert verdict["checks"]["delivery_matches_plan"] is False, verdict
    assert verdict["blocking"] is True, verdict
    assert verdict["verdict"] == "failed", verdict
    assert any("零改动" in item for item in verdict["findings"]), verdict["findings"]


def test_a_chat_answer_without_a_plan_is_not_a_code_failure():
    """保守原则不破：没有验收标准、没有编辑计划时，零改动仍是正常回答。"""
    verdict = verify(
        goal_spec={},
        plan=None,
        tool_results=[],
        validation=REAL_SKIPPED,
        changed_paths=[],
        report="完成：这是纯问答，没有文件需要改\n需要你：无",
    )
    assert verdict["blocking"] is False, verdict
    assert verdict["verdict"] != "failed", verdict


def test_an_edit_plan_without_acceptance_criteria_stays_incomplete_not_failed():
    """宽度守卫：计划里有 edit 步还不够，目标必须同时给了验收标准才改判失败。

    调研后判断「本来就不需要改」是合法结论；只凭计划措辞把它判死，会把
    一次老实的问答变成失败记录。
    """
    verdict = verify(
        goal_spec={},
        plan=EDIT_PLAN,
        tool_results=[],
        validation=REAL_SKIPPED,
        changed_paths=[],
        report="完成：检查后判断不需要改动\n需要你：无",
    )
    assert verdict["checks"]["delivery_matches_plan"] is True, verdict
    assert verdict["blocking"] is False, verdict
    assert verdict["verdict"] != "failed", verdict


def test_a_readonly_investigation_stays_not_applicable():
    verdict = verify(
        goal_spec=DONE_WHEN,
        plan=EDIT_PLAN,
        tool_results=[],
        validation=REAL_SKIPPED,
        changed_paths=[],
        report="完成：已阅读相关文件\n需要你：无",
        readonly=True,
    )
    assert verdict["verdict"] == "not_applicable", verdict
    assert verdict["blocking"] is False, verdict
