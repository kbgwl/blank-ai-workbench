"""R6：AskUserQuestion / ProposeGoal / ProposeSkills 的契约判据。

三件东西共用同一条中断通道，所以判据也按同一条线切：

1. **纯函数**钉住形状——多选、preview、注解、目标判定、提案硬校验。
   这里每一条错误信息都是模型拿到的下一步指令，写宽了等于没判。
2. **落库**钉住去向——提案进 ``skills.status=proposed``，采用时 new 转 draft、
   improvement 合并并让目标状态不变；会话目标进 ``threads.session_goal``。
3. **端到端**钉住闭环——多选答案带备注与补充回到模型；目标提案经问题卡确认后
   跨轮注入，并能被用户清除。只测纯函数会在"答案到了事件但没到上下文"这种
   空洞上全绿（v12 答题那一步就是这么漏过去的）。
"""

import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from tests.test_agent_v04 import (  # noqa: F401  (env 夹具)
    API,
    call,
    create_project,
    create_provider,
    create_task,
    env,
)

from blank_api.db import get_session_factory
from blank_api.models.skill import Skill
from blank_api.models.task import Task
from blank_api.services.tools.ask import (
    PREVIEW_CHARS,
    clean_preview,
    normalize_questions,
    parse_answer_meta,
    preview_problem,
    render_answers,
    validate_model_arguments,
)
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.goals import (
    ACCEPT_LABEL,
    CONDITION_CHARS,
    DECLINE_LABEL,
    goal_decision,
    goal_question,
    normalize_proposal,
    validate_proposal,
    wants_confirmation,
)
from blank_api.services.tools.skill_proposals import _propose_skills, normalize_proposals
from blank_api.services.tools.skills import SkillCatalog


# --------------------------------------------------------------------------- #
# 1. AskUserQuestion：归一化
# --------------------------------------------------------------------------- #
def test_normalize_keeps_multi_select_and_drops_preview_on_it() -> None:
    questions = normalize_questions(
        {
            "questions": [
                {
                    "id": "files",
                    "question": "要改哪些文件？",
                    "header": "文件范围",
                    "multiSelect": True,
                    "options": [
                        {"label": "核心", "preview": "<p>core</p>"},
                        {"label": "界面", "preview": "<p>ui</p>"},
                    ],
                },
                {
                    "id": "plan",
                    "question": "用哪个方案？",
                    "header": "方案",
                    "options": [
                        {"label": "A", "preview": "<table><tr><td>A</td></tr></table>"},
                        {"label": "B"},
                    ],
                },
            ]
        }
    )
    assert questions[0]["multiSelect"] is True
    # 多选时预览区该显示哪一个没有定义，所以整题都不留 preview。
    assert [item["preview"] for item in questions[0]["options"]] == ["", ""]
    assert questions[1]["multiSelect"] is False
    assert questions[1]["options"][0]["preview"].startswith("<table>")


def test_normalize_truncates_header_to_claude_limit() -> None:
    question = normalize_questions(
        {"question": "问", "header": "这是一个非常非常长的标题啊"}
    )[0]
    assert len(question["header"]) == 12
    assert question["header"] == "这是一个非常非常长的标题"


def test_normalize_single_recommendation_keeps_only_one() -> None:
    question = normalize_questions(
        {
            "question": "选哪个？",
            "options": [
                {"label": "A", "recommended": True},
                {"label": "B", "recommended": True},
            ],
        }
    )[0]
    # 单选留两个推荐项等于没留：模型和界面都无法判断该用哪个。
    assert question["default"] == "A"
    assert [item["recommended"] for item in question["options"]] == [True, False]


def test_normalize_multi_select_keeps_every_recommendation() -> None:
    question = normalize_questions(
        {
            "question": "都要什么？",
            "multiSelect": True,
            "options": [
                {"label": "A", "recommended": True},
                {"label": "B", "recommended": True},
            ],
        }
    )[0]
    assert question["default"] == "A, B"
    assert all(item["recommended"] for item in question["options"])


