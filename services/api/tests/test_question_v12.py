"""P1-3：Agent 主动提问（request_user_input）。

提问与审批共用同一条中断通道，因此这里既要验证纯解析，也要验证端到端的
"问 → 等 → 答 → 继续"，并且答案必须真的出现在工具记录与事件流里。
"""

import json

from tests.test_agent_v04 import (
    API,
    call,
    create_project,
    create_provider,
    create_task,
    env,  # noqa: F401  (复用阶段3 的夹具)
)

from blank_api.services.tools.ask import (
    normalize_questions,
    parse_answers,
    render_answers,
)


# --------------------------------------------------------------------------- #
# 1. 纯函数
# --------------------------------------------------------------------------- #
def test_normalize_single_question_with_options() -> None:
    questions = normalize_questions(
        {
            "question": "用哪种方案？",
            "header": "方案",
            "options": ["方案 A", {"label": "方案 B", "description": "更慢但更稳"}],
        }
    )
    assert len(questions) == 1
    assert questions[0]["id"] == "q1"
    assert questions[0]["header"] == "方案"
    assert [item["label"] for item in questions[0]["options"]] == ["方案 A", "方案 B"]


def test_normalize_batch_keeps_ids_and_drops_empty() -> None:
    questions = normalize_questions(
        {
            "questions": [
                {"id": "db", "question": "用哪个数据库？"},
                {"question": "   "},
                "要不要加缓存？",
                {"id": "style", "question": "代码风格？", "options": [{"label": "PEP8"}]},
            ]
        }
    )
    assert [item["id"] for item in questions] == ["db", "q3", "style"]
    assert questions[1]["question"] == "要不要加缓存？"


def test_normalize_caps_questions_and_options() -> None:
    questions = normalize_questions(
        {
            "questions": [
                {"id": f"q{index}", "question": f"问题 {index}", "options": [f"o{n}" for n in range(10)]}
                for index in range(8)
            ]
        }
    )
    assert len(questions) == 4
    assert all(len(item["options"]) <= 6 for item in questions)


def test_normalize_rejects_noise() -> None:
    assert normalize_questions({}) == []
    assert normalize_questions({"question": None, "options": "not-a-list"}) == []


def test_parse_answers_accepts_every_transport_shape() -> None:
    questions = normalize_questions({"questions": [{"id": "a", "question": "A?"}, {"id": "b", "question": "B?"}]})
    assert parse_answers("就用 A", questions) == {"a": "就用 A"}
    assert parse_answers(["甲", "乙"], questions) == {"a": "甲", "b": "乙"}
    assert parse_answers({"answers": {"a": "甲", "b": "  "}}, questions) == {"a": "甲"}
    assert parse_answers({"answer": "直接答"}, questions) == {"a": "直接答"}
    assert parse_answers({"a": "键控"}, questions) == {"a": "键控"}
    assert parse_answers(None, questions) == {}
    assert parse_answers({"answers": []}, questions) == {}


def test_render_answers_marks_unanswered_explicitly() -> None:
    questions = normalize_questions({"questions": [{"id": "a", "question": "A?", "header": "选择"}]})
    text = render_answers(questions, {"a": "选 A"})
    assert "选择：A?" in text and "答：选 A" in text
    assert "（用户未回答）" in render_answers(questions, {})


def test_question_tool_is_registered_and_low_risk() -> None:
    from blank_api.services.tools import build_default_tools

    tools = {tool.name: tool for tool in build_default_tools()}
    assert "request_user_input" in tools
    assert tools["request_user_input"].requires_approval is False


# --------------------------------------------------------------------------- #
# 2. 端到端
# --------------------------------------------------------------------------- #
async def _waiting_question_task(env, workspace):  # noqa: F811
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["按用户选择的方案执行"])
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 先确认方案\n2. 再改代码"},
            {
                "content": "方案不清楚，先问用户",
                "tool_calls": [
                    call(
                        "request_user_input",
                        {
                            "question": "用哪种方案？",
                            "header": "方案",
                            "options": [{"label": "方案 A"}, {"label": "方案 B"}],
                        },
                    )
                ],
            },
            {"content": "按 A 执行", "tool_calls": [call("read_file", {"path": "main.py"})]},
            {"content": "已按方案 A 完成"},
            {"content": "已完成"},
        ]
    )
    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    return task_id, provider_id, resp.json()


async def test_question_blocks_the_run_and_resumes_with_the_answer(env, workspace):  # noqa: F811
    task_id, provider_id, body = await _waiting_question_task(env, workspace)
    assert body["task"]["status"] == "waiting_approval"
    questions = body["evidence"]["pending_questions"]
    assert len(questions) == 1
    assert questions[0]["question"] == "用哪种方案？"
    assert [item["label"] for item in questions[0]["options"]] == ["方案 A", "方案 B"]
    # 提问不能被当成审批
    assert body["evidence"]["pending_approvals"] == []
    # 提问期间不该执行任何工具
    assert await env.tool_calls(task_id) == []

    resp = await env.client.post(
        f"{API}/tasks/{task_id}/answer",
        json={"provider_id": provider_id, "answers": {"q1": "方案 A"}},
    )
    assert resp.status_code == 200, resp.text
    finished = resp.json()
    assert finished["task"]["status"] != "waiting_approval"
    assert finished["evidence"]["pending_questions"] == []

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    types = [event["type"] for event in events]
    assert "question.required" in types and "approval.required" not in types, types
    answered = [event for event in events if event["type"] == "question.answered"]
    assert answered and answered[0]["payload"]["answers"] == {"q1": "方案 A"}
    # 时间线里也得留下「这一步做完了」：前端逐条渲染、审计逐条回放读的是这一份。
    # 只有工具台账而时间线跳过它，"答案回到了上下文"这句话就没有可对核对的实物
    # （方言端到端第 61 步实测到的正是这个空洞）。
    completed = [
        event for event in events
        if event["type"] == "tool.completed" and (event["payload"] or {}).get("tool") == "request_user_input"
    ]
    assert completed, f"答题那一步没进时间线：{types}"
    assert "方案 A" in completed[0]["payload"]["summary"]

    calls = await env.tool_calls(task_id)
    ask_calls = [item for item in calls if item.name == "request_user_input"]
    assert len(ask_calls) == 1
    assert ask_calls[0].status == "ok"
    assert "方案 A" in ask_calls[0].result_summary
    # 答案确实回到了模型上下文里
    forwarded = json.dumps(env.mock.requests[-1], ensure_ascii=False)
    assert "方案 A" in forwarded


async def test_answer_endpoint_rejects_when_there_is_no_question(env, workspace):  # noqa: F811
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["随便做点什么"])
    env.mock.reset([{"content": "执行计划：\n1. 读取 main.py"}])
    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text

    answered = await env.client.post(
        f"{API}/tasks/{task_id}/answer", json={"provider_id": provider_id, "answer": "随便"}
    )
    # 任务没在等待输入（已经跑完），必须先被状态挡住
    assert answered.status_code == 409, answered.text
