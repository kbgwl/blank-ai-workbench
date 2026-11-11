"""Q 批第 3 项：Skill 工具对齐 Claude。

判据分四层：
- 工具面：`skill` 在默认工具表里、低风险、lean 档与计划模式都留着它；
- 目录：一句话说明来自正文首行、同名以后出现的为准；
- 加载：正文进工具回执、参数透传、重复调用只回一行、坏名字 fail-closed；
- 接线：系统提示只放清单不放正文；运行时按目录决定工具在不在；子代理拿到独立副本，
  它加载过的 Skill 不能让父代理误以为自己也见过正文。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from blank_api.db import Base, get_engine, get_session_factory
from blank_api.services.agent.graph import initial_messages
from blank_api.services.agent.runtime import AgentRuntime, RunRequest
from blank_api.services.models.base import AdapterConfig, ModelAdapter, ModelResponse, Usage
from blank_api.services.permissions import READ_ONLY_TOOLS
from blank_api.services.subagent import READ_ONLY_TOOLS as SUBAGENT_READ_ONLY_TOOLS
from blank_api.services.subagent import (
    SubAgentRequest,
    SubAgentResult,
    SubAgentRunner,
    run_subagent,
)
from blank_api.services.tools import SkillCatalog, build_default_tools
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.registry import LEAN_TOOLS, PARALLEL_SAFE_TOOLS, ToolRegistry
from blank_api.services.tools.skills import SUMMARY_CHARS, skill_summary, skill_tools
from tests.test_agent_v04 import env  # noqa: F401  (复用端到端夹具)

DEPLOY_BODY = """# 上线步骤