def test_normalize_explicit_default_splits_commas_for_multi_select() -> None:
    question = normalize_questions(
        {
            "question": "都要什么？",
            "multiSelect": True,
            "default": "A, C",
            "options": ["A", "B", "C"],
        }
    )[0]
    assert question["default"] == "A, C"
    assert [item["recommended"] for item in question["options"]] == [True, False, True]


def test_normalize_caps_options_at_four_labels() -> None:
    question = normalize_questions(
        {"question": "选一个", "options": [f"o{index}" for index in range(6)]}
    )[0]
    assert [item["label"] for item in question["options"]] == ["o0", "o1", "o2", "o3"]


def test_normalize_dedupes_question_ids() -> None:
    questions = normalize_questions(
        {"questions": [{"id": "x", "question": "一"}, {"id": "x", "question": "二"}]}
    )
    assert [item["id"] for item in questions] == ["x", "x-2"]


# --------------------------------------------------------------------------- #
# 2. AskUserQuestion：模型入参硬校验
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("arguments", "needle"),
    [
        ({}, "缺少 question 正文"),
        ({"questions": [{"question": f"q{index}"} for index in range(5)]}, "一次最多问 4 个问题"),
        ({"question": "x" * 501}, "超过 500 字符"),
        ({"question": "问", "header": "标题超过十二个字符了吧啊呀"}, "超过 12 字符"),
        ({"question": "问", "options": "A,B"}, "options 必须是数组"),
        ({"question": "问", "options": ["A", "B", "C", "D", "E"]}, "超过上限 4"),
        ({"question": "问", "options": [{"description": "没有 label"}]}, "缺少 label"),
        (
            {"question": "问", "options": [{"label": "A", "preview": "<script>x</script>"}]},
            "不能使用 script",
        ),
        ({"question": "问", "options": [{"label": "A", "preview": "纯文本"}]}, "至少要包含一个 HTML 标签"),
        (
            {
                "question": "问",
                "multiSelect": True,
                "options": [{"label": "A", "preview": "<b>x</b>"}],
            },
            "多选不支持 preview",
        ),
        ({"question": "问", "default": "B", "options": ["A"]}, "default 指向不存在的选项"),
    ],
)
def test_validate_model_arguments_rejects_with_an_actionable_reason(
    arguments: dict, needle: str
) -> None:
    problem = validate_model_arguments(arguments)
    assert needle in problem, problem


def test_validate_model_arguments_accepts_fact_and_single_option_shapes() -> None:
    # 下界不卡：事实题没有选项、确认题只有一个选项都是真实形状。
    assert validate_model_arguments({"question": "你的电话是多少？"}) == ""
    assert validate_model_arguments({"question": "确认吗？", "options": ["是"]}) == ""


def test_preview_problem_stops_at_a_fragment() -> None:
    assert preview_problem("<p>ok</p>") == ""
    assert "doctype" in preview_problem("<!DOCTYPE html><p>x</p>")
    assert "HTML 注释" in preview_problem("<p>x</p><!-- hi -->")
    assert "代码围栏" in preview_problem("```html\n<p>x</p>\n```")
    assert "超过" in preview_problem("<p>" + "x" * PREVIEW_CHARS + "</p>")
    # 多选一律不留预览，哪怕内容本身合法。
    assert clean_preview("<p>x</p>", multi_select=True) == ""


# --------------------------------------------------------------------------- #
# 3. AskUserQuestion：答案回执
# --------------------------------------------------------------------------- #
def test_parse_answer_meta_joins_multi_select_and_maps_annotations() -> None:
    questions = normalize_questions(
        {
            "questions": [
                {"id": "files", "question": "要改哪些文件？", "header": "文件"},
                {"id": "plan", "question": "用哪个方案？", "header": "方案"},
            ]
        }
    )
    answers, response, annotations = parse_answer_meta(
        {
            "answers": {"要改哪些文件？": ["core.py", "ui.py"], "方案": "B"},
            "response": " 两个都要 ",
            "annotations": {
                "要改哪些文件？": {"preview": "<b>core</b>", "notes": "先改核心"},
                "方案": {"notes": "选 B"},
                "不存在的题": {"notes": "必须被忽略"},
            },
        },
        questions,
    )
    # Claude 的回执按问题文本做键，内部账按 id；header 也认。
    assert answers == {"files": "core.py, ui.py", "plan": "B"}
    assert response == "两个都要"
    assert annotations == {
        "files": {"preview": "<b>core</b>", "notes": "先改核心"},
        "plan": {"notes": "选 B"},
    }


