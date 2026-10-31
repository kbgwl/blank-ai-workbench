"""端到端：同一个答案接口，两种问题两种待遇。

- 偏好题（带推荐项）：用户直接跳过 → 200，任务按推荐项继续，交付里写明"这是我替你定的"；
- 事实题（没有推荐项）：用户直接跳过 → 422，必须明说。那里"没填"的含义是"这处留空"，
  不能靠不填来推断——否则等于服务器替用户编了个电话。

这一组值钱的地方在于它把三条链路串起来了：工具（问题结构）→ 图（按推荐项继续 + 记账）
→ 路由（空答案放不放行）→ 交付正文（说了还是没说）。缺任何一环，用户看到的都是
"卡片关了，事情没了下文"。
"""

from __future__ import annotations

from tests.test_agent_v04 import (  # noqa: F401  (复用阶段3 的夹具)
    API,
    call,
    create_project,
    create_provider,
    create_task,
    env,
)

BEFORE = "<!-- hero -->"
PHONE = "138-1234-5678"


def _patch(text: str) -> str:
    return (
        "--- a/index.html\n"
        "+++ b/index.html\n"
        "@@ -1,1 +1,1 @@\n"
        f"-{BEFORE}\n"
        f"+{text}\n"
    )


def _ask(name: str, *, recommended: bool) -> dict:
    options: list[dict] = [{"label": "我另给一个"}]
    if recommended:
        options.insert(0, {"label": name, "recommended": True, "description": "先用这个"})
    return call(
        "request_user_input",
        {
            "questions": [
                {
                    "id": "name",
                    "header": "公司名",
                    "question": "公司叫什么名字？",
                    "options": options,
                }
            ]
        },
    )


async def test_skipping_a_preference_question_moves_on_with_the_default(
    env, workspace
):  # noqa: F811
    (workspace / "index.html").write_text(BEFORE + "\n", encoding="utf-8")

    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["做出首页"], goal="帮我做个公司官网")
    env.mock.reset(
        [
            {"content": "先问一句名字"},
            {"content": "先确认公司名", "tool_calls": [_ask("星河科技", recommended=True)]},
            {
                "content": "按默认写了",
                "tool_calls": [call("apply_patch", {"path": "index.html", "patch": _patch("星河科技")})],
            },
            {"content": "完成"},
        ]
    )

    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task"]["status"] == "waiting_approval", body["task"]
    questions = body["evidence"]["pending_questions"]
    assert questions, body["evidence"]
    # 界面要靠这个字段标出"不选就按它走"。
    assert questions[0]["options"][0]["recommended"] is True

    # 用户什么都不选，直接按"跳过"——发上去的是一份空答案。
    answered = await env.client.post(
        f"{API}/tasks/{task_id}/answer", json={"provider_id": provider_id, "answers": {}}
    )
    assert answered.status_code == 200, answered.text
    after = answered.json()
    if after["task"]["status"] == "waiting_approval" and after["evidence"]["pending_approvals"]:
        after = (
            await env.client.post(
                f"{API}/tasks/{task_id}/approval",
                json={"approve": True, "provider_id": provider_id, "scope": "once"},
            )
        ).json()

    # 事情继续了：文件真的写下去了，用的是推荐项。
    assert "星河科技" in (workspace / "index.html").read_text(encoding="utf-8")
    # 而且交付里明说了这是替用户定的——不能让他以为那是自己的选择。
    summary = str(after["task"].get("result_summary") or "")
    assert "替你定的" in summary, summary
    assert "星河科技" in summary


async def test_skipping_a_fact_question_is_still_refused(env, workspace):  # noqa: F811
    """事实题没有推荐项：跳过 = 422。这条与上一条同一个接口，差别只在有没有 recommended。"""
    (workspace / "index.html").write_text(BEFORE + "\n", encoding="utf-8")

    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["做出联系区块"], goal="帮我做个公司官网")
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 写联系区块"},
            {
                "content": "写联系区块",
                "tool_calls": [
                    call("apply_patch", {"path": "index.html", "patch": _patch(f"<p>电话：{PHONE}</p>")})
                ],
            },
            {"content": "完成"},
        ]
    )

    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task"]["status"] == "waiting_approval", body["task"]
    assert body["evidence"]["pending_questions"][0]["header"] == "电话出处"

    skipped = await env.client.post(
        f"{API}/tasks/{task_id}/answer", json={"provider_id": provider_id, "answers": {}}
    )
    assert skipped.status_code == 422, skipped.text
    assert "答案为空" in skipped.text
    # 跳过失败之后文件仍然原样：这次拦截没被绕过。
    assert (workspace / "index.html").read_text(encoding="utf-8").strip() == BEFORE