先把测试跑绿，再打标签。
第一步：pytest 全量
第二步：git tag
"""


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    async def add(self, event: str, phase: str, payload: dict) -> None:
        self.events.append((event, phase, payload))


def _context(catalog: SkillCatalog | None) -> ToolContext:
    return ToolContext(
        root=Path("."),
        task_id="task-skill",
        session_factory=None,
        skills=catalog,
        recorder=_Recorder(),
    )


def _tool():
    return {tool.name: tool for tool in skill_tools()}["skill"]


@pytest.fixture
async def skill_schema():
    """网关每记一次调用都要写 ToolCall 行；只给走 run_subagent 的用例把表建出来。"""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


# --------------------------------------------------------------------------- #
# 1. 工具面
# --------------------------------------------------------------------------- #
def test_skill_is_wired_into_the_default_surface() -> None:
    tools = {tool.name: tool for tool in build_default_tools()}
    assert "skill" in tools
    tool = tools["skill"]
    assert tool.risk_level == "low"
    assert tool.requires_approval is False
    assert tool.input_schema["required"] == ["skill"]
    # 名字必须"照着清单来"，不能让模型自己编
    assert "不要猜" in tool.description
    assert "skill" in tool.description


def test_skill_survives_the_lean_profile() -> None:
    """lean 视图只从白名单里裁：清单里列了 Skill，就必须留一条加载它的路。"""
    assert "skill" in LEAN_TOOLS
    registry = ToolRegistry(build_default_tools())
    registry.set_profile("lean")
    assert "skill" in registry.visible_names()


def test_skill_is_searchable_by_its_chinese_aliases() -> None:
    """模型说「技能」「工作流」时要能检索到它，而不是只认英文名。"""
    registry = ToolRegistry(build_default_tools())
    assert "skill" in [tool.name for tool in registry.search("技能")[0]]


def test_skill_is_allowed_in_plan_mode() -> None:
    """加载说明只是读内存，不碰工作区；计划模式最该先知道项目的流程要求。"""
    assert "skill" in READ_ONLY_TOOLS


def test_skill_is_not_parallel_safe() -> None:
    """调用会改会话状态（记已加载），而且语义上要先拿到正文再动手。"""
    assert "skill" not in PARALLEL_SAFE_TOOLS


def test_subagents_can_load_skills() -> None:
    assert "skill" in SUBAGENT_READ_ONLY_TOOLS


# --------------------------------------------------------------------------- #
# 2. 目录
# --------------------------------------------------------------------------- #
def test_summary_is_the_first_content_line_with_markdown_marks_stripped() -> None:
    assert skill_summary(DEPLOY_BODY) == "上线步骤"
    assert skill_summary("\n\n  - 先看结论\n正文") == "先看结论"
    assert skill_summary("") == ""


def test_summary_is_truncated_to_one_line_of_constant_cost() -> None:
    long_line = "很长的说明" * 100
    summary = skill_summary(long_line)
    assert len(summary) == SUMMARY_CHARS
    assert summary.endswith("…")


def test_catalog_keeps_the_latest_revision_of_a_duplicate_name() -> None:
    catalog = SkillCatalog(
        [
            {"name": "deploy", "instructions": "# 旧流程\n旧正文"},
            {"name": "deploy", "instructions": "# 新流程\n新正文"},
        ]
    )
    assert catalog.names() == ["deploy"]
    assert catalog.get("deploy")["instructions"] == "# 新流程\n新正文"
    assert [item["description"] for item in catalog.listing()] == ["新流程"]


def test_catalog_skips_entries_without_a_name() -> None:
    catalog = SkillCatalog([{"name": "  ", "instructions": "x"}, {"instructions": "y"}])
    assert len(catalog) == 0
    assert catalog.listing() == []


# --------------------------------------------------------------------------- #
# 3. 加载
# --------------------------------------------------------------------------- #
async def test_loading_returns_the_body_and_marks_it_loaded() -> None:
    catalog = SkillCatalog([{"name": "deploy", "instructions": DEPLOY_BODY}])
    outcome = await _tool().handler(_context(catalog), {"skill": "deploy"})

    assert outcome.ok, outcome.message
    assert "pytest 全量" in outcome.output
    assert outcome.data == {"skill": "deploy", "already_loaded": False}
    assert catalog.loaded == {"deploy"}


async def test_second_call_only_says_it_is_already_loaded() -> None:
    catalog = SkillCatalog([{"name": "deploy", "instructions": DEPLOY_BODY}])
    context = _context(catalog)
    await _tool().handler(context, {"skill": "deploy"})

    again = await _tool().handler(context, {"skill": "deploy"})

    assert again.ok
    assert again.data["already_loaded"] is True
    # 正文不能再贴一遍：重复调用不该为同一段说明付第二次 token
    assert "pytest 全量" not in again.output
    assert len(catalog.loaded) == 1


async def test_args_are_appended_when_the_body_has_no_placeholder() -> None:
    catalog = SkillCatalog([{"name": "deploy", "instructions": "按步骤上线"}])
    outcome = await _tool().handler(_context(catalog), {"skill": "deploy", "args": "生产环境"})
    assert outcome.output.strip().endswith("调用参数：生产环境")


async def test_args_replace_the_placeholder() -> None:
    catalog = SkillCatalog([{"name": "deploy", "instructions": "目标环境：$ARGUMENTS\n其余照旧"}])
    outcome = await _tool().handler(_context(catalog), {"skill": "deploy", "args": "staging"})
    assert "目标环境：staging" in outcome.output
    assert "$ARGUMENTS" not in outcome.output


async def test_unknown_name_fails_closed_and_lists_the_real_names() -> None:
    catalog = SkillCatalog([{"name": "deploy", "instructions": DEPLOY_BODY}])
    outcome = await _tool().handler(_context(catalog), {"skill": "deplyo"})

    assert outcome.ok is False
    assert outcome.status == "error"
    assert "deplyo" in outcome.message
    assert "deploy" in outcome.message
    assert "不要猜" in outcome.message
    assert catalog.loaded == set()


async def test_leading_slash_is_rejected_with_the_fixed_name() -> None:
    """用户打 /deploy 可以，模型照着把斜杠传进来时要直接说清楚怎么改。"""
    catalog = SkillCatalog([{"name": "deploy", "instructions": DEPLOY_BODY}])
    outcome = await _tool().handler(_context(catalog), {"skill": "/deploy"})

    assert outcome.ok is False
    assert "deploy" in outcome.message
    assert "前导斜杠" in outcome.message
    assert catalog.loaded == set()


async def test_no_catalog_fails_closed() -> None:
    outcome = await _tool().handler(_context(None), {"skill": "deploy"})
    assert outcome.ok is False
    assert "没有可用的 Skill" in outcome.message


async def test_loading_records_one_event_with_whether_args_were_given() -> None:
    catalog = SkillCatalog([{"name": "deploy", "instructions": DEPLOY_BODY}])
    context = _context(catalog)
    await _tool().handler(context, {"skill": "deploy", "args": "生产"})
    await _tool().handler(context, {"skill": "deploy"})

    events = [item for item in context.recorder.events if item[0] == "skill.invoked"]
    assert len(events) == 1
    assert events[0][1] == "skill"
    assert events[0][2]["skill"] == "deploy"
    assert events[0][2]["has_args"] is True


# --------------------------------------------------------------------------- #
# 4. 系统提示与运行时接线
# --------------------------------------------------------------------------- #
def test_system_prompt_lists_names_and_descriptions_but_not_the_body() -> None:
    content = initial_messages(
        "上线",
        [],
        skill_catalog=[{"name": "deploy", "description": "上线步骤"}],
    )[0]["content"]

    assert "可用 Skill" in content
    assert "- deploy：上线步骤" in content
    # 正文里的步骤不能出现在提示里——那正是 skill 工具要按需加载的部分
    assert "pytest 全量" not in content


def test_system_prompt_ignores_body_even_when_the_snapshot_carries_it() -> None:
    """快照里就算多带了正文，提示词也只能出说明那一句。"""
    content = initial_messages(
        "上线",
        [],
        skill_catalog=[
            {"name": "deploy", "description": "上线步骤", "instructions": DEPLOY_BODY}
        ],
    )[0]["content"]

    assert "- deploy：上线步骤" in content
    assert "pytest 全量" not in content


def test_legacy_skill_instructions_still_render() -> None:
    """外部集成还在用老的 skill_instructions：行为保持不变。"""
    content = initial_messages("目标", [], ["先看结论"])[0]["content"]
    assert "用户已明确启用以下 Skill" in content
    assert "先看结论" in content


class _TinyAdapter(ModelAdapter):
    def __init__(self) -> None:
        super().__init__(
            AdapterConfig(
                name="tiny",
                kind="openai",
                base_url="http://localhost",
                api_key="",
                model="tiny",
                context_window=32_000,
                supports_tools=True,
            )
        )

    async def invoke(self, messages, **kwargs):  # pragma: no cover
        return ModelResponse(text="{}", usage=Usage(1, 1), model="tiny")

    async def stream(self, messages, **kwargs):  # pragma: no cover
        yield ""

    async def bind_tools(self, specs, messages, **kwargs):  # pragma: no cover
        return ModelResponse(text="ok", usage=Usage(1, 1), model="tiny")


def _request(task_id: str, workspace: Path, catalog) -> RunRequest:
    return RunRequest(
        task_id=task_id,
        provider_id="a",
        provider_name="A",
        adapter=_TinyAdapter(),
        root=workspace,
        goal="按项目流程上线",
        max_steps=3,
        skill_catalog=tuple(catalog),
    )


async def test_prepare_builds_the_catalog_and_keeps_the_skill_tool(workspace) -> None:
    runtime = AgentRuntime(session_factory=None)
    scope = await runtime._prepare(
        _request("task-skill-on", workspace, [{"name": "deploy", "instructions": DEPLOY_BODY}]),
        "task-skill-on:1",
    )

    assert scope.session.context.skills is not None
    assert scope.session.context.skills.names() == ["deploy"]
    assert scope.session.skill_catalog == [{"name": "deploy", "description": "上线步骤"}]
    assert "skill" in [tool.name for tool in scope.session.gateway.registry.visible()]


async def test_prepare_drops_the_skill_tool_without_a_catalog(workspace) -> None:
    runtime = AgentRuntime(session_factory=None)
    scope = await runtime._prepare(_request("task-skill-off", workspace, []), "task-skill-off:1")

    assert scope.session.context.skills is None
    assert scope.session.skill_catalog == []
    assert "skill" not in [tool.name for tool in scope.session.gateway.registry.visible()]


# --------------------------------------------------------------------------- #
# 5. 子代理：目录是副本，加载状态不串
# --------------------------------------------------------------------------- #
class _ScriptedAdapter(ModelAdapter):
    def __init__(self, script: list[ModelResponse]) -> None:
        super().__init__(
            AdapterConfig(
                name="mock",
                kind="openai_compatible",
                base_url="",
                api_key="",
                model="mock",
                supports_tools=True,
            )
        )
        self.script = script
        self.calls = 0
        self.seen: list[list] = []
        self.spec_names: list[list[str]] = []

    async def invoke(self, messages, **kwargs):  # pragma: no cover
        raise NotImplementedError

    async def stream(self, messages, **kwargs):  # pragma: no cover
        raise NotImplementedError

    async def bind_tools(self, tools, messages, **kwargs):
        self.seen.append(list(messages))
        self.spec_names.append([getattr(item, "name", "") for item in tools])
        item = self.script[self.calls]
        self.calls += 1
        return item


async def _silent(*args, **kwargs) -> None:
    return None


async def test_subagent_can_load_a_skill_from_its_own_catalog_copy(
    tmp_path, skill_schema: None
) -> None:
    adapter = _ScriptedAdapter(
        [
            ModelResponse(
                text="",
                usage=Usage(10, 5),
                model="mock",
                tool_calls=[
                    {
                        "id": "call-skill",
                        "type": "function",
                        "function": {"name": "skill", "arguments": '{"skill": "deploy"}'},
                    }
                ],
            ),
            ModelResponse(text="已按上线步骤处理", usage=Usage(10, 5), model="mock"),
        ]
    )
    parent = SkillCatalog([{"name": "deploy", "instructions": DEPLOY_BODY}])

    result = await run_subagent(
        adapter=adapter,
        tools=skill_tools(),
        root=tmp_path,
        task_id="",
        # 网关落 ToolCall 行必须能开会话；这里用测试库的工厂，别传 None。
        session_factory=get_session_factory(),
        recorder=SimpleNamespace(add=_silent),
        request=SubAgentRequest(goal="按流程上线", max_steps=3),
        agent_path="root/sub-1",
        depth=1,
        parent_thread_id="t",
        provider_id="",
        provider_name="",
        skill_catalog=parent,
    )

    assert result.ok, result.error
    assert result.summary == "已按上线步骤处理"
    # 第二次调用时，工具回执里必须已经有正文
    tool_messages = [
        item
        for item in adapter.seen[1]
        if getattr(item, "role", item.get("role") if isinstance(item, dict) else "") == "tool"
    ]
    assert tool_messages, "子代理没有拿到 skill 的工具回执"
    assert "pytest 全量" in str(getattr(tool_messages[0], "content", ""))
    # 子代理的加载状态不能写回父目录
    assert parent.loaded == set()


async def test_subagent_without_a_catalog_cannot_see_the_skill_tool(
    tmp_path, skill_schema: None
) -> None:
    adapter = _ScriptedAdapter([ModelResponse(text="直接答", usage=Usage(1, 1), model="mock")])

    result = await run_subagent(
        adapter=adapter,
        tools=skill_tools(),
        root=tmp_path,
        task_id="",
        session_factory=get_session_factory(),
        recorder=SimpleNamespace(add=_silent),
        request=SubAgentRequest(goal="随便看看", max_steps=2),
        agent_path="root/sub-2",
        depth=1,
        parent_thread_id="t",
    )

    assert result.ok
    # 传给模型的工具表里不该出现 skill：没有可加载的名字却摆着入口只会让模型反复试错
    assert adapter.spec_names
    assert "skill" not in adapter.spec_names[0]


async def test_runner_hands_its_parent_catalog_to_the_subagent(monkeypatch, tmp_path) -> None:
    """spawn_agent 这条路上，父会话的 Skill 目录必须真的传下去。

    直接调 run_subagent 的用例证明不了接线：真正的调用点在 SubAgentRunner 里，
    漏传一行参数，子代理的系统提示就没有清单、工具表也没有加载入口。
    """
    import blank_api.services.subagent as subagent_module

    captured: dict = {}

    async def fake_run_subagent(**kwargs):
        captured.update(kwargs)
        return SubAgentResult(ok=True, summary="ok")

    monkeypatch.setattr(subagent_module, "run_subagent", fake_run_subagent)
    catalog = SkillCatalog([{"name": "deploy", "instructions": DEPLOY_BODY}])
    context = ToolContext(
        root=tmp_path, task_id="task-runner", session_factory=None, skills=catalog
    )
    session = SimpleNamespace(
        depth=0,
        agent_path="root",
        trace_id="trace-1",
        root_thread_id="trace-1",
        sandbox_tier="workspace",
        recorder=_Recorder(),
        adapter=SimpleNamespace(),
        context=context,
        session_factory=None,
    )

    result = await SubAgentRunner().bind(session)(SubAgentRequest(goal="按流程上线"))

    assert result.ok
    assert captured["skill_catalog"] is catalog
    # 传的是父目录本身，副本由 run_subagent 建；父目录的加载状态不能被这轮写脏。
    assert catalog.loaded == set()


async def test_fan_out_hands_the_parent_catalog_to_every_subagent(monkeypatch, tmp_path) -> None:
    """并行编排是另一处调用点：漏传同样会让整批子代理拿不到清单。"""
    import blank_api.services.subagent as subagent_module

    seen: list = []

    async def fake_run_subagent(**kwargs):
        seen.append(kwargs.get("skill_catalog"))
        return SubAgentResult(ok=True, summary="ok")

    monkeypatch.setattr(subagent_module, "run_subagent", fake_run_subagent)
    catalog = SkillCatalog([{"name": "deploy", "instructions": DEPLOY_BODY}])
    context = ToolContext(
        root=tmp_path, task_id="task-fanout", session_factory=None, skills=catalog
    )
    session = SimpleNamespace(
        depth=0,
        agent_path="root",
        trace_id="trace-2",
        root_thread_id="trace-2",
        sandbox_tier="workspace",
        recorder=_Recorder(),
        adapter=SimpleNamespace(),
        context=context,
        session_factory=None,
    )

    results = await SubAgentRunner().bind(session).fan_out(
        [SubAgentRequest(goal="甲"), SubAgentRequest(goal="乙")]
    )

    assert len(results) == 2 and all(item.ok for item in results)
    assert seen and all(item is catalog for item in seen)


async def test_task_run_snapshot_turns_a_skill_into_a_catalog_line(env, workspace) -> None:
    """真实任务快照：库里的 Skill 只以「名字 + 一句话」进系统提示，正文不进。

    前面的用例直接喂 skill_catalog，证明不了 routes/tasks.py 取的是名字+正文、
    也证明不了运行时那一步快照真的到了模型手里。
    """
    from blank_api.models.skill import Skill
    from tests.test_agent_v04 import API, create_project, create_provider, create_task

    async with get_session_factory()() as session:
        session.add(
            Skill(id="skill-deploy", name="deploy", instructions=DEPLOY_BODY, status="active", history=[])
        )
        await session.commit()
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["跑完测试"])
    env.mock.reset([{"content": "已完成"}])

    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text

    payload = env.mock.requests[0]
    system = "\n".join(
        str(item.get("content") or "")
        for item in payload["messages"]
        if item.get("role") == "system"
    )
    assert "- deploy：上线步骤" in system
    assert "pytest 全量" not in system
    tool_names = [(item.get("function") or {}).get("name") for item in payload.get("tools") or []]
    assert "skill" in tool_names


@pytest.mark.parametrize("name", ["deploy", "ops:deploy"])
def test_catalog_accepts_plugin_style_names(name: str) -> None:
    catalog = SkillCatalog([{"name": name, "instructions": "说明"}])
    assert catalog.names() == [name]