def test_render_answers_carries_notes_and_free_text_but_not_preview() -> None:
    questions = normalize_questions(
        {"questions": [{"id": "files", "question": "改哪些文件？", "header": "文件"}]}
    )
    text = render_answers(
        questions,
        {"files": "core.py"},
        [{"id": "files"}],
        response="两个都要",
        annotations={"files": {"preview": "<b>core</b>", "notes": "先改核心"}},
    )
    assert "答：core.py（用户没答，按推荐默认值继续）" in text
    assert "备注：先改核心" in text
    assert "补充：两个都要" in text
    # 预览是给用户看的展示片段，贴回模型是白付 token。
    assert "<b>core</b>" not in text


# --------------------------------------------------------------------------- #
# 4. ProposeGoal：纯函数
# --------------------------------------------------------------------------- #
def test_goal_proposal_helpers() -> None:
    assert normalize_proposal({"condition": " 发布包可安装 "}) == "发布包可安装"
    assert normalize_proposal({"objective": "能连上后端"}) == "能连上后端"
    assert validate_proposal({}) != ""
    assert "超过" in validate_proposal({"condition": "x" * (CONDITION_CHARS + 1)})
    assert validate_proposal({"condition": "x" * CONDITION_CHARS}) == ""


def test_wants_confirmation_defaults_to_true_and_only_explicit_false_skips() -> None:
    # 默认必须问：模型不能靠省略字段把目标偷偷写进会话。
    assert wants_confirmation({}) is True
    assert wants_confirmation({"ask_user": True}) is True
    assert wants_confirmation({"ask_user": "on"}) is True
    assert wants_confirmation({"ask_user": False}) is False
    assert wants_confirmation({"askUser": "false"}) is False


def test_goal_question_is_an_ordinary_question_card() -> None:
    card = goal_question("发布包能装能连", "goal1")
    assert card["id"] == "goal1"
    assert card["header"] == "会话目标"
    assert [item["label"] for item in card["options"]] == [ACCEPT_LABEL, DECLINE_LABEL]
    # 目标不是偏好：没有默认值，不答即视为不设置。
    assert card["default"] == ""
    assert card["multiSelect"] is False


def test_goal_decision_branches() -> None:
    assert goal_decision(ACCEPT_LABEL, "目标") == ("accepted", "目标", "用户确认")
    assert goal_decision(DECLINE_LABEL, "目标")[0] == "declined"
    assert goal_decision("no", "目标")[0] == "declined"
    assert goal_decision("", "目标") == ("declined", "", "用户没有回答")
    # 用户重写了一遍目标，本来就是在回答"是什么"。
    assert goal_decision("改成能离线启动", "目标") == (
        "accepted",
        "改成能离线启动",
        "用户改写后接受",
    )


# --------------------------------------------------------------------------- #
# 5. ProposeSkills：硬校验
# --------------------------------------------------------------------------- #
def _proposal(**overrides) -> dict:
    proposal = {
        "name": "release-check",
        "kind": "new",
        "description": "发布前把安装与启动路径都验一遍",
        "skillMd": "---\nname: release-check\ndescription: 发布检查\n---\n先打包，再安装，再启动。",
        "evidence": ["README 第 3 节"],
    }
    proposal.update(overrides)
    return proposal


