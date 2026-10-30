"""端到端：「帮我做个公司官网」这一句话，现在会先停下来问电话。

用户报的是这一幕：一句「帮我创建一个公司官网」，它没问公司叫什么、联系方式是什么，
直接写出页面，电话邮箱都是编的，只在结论里补一句"请替换"。文件已经落盘，用户看到的是
"做完了"，实际上每个关键字段都得自己重填。

这条链路要证明的是**写盘之前**就停了：任务进 ``waiting_approval``、证据里带问题、
工作区里那个文件还是原样；用户回答之后才写进去，而且写的就是用户认过的那个值。

写文件用的是 ``apply_patch``——那才是这个产品里真正落盘的工具（``write_file`` 只是
历史名字，注册表里没有它）。第一版用例写成 ``write_file``，结果"文件不存在"是因为
工具不存在，而不是因为闸门拦住了：那种通过比失败更危险。
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

PHONE = "138-1234-5678"
BEFORE = "<!-- contact -->"
AFTER = f"<p>电话：{PHONE}</p>"


def _patch() -> str:
    return (
        "--- a/index.html\n"
        "+++ b/index.html\n"
        "@@ -1,1 +1,1 @@\n"
        f"-{BEFORE}\n"
        f"+{AFTER}\n"
    )


async def test_the_homepage_waits_for_the_phone_number(env, workspace):  # noqa: F811
    (workspace / "index.html").write_text(BEFORE + "\n", encoding="utf-8")

    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["做出联系区块"], goal="帮我做个公司官网")
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 写联系区块"},
            {
                "content": "先写联系方式",
                "tool_calls": [
                    call("apply_patch", {"path": "index.html", "patch": _patch()}),
                ],
            },
            {"content": "好的，先不写电话"},
        ]
    )

    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # 停在"要你回答"，不是"做完了"。
    assert body["task"]["status"] == "waiting_approval", body["task"]
    questions = body["evidence"]["pending_questions"]
    assert questions, body["evidence"]
    assert questions[0]["header"] == "电话出处"
    assert PHONE in questions[0]["question"]
    # 关键的一条：文件还是原样。问完再写，比写完再补一句"请替换"值钱。
    assert (workspace / "index.html").read_text(encoding="utf-8").strip() == BEFORE

    answered = await env.client.post(
        f"{API}/tasks/{task_id}/answer",
        json={"provider_id": provider_id, "answers": {"fact_1": "就用这个"}},
    )
    assert answered.status_code == 200, answered.text
    after = answered.json()
    # 用户认了之后才轮到写操作本身：落盘工具自己还要过一个审批门，这是另一回事。
    if after["task"]["status"] == "waiting_approval" and after["evidence"]["pending_approvals"]:
        approved = await env.client.post(
            f"{API}/tasks/{task_id}/approval",
            json={"approve": True, "provider_id": provider_id, "scope": "once"},
        )
        assert approved.status_code == 200, approved.text
        after = approved.json()

    assert after["task"]["status"] != "waiting_approval", after["task"]
    written = (workspace / "index.html").read_text(encoding="utf-8")
    assert AFTER in written

async def test_leaving_it_blank_never_writes_the_invented_number(env, workspace):  # noqa: F811
    """用户说"先留空"：编出来的号码一个字节都不许进文件。

    这条才是"确定性"本身——上面那条用例即使闸门关掉也能过（问题照样会被问出来），
    只有这条会在闸门失效时变红：文件里出现了用户从来没给过的电话。
    """
    (workspace / "index.html").write_text(BEFORE + "\n", encoding="utf-8")

    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["做出联系区块"], goal="帮我做个公司官网")
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 写联系区块"},
            {
                "content": "先写联系方式",
                "tool_calls": [
                    call("apply_patch", {"path": "index.html", "patch": _patch()}),
                ],
            },
            {"content": "好的，这处留空"},
            {"content": "完成"},
        ]
    )

    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    assert resp.json()["evidence"]["pending_questions"], resp.json()["evidence"]

    answered = await env.client.post(
        f"{API}/tasks/{task_id}/answer",
        json={"provider_id": provider_id, "answers": {"fact_1": "先留空"}},
    )
    assert answered.status_code == 200, answered.text
    body = answered.json()
    if body["task"]["status"] == "waiting_approval" and body["evidence"]["pending_approvals"]:
        body = (
            await env.client.post(
                f"{API}/tasks/{task_id}/approval",
                json={"approve": True, "provider_id": provider_id, "scope": "once"},
            )
        ).json()

    assert PHONE not in (workspace / "index.html").read_text(encoding="utf-8")