def test_normalize_proposals_keeps_body_and_frontmatter_keys_for_audit() -> None:
    proposals, problem = normalize_proposals({"proposals": [_proposal()]})
    assert problem == ""
    assert proposals[0]["body"] == "先打包，再安装，再启动。"
    assert proposals[0]["frontmatter_keys"] == ["description", "name"]
    assert proposals[0]["evidence"] == ["README 第 3 节"]


@pytest.mark.parametrize(
    ("proposals", "needle"),
    [
        ([], "需要 proposals"),
        (
            [_proposal(), _proposal(name="b"), _proposal(name="c"), _proposal(name="d")],
            "一次最多提交 3 条",
        ),
        (["不是对象"], "不是对象"),
        ([_proposal(name="Release Check")], "不是 kebab-case"),
        ([_proposal(), _proposal(name="release-check")], "name 重复"),
        ([_proposal(kind="improvement")], "必须给 target"),
        ([_proposal(kind="improvement", target="release-check")], "target 不能等于 name"),
        ([_proposal(target="release-check")], "不该带 target"),
        ([_proposal(description="")], "缺少 description"),
        ([_proposal(description="<b>带尖括号</b>")], "不能包含尖括号"),
        ([_proposal(skillMd="没有 frontmatter")], "必须以 YAML frontmatter"),
        ([_proposal(skillMd="---\nname: x\n---\n")], "正文为空"),
    ],
)
def test_normalize_proposals_rejects_unusable_entries(
    proposals: list, needle: str
) -> None:
    kept, problem = normalize_proposals({"proposals": proposals})
    assert kept == []
    assert needle in problem, problem


# --------------------------------------------------------------------------- #
# 6. ProposeSkills：落库与采用
# --------------------------------------------------------------------------- #
class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    async def add(self, event: str, node: str, payload: dict) -> None:
        self.events.append((event, node, payload))


def _context(catalog, recorder=None) -> ToolContext:
    return ToolContext(
        root=Path("."),
        task_id="task-v43",
        session_factory=get_session_factory(),
        skills=catalog,
        recorder=recorder,
    )


async def _proposed_rows() -> list[Skill]:
    async with get_session_factory()() as session:
        return list(
            (
                await session.execute(select(Skill).where(Skill.status == "proposed"))
            ).scalars()
        )


async def test_propose_skills_handler_stores_proposed_rows(client) -> None:  # noqa: F811
    catalog = SkillCatalog([{"name": "release-check", "instructions": "旧流程"}])
    catalog.loaded.add("release-check")
    recorder = _Recorder()
    outcome = await _propose_skills(
        _context(catalog, recorder),
        {
            "proposals": [
                _proposal(),
                _proposal(
                    name="release-check-v2",
                    kind="improvement",
                    target="release-check",
                ),
            ]
        },
    )
    assert outcome.ok is True, outcome.message
    assert outcome.data["proposalCount"] == 2

    rows = await _proposed_rows()
    by_name = {row.name: row for row in rows}
    assert set(by_name) == {"release-check", "release-check-v2"}
    assert by_name["release-check"].metadata_json["kind"] == "new"
    assert by_name["release-check"].metadata_json["description"]
    assert by_name["release-check-v2"].metadata_json["target"] == "release-check"
    assert by_name["release-check-v2"].metadata_json["evidence"] == ["README 第 3 节"]
    # 提案正文只留 frontmatter 之后的那一段。
    assert by_name["release-check"].instructions == "先打包，再安装，再启动。"
    assert [event[0] for event in recorder.events] == ["skill.proposed"]


async def test_propose_skills_requires_the_target_to_be_loaded(client) -> None:  # noqa: F811
    catalog = SkillCatalog([{"name": "release-check", "instructions": "旧流程"}])
    outcome = await _propose_skills(
        _context(catalog),
        {"proposals": [_proposal(name="release-check-v2", kind="improvement", target="release-check")]},
    )
    assert outcome.ok is False
    assert "加载" in outcome.message
    assert await _proposed_rows() == []


async def test_propose_skills_rejects_a_target_outside_the_catalog(client) -> None:  # noqa: F811
    catalog = SkillCatalog([{"name": "release-check", "instructions": "旧流程"}])
    outcome = await _propose_skills(
        _context(catalog),
        {"proposals": [_proposal(kind="improvement", target="ghost-skill")]},
    )
    assert outcome.ok is False
    assert "不在本任务可用的 Skill 清单里" in outcome.message
    assert await _proposed_rows() == []


async def _add_skill(
    name: str,
    *,
    status: str,
    instructions: str = "旧正文",
    version: int = 1,
    history: list | None = None,
    metadata: dict | None = None,
) -> str:
    skill_id = str(uuid.uuid4())
    async with get_session_factory()() as session:
        session.add(
            Skill(
                id=skill_id,
                name=name,
                instructions=instructions,
                status=status,
                version=version,
                history=history or [],
                metadata_json=metadata or {},
            )
        )
        await session.commit()
    return skill_id


async def test_accept_new_proposal_turns_it_into_a_draft(client) -> None:  # noqa: F811
    proposal_id = await _add_skill(
        "release-check",
        status="proposed",
        instructions="新正文",
        metadata={"kind": "new", "description": "发布检查"},
    )

    resp = await client.post(f"{API}/skills/{proposal_id}/accept")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == proposal_id
    # 采用不等于启用：catalog 只吃 active，这一步不能替用户做主。
    assert body["status"] == "draft"
    assert body["metadata"]["kind"] == "new"
    assert body["metadata"]["accepted_at"]


async def test_accept_improvement_merges_into_the_target_and_deletes_the_proposal(
    client,
) -> None:  # noqa: F811
    target_id = await _add_skill(
        "release-check", status="active", instructions="旧正文", version=2
    )
    proposal_id = await _add_skill(
        "release-check-v2",
        status="proposed",
        instructions="新正文",
        metadata={
            "kind": "improvement",
            "target": "release-check",
            "description": "更细的检查",
            "evidence": ["run.log"],
        },
    )

    resp = await client.post(f"{API}/skills/{proposal_id}/accept")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == target_id
    # 目标 Skill 的启用状态不变，正文换代、版本 +1、旧正文进 history。
    assert body["status"] == "active"
    assert body["version"] == 3
    assert body["instructions"] == "新正文"
    assert body["history"] == [{"version": 2, "instructions": "旧正文"}]
    assert body["metadata"]["adopted_from"] == proposal_id
    assert body["metadata"]["description"] == "更细的检查"
    assert body["metadata"]["evidence"] == ["run.log"]

    async with get_session_factory()() as session:
        assert await session.get(Skill, proposal_id) is None


async def test_accept_rejects_a_row_that_is_not_a_proposal(client) -> None:  # noqa: F811
    skill_id = await _add_skill("release-check", status="active")
    resp = await client.post(f"{API}/skills/{skill_id}/accept")
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "skill_not_proposed"


async def test_accept_improvement_without_target_is_rejected(client) -> None:  # noqa: F811
    proposal_id = await _add_skill(
        "release-check-v2",
        status="proposed",
        metadata={"kind": "improvement"},
    )
    resp = await client.post(f"{API}/skills/{proposal_id}/accept")
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "skill_proposal_invalid"


async def test_accept_improvement_with_missing_target_skill_is_rejected(client) -> None:  # noqa: F811
    proposal_id = await _add_skill(
        "release-check-v2",
        status="proposed",
        metadata={"kind": "improvement", "target": "release-check"},
    )
    resp = await client.post(f"{API}/skills/{proposal_id}/accept")
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "skill_target_not_found"


# --------------------------------------------------------------------------- #
# 7. 端到端：多选答案 + 备注 + 补充回到模型
# --------------------------------------------------------------------------- #
async def test_multi_select_answer_returns_notes_and_free_text_to_the_model(
    env, workspace
) -> None:  # noqa: F811
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["按用户选择的文件范围修改"])
    question_text = "要改哪些文件？"
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 确认范围\n2. 修改"},
            {
                "content": "先问范围",
                "tool_calls": [
                    call(
                        "request_user_input",
                        {
                            "question": question_text,
                            "header": "文件范围",
                            "multiSelect": True,
                            "options": [{"label": "核心"}, {"label": "界面"}],
                        },
                    )
                ],
            },
            {"content": "按选择执行", "tool_calls": [call("read_file", {"path": "main.py"})]},
            {"content": "已按选择完成"},
            {"content": "已完成"},
        ]
    )
    resp = await env.client.post(
        f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task"]["status"] == "waiting_approval"
    pending = body["evidence"]["pending_questions"]
    assert pending[0]["multiSelect"] is True
    assert pending[0]["id"] == "q1"

    answered = await env.client.post(
        f"{API}/tasks/{task_id}/answer",
        json={
            "provider_id": provider_id,
            "answers": {"q1": ["核心", "界面"]},
            "response": "两个都要",
            "annotations": {question_text: {"preview": "<b>预览</b>", "notes": "先改核心"}},
        },
    )
    assert answered.status_code == 200, answered.text
    finished = answered.json()
    assert finished["task"]["status"] != "waiting_approval"
    assert finished["evidence"]["pending_questions"] == []

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    answered_events = [event for event in events if event["type"] == "question.answered"]
    assert answered_events, [event["type"] for event in events]
    # 多选落账成 ", " 连接的一段：事件与提示词读到的是同一个形状。
    assert answered_events[0]["payload"]["answers"] == {"q1": "核心, 界面"}
    decided = [event for event in events if event["type"] == "question.decided"]
    assert decided and decided[0]["payload"]["response"] == "两个都要"
    assert decided[0]["payload"]["annotations"][question_text]["notes"] == "先改核心"

    calls = await env.tool_calls(task_id)
    ask_calls = [row for row in calls if row.name == "request_user_input"]
    assert len(ask_calls) == 1 and ask_calls[0].status == "ok"

    forwarded = json.dumps(env.mock.requests[-1], ensure_ascii=False)
    assert "答：核心, 界面" in forwarded
    assert "备注：先改核心" in forwarded
    assert "补充：两个都要" in forwarded
    # 预览只给用户看，不能原样贴回模型。
    assert "<b>预览</b>" not in forwarded


# --------------------------------------------------------------------------- #
# 8. 端到端：目标提案 → 确认 → 跨轮注入 → 清除
# --------------------------------------------------------------------------- #
async def _thread(client, project_id: str) -> str:
    resp = await client.post(
        f"{API}/projects/{project_id}/threads", json={"title": "目标会话"}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _task_in_thread(project_id: str, thread_id: str, *, turn: int, goal: str) -> str:
    task_id = str(uuid.uuid4())
    async with get_session_factory()() as session:
        session.add(
            Task(
                id=task_id,
                project_id=project_id,
                thread_id=thread_id,
                turn_index=turn,
                title=goal[:40],
                goal=goal,
            )
        )
        await session.commit()
    return task_id


async def test_propose_goal_is_confirmed_on_a_question_card_and_persists_to_the_thread(
    env, workspace
) -> None:  # noqa: F811
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    thread_id = await _thread(env.client, project_id)
    condition = "发布包能安装、启动后能连上后端"
    task_id = await _task_in_thread(project_id, thread_id, turn=1, goal="把项目整理到可发布")
    env.mock.reset(
        [
            {"content": "先确认会话目标"},
            {
                "content": "提案",
                "tool_calls": [call("propose_goal", {"condition": condition})],
            },
            {"content": "目标已确认，开始执行"},
            {"content": "完成"},
        ]
    )

    resp = await env.client.post(
        f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task"]["status"] == "waiting_approval"
    pending = body["evidence"]["pending_questions"]
    assert len(pending) == 1
    # 目标提案走的是同一条问题中断通道，不是审批卡。
    assert pending[0]["id"] == "goal1"
    assert pending[0]["header"] == "会话目标"
    assert [item["label"] for item in pending[0]["options"]] == [ACCEPT_LABEL, DECLINE_LABEL]
    assert body["evidence"]["pending_approvals"] == []
    assert condition in pending[0]["question"]

    answered = await env.client.post(
        f"{API}/tasks/{task_id}/answer",
        json={"provider_id": provider_id, "answers": {"goal1": ACCEPT_LABEL}},
    )
    assert answered.status_code == 200, answered.text
    assert answered.json()["task"]["status"] != "waiting_approval"

    stored = await env.client.get(f"{API}/threads/{thread_id}/session-goal")
    assert stored.status_code == 200, stored.text
    assert stored.json()["session_goal"] == condition

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    goal_events = [event for event in events if event["type"] == "goal.set"]
    assert goal_events, [event["type"] for event in events]
    assert goal_events[0]["payload"]["source"] == "user"
    assert goal_events[0]["payload"]["persisted"] is True

    # 第二轮：目标从库里重新读出并注入系统提示，而不是信内存里的副本。
    second = await _task_in_thread(project_id, thread_id, turn=2, goal="继续补启动检查")
    env.mock.reset([{"content": "继续"}, {"content": "完成"}])
    second_run = await env.client.post(
        f"{API}/tasks/{second}/run", json={"provider_id": provider_id}
    )
    assert second_run.status_code == 200, second_run.text
    forwarded = json.dumps(env.mock.requests[-1], ensure_ascii=False)
    assert "本次会话中确认的目标" in forwarded
    assert condition in forwarded

    cleared = await env.client.delete(f"{API}/threads/{thread_id}/session-goal")
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["cleared"] == condition
    after = await env.client.get(f"{API}/threads/{thread_id}/session-goal")
    assert after.json()["session_goal"] == ""


async def test_declining_the_goal_leaves_the_thread_without_a_session_goal(
    env, workspace
) -> None:  # noqa: F811
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    thread_id = await _thread(env.client, project_id)
    task_id = await _task_in_thread(project_id, thread_id, turn=1, goal="把发布流程整理一遍")
    env.mock.reset(
        [
            {"content": "先确认"},
            {
                "content": "提案",
                "tool_calls": [call("propose_goal", {"condition": "每天都跑一遍回归"})],
            },
            {"content": "用户不设，按本轮继续"},
            {"content": "完成"},
        ]
    )
    resp = await env.client.post(
        f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id}
    )
    assert resp.json()["task"]["status"] == "waiting_approval"

    answered = await env.client.post(
        f"{API}/tasks/{task_id}/answer",
        json={"provider_id": provider_id, "answers": {"goal1": DECLINE_LABEL}},
    )
    assert answered.status_code == 200, answered.text
    assert (await env.client.get(f"{API}/threads/{thread_id}/session-goal")).json()[
        "session_goal"
    ] == ""
    calls = await env.tool_calls(task_id)
    goal_calls = [row for row in calls if row.name == "propose_goal"]
    assert len(goal_calls) == 1 and goal_calls[0].status == "declined"


async def test_invalid_goal_proposal_becomes_a_receipt_instead_of_a_card(
    env, workspace
) -> None:  # noqa: F811
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["按用户选择的方案执行"])
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 提目标"},
            {"content": "提案", "tool_calls": [call("propose_goal", {"condition": "   "})]},
            {"content": "收到，按本轮任务继续"},
            {"content": "完成"},
        ]
    )
    resp = await env.client.post(
        f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id}
    )
    assert resp.status_code == 200, resp.text
    finished = resp.json()
    # 不合格的入参不能变成一张让用户按掉才拿到理由的卡。
    assert finished["task"]["status"] != "waiting_approval"
    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    assert "question.required" not in [event["type"] for event in events]
    calls = await env.tool_calls(task_id)
    goal_calls = [row for row in calls if row.name == "propose_goal"]
    assert len(goal_calls) == 1 and goal_calls[0].status == "denied"
    forwarded = json.dumps(env.mock.requests[-1], ensure_ascii=False)
    assert "目标提案不合格" in forwarded
