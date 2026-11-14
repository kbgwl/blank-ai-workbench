"""Skill frontmatter ``agent`` + ``background``（#303）：判据。

上游锚点：``lme`` 返回结构里 ``agent:O.agent!=null?String(O.agent):void 0`` 与
``background:m1(O.background)``。SkillTool 执行时 ``vQt(e)`` 判定是否后台
（``e.background??!0``），``EQt`` 后台派发；非后台走前台 fork 子代理。``agent``
决定用哪个 agent 定义执行正文。

本批只移植 Skill 侧（skill 工具处理器路径），命令侧的 ``agent``/``background``
在输入管道（``expand_user_input``），不在本批范围，仍留在 ``UNSUPPORTED_FIELDS``。

钉的不是「字段存在」——每条判据指向一个会走形的具体行为：解析丢了字段、目录不
携带、handler 不派生、未知类型不 fail-closed、无 runner 不报错。改前这些用例要么
AttributeError、要么断言失败。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from blank_api.services.agents_defs import AgentCatalog, AgentDefinition
from blank_api.services.skills_defs import (
    SKILL_UNSUPPORTED_FIELDS,
    UNSUPPORTED_FIELDS,
    SkillDefinition,
    _definition_from_dir,
)
from blank_api.services.subagent import SubAgentResult
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.skills import SkillCatalog, skill_tools

from tests.test_skills_disk_v45 import _write_skill


# --------------------------------------------------------------------------- #
# 测试装置
# --------------------------------------------------------------------------- #


class _FakeRunner:
    """记录收到的请求，返回预设结果。前台 ``__call__`` + 后台 ``spawn_background``。"""

    def __init__(self, *, foreground_ok: bool = True, background_ok: bool = True):
        self.requests: list = []
        self.foreground_ok = foreground_ok
        self.background_ok = background_ok
        self.foreground_result = SubAgentResult(ok=foreground_ok, summary="子代理结论")
        self.background_agent_id = "bg-agent-1"

    async def __call__(self, request, *, agent_id: str = "") -> SubAgentResult:
        self.requests.append(("foreground", request))
        return self.foreground_result

    async def spawn_background(self, request):
        self.requests.append(("background", request))
        if self.background_ok:
            return self.background_agent_id, None
        return "", SubAgentResult(ok=False, error="后台启动失败")


def _reviewer_catalog() -> AgentCatalog:
    """一个含 reviewer 与默认 general-purpose 的 AgentCatalog，供 _resolve_agent 解析。"""
    return AgentCatalog(
        entries={
            "general-purpose": AgentDefinition(
                name="general-purpose",
                description="默认子代理",
                instructions="你是通用子代理。",
            ),
            "reviewer": AgentDefinition(
                name="reviewer",
                description="评审子代理",
                instructions="你是评审员。",
            )
        }
    )


def _tool_context(
    catalog: SkillCatalog,
    *,
    runner: _FakeRunner | None = None,
    agents: AgentCatalog | None = None,
) -> ToolContext:
    return ToolContext(
        root=Path("."),
        task_id="task-ab",
        session_factory=None,
        skills=catalog,
        subagent_runner=runner,
        agents=agents,
        turn_state={},
    )


def _skill_tool():
    return {tool.name: tool for tool in skill_tools()}["skill"]


# --------------------------------------------------------------------------- #
# 1. 解析
# --------------------------------------------------------------------------- #


def test_agent_frontmatter_is_parsed_into_definition(tmp_path: Path) -> None:
    """``agent: reviewer`` 解析进 ``SkillDefinition.agent``。

    解析里丢了这个字段，skill 声明了 agent 也不会生效——handler 拿到的永远是空串。
    """
    skill_dir = tmp_path / "review-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: review-skill\ndescription: 评审\nagent: reviewer\n---\n评审代码。\n",
        encoding="utf-8",
    )
    definition, errors, warnings = _definition_from_dir(skill_dir, "project")
    assert definition is not None
    assert definition.agent == "reviewer"
    assert not errors


def test_agent_whitespace_is_stripped(tmp_path: Path) -> None:
    """``agent: "  reviewer  "`` 首尾空白要去掉。

    不 strip，``_resolve_agent`` 按名字查不到，报"未知类型"。
    """
    skill_dir = tmp_path / "padded-agent"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        '---\nname: padded-agent\ndescription: d\nagent: "  reviewer  "\n---\nbody\n',
        encoding="utf-8",
    )
    definition, _, _ = _definition_from_dir(skill_dir, "project")
    assert definition is not None
    assert definition.agent == "reviewer"


def test_agent_non_string_is_dropped_with_warning(tmp_path: Path) -> None:
    """``agent: 123`` 不是字符串——告警丢弃，不猜一个值填入。

    不告警直接丢，用户不知道写了什么；把 123 当 agent 名查，报错离书写位置太远。
    """
    skill_dir = tmp_path / "bad-agent"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: bad-agent\ndescription: d\nagent: 123\n---\nbody\n",
        encoding="utf-8",
    )
    definition, _, warnings = _definition_from_dir(skill_dir, "project")
    assert definition is not None
    assert definition.agent == ""
    assert any("agent" in w and "不是非空字符串" in w for w in warnings)


def test_agent_absent_defaults_to_empty(tmp_path: Path) -> None:
    """不写 ``agent`` 字段——定义里 agent 是空串，不派生子代理。

    默认值写错（比如默认成 general-purpose），每个没声明 agent 的 skill 都会
    悄悄派生子代理，这是最隐蔽的走形。
    """
    skill_dir = tmp_path / "no-agent"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: no-agent\ndescription: d\n---\nbody\n",
        encoding="utf-8",
    )
    definition, _, _ = _definition_from_dir(skill_dir, "project")
    assert definition is not None
    assert definition.agent == ""


def test_background_true_is_parsed(tmp_path: Path) -> None:
    """``background: true`` 解析进 ``SkillDefinition.background`` 为 True。

    解析里丢了这个字段或默认值写成 False，声明了 background 的 skill 也不会后台跑。
    """
    skill_dir = tmp_path / "bg-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: bg-skill\ndescription: d\nbackground: true\n---\nbody\n",
        encoding="utf-8",
    )
    definition, _, _ = _definition_from_dir(skill_dir, "project")
    assert definition is not None
    assert definition.background is True


def test_background_false_is_parsed(tmp_path: Path) -> None:
    """``background: false`` 解析为 False——等同没写。

    解析把 false 当成 True，声明了 background:false 的 skill 也会后台跑。
    """
    skill_dir = tmp_path / "fg-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: fg-skill\ndescription: d\nbackground: false\n---\nbody\n",
        encoding="utf-8",
    )
    definition, _, _ = _definition_from_dir(skill_dir, "project")
    assert definition is not None
    assert definition.background is False


def test_background_absent_defaults_to_false(tmp_path: Path) -> None:
    """不写 ``background`` 字段——定义里 background 是 False。

    默认值写成 True，每个没声明 background 的 skill 都会后台派发，行为全变。
    """
    skill_dir = tmp_path / "no-bg"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: no-bg\ndescription: d\n---\nbody\n",
        encoding="utf-8",
    )
    definition, _, _ = _definition_from_dir(skill_dir, "project")
    assert definition is not None
    assert definition.background is False


def test_background_invalid_value_warns(tmp_path: Path) -> None:
    """``background: maybe`` 不是布尔值——告警并按默认 False 处理。

    不告警直接丢，用户不知道写了什么；把 maybe 当 True 或 False 都靠猜。
    """
    skill_dir = tmp_path / "bad-bg"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: bad-bg\ndescription: d\nbackground: maybe\n---\nbody\n",
        encoding="utf-8",
    )
    definition, _, warnings = _definition_from_dir(skill_dir, "project")
    assert definition is not None
    assert definition.background is False
    assert any("background" in w for w in warnings)


# --------------------------------------------------------------------------- #
# 2. UNSUPPORTED_FIELDS 分叉
# --------------------------------------------------------------------------- #


def test_agent_not_in_skill_unsupported_fields() -> None:
    """``agent`` 不在 ``SKILL_UNSUPPORTED_FIELDS`` 里——Skill 侧识别并生效。

    还在列表里，解析时会被记进 ignored 但不生效，前面的解析用例全红。
    """
    assert "agent" not in SKILL_UNSUPPORTED_FIELDS


def test_background_not_in_skill_unsupported_fields() -> None:
    """``background`` 不在 ``SKILL_UNSUPPORTED_FIELDS`` 里。"""
    assert "background" not in SKILL_UNSUPPORTED_FIELDS


def test_agent_still_in_command_unsupported_fields() -> None:
    """``agent`` 仍在 ``UNSUPPORTED_FIELDS`` 里——命令侧本批不实现，保持忽略审计。

    从 UNSUPPORTED_FIELDS 也删掉，命令的 agent 字段会从 ignored 审计里消失，
    用户不知道命令声明了 agent 但没生效。
    """
    assert "agent" in UNSUPPORTED_FIELDS


def test_background_still_in_command_unsupported_fields() -> None:
    """``background`` 仍在 ``UNSUPPORTED_FIELDS`` 里——命令侧本批不实现。"""
    assert "background" in UNSUPPORTED_FIELDS


def test_skill_unsupported_is_subset_of_command_unsupported() -> None:
    """``SKILL_UNSUPPORTED_FIELDS`` 是 ``UNSUPPORTED_FIELDS`` 去掉 agent/background。

    两个列表的差集不是恰好 {agent, background}，说明分叉写错了。
    """
    removed = set(UNSUPPORTED_FIELDS) - set(SKILL_UNSUPPORTED_FIELDS)
    assert removed == {"agent", "background"}


# --------------------------------------------------------------------------- #
# 3. as_entry / as_dict 携带
# --------------------------------------------------------------------------- #


def test_as_entry_includes_agent_and_background() -> None:
    """``as_entry()`` 的字典里有 ``agent`` 和 ``background`` 键。

    as_entry 不写这两个键，SkillCatalog 拿不到，handler 自然也派不了子代理。
    """
    definition = SkillDefinition(
        name="x", description="d", instructions="body", agent="reviewer", background=True
    )
    entry = definition.as_entry()
    assert entry["agent"] == "reviewer"
    assert entry["background"] is True


def test_as_entry_defaults_for_absent_fields() -> None:
    """没声明时 ``as_entry()`` 的 ``agent`` 是空串、``background`` 是 False。

    默认值写成别的（比如 agent 默认 None），handler 的 ``str(entry.get("agent") or "")``
    会把 None 读成 "None"，传给 _resolve_agent 就是一个假类型名。
    """
    definition = SkillDefinition(name="x", description="d", instructions="b")
    entry = definition.as_entry()
    assert entry["agent"] == ""
    assert entry["background"] is False


def test_as_dict_includes_agent_when_declared() -> None:
    """审计视图 ``as_dict()`` 声明了 agent 才带上——没声明的不出现在审计里。"""
    with_agent = SkillDefinition(
        name="x", description="d", instructions="b", agent="reviewer"
    )
    assert with_agent.as_dict()["agent"] == "reviewer"

    without = SkillDefinition(name="y", description="d", instructions="b")
    assert "agent" not in without.as_dict()


def test_as_dict_includes_background_when_true() -> None:
    """审计视图 ``as_dict()`` background 为 True 才带上。"""
    with_bg = SkillDefinition(
        name="x", description="d", instructions="b", background=True
    )
    assert with_bg.as_dict()["background"] is True

    without = SkillDefinition(name="y", description="d", instructions="b")
    assert "background" not in without.as_dict()


# --------------------------------------------------------------------------- #
# 4. 目录携带
# --------------------------------------------------------------------------- #


def test_catalog_entry_carries_agent_and_background() -> None:
    """``SkillCatalog`` 构造时把 entry 里的 ``agent``/``background`` 带进内部字典。

    构造时不取这两个键，handler 从 entry 里读出来永远是空/False。
    """
    catalog = SkillCatalog(
        [
            {
                "name": "review",
                "description": "d",
                "instructions": "body",
                "agent": "reviewer",
                "background": True,
            }
        ]
    )
    entry = catalog.get("review")
    assert entry is not None
    assert entry["agent"] == "reviewer"
    assert entry["background"] is True


def test_catalog_entry_defaults_for_absent_fields() -> None:
    """没传 ``agent``/``background`` 键的 entry——目录里是空串/False。"""
    catalog = SkillCatalog([{"name": "plain", "description": "d", "instructions": "b"}])
    entry = catalog.get("plain")
    assert entry is not None
    assert entry["agent"] == ""
    assert entry["background"] is False


# --------------------------------------------------------------------------- #
# 5. handler：agent → 前台子代理派发
# --------------------------------------------------------------------------- #


async def test_skill_with_agent_spawns_foreground_subagent() -> None:
    """skill 有 ``agent: reviewer``——handler 派发前台子代理，不返回正文。

    handler 不检查 agent 字段，直接返回渲染正文（主线程注入），子代理永远不派生。
    """
    catalog = SkillCatalog(
        [
            {
                "name": "review",
                "description": "d",
                "instructions": "评审这份代码。",
                "agent": "reviewer",
            }
        ]
    )
    runner = _FakeRunner()
    context = _tool_context(catalog, runner=runner, agents=_reviewer_catalog())
    tool = _skill_tool()
    outcome = await tool.handler(context, {"skill": "review"})
    assert outcome.ok
    assert len(runner.requests) == 1
    mode, request = runner.requests[0]
    assert mode == "foreground"
    assert request.goal == "评审这份代码。"
    assert request.agent_name == "reviewer"
    # 派发结果里带 fork 标记与 agent 类型，让模型知道不是主线程执行
    assert outcome.data.get("status") == "forked"
    assert outcome.data.get("agent_type") == "reviewer"


async def test_skill_with_agent_body_goes_to_subagent_not_prompt() -> None:
    """skill 正文作为子代理 goal 传出——不是作为提示词返回给主线程。

    handler 把正文拼进返回文本而不是传给子代理，主线程拿到正文自己执行，
    与声明 agent 的意图相反。
    """
    catalog = SkillCatalog(
        [
            {
                "name": "review",
                "description": "d",
                "instructions": "评审这份代码并给出结论。",
                "agent": "reviewer",
            }
        ]
    )
    runner = _FakeRunner()
    context = _tool_context(catalog, runner=runner, agents=_reviewer_catalog())
    tool = _skill_tool()
    outcome = await tool.handler(context, {"skill": "review"})
    assert outcome.ok
    _, request = runner.requests[0]
    assert "评审这份代码并给出结论。" in request.goal
    # 返回文本里不应包含完整正文（子代理执行，主线程不需要正文）
    assert "评审这份代码并给出结论。" not in outcome.output


async def test_skill_with_unknown_agent_fails_closed() -> None:
    """skill 有 ``agent: nonexistent``——handler fail-closed，列出可用类型。

    不 fail-closed，要么静默回退到主线程注入（用户以为派生了子代理），
    要么把不存在的类型名传给运行器报错。
    """
    catalog = SkillCatalog(
        [
            {
                "name": "bad",
                "description": "d",
                "instructions": "body",
                "agent": "nonexistent",
            }
        ]
    )
    runner = _FakeRunner()
    context = _tool_context(catalog, runner=runner, agents=_reviewer_catalog())
    tool = _skill_tool()
    outcome = await tool.handler(context, {"skill": "bad"})
    assert not outcome.ok
    assert len(runner.requests) == 0
    error_text = outcome.message
    assert "nonexistent" in error_text
    assert "reviewer" in error_text  # 列出可用类型


async def test_skill_with_agent_no_runner_errors() -> None:
    """skill 有 ``agent`` 但 context 没有 ``subagent_runner``——明确报错。

    不报错，要么 AttributeError 崩溃，要么静默回退到主线程注入。
    """
    catalog = SkillCatalog(
        [
            {
                "name": "review",
                "description": "d",
                "instructions": "body",
                "agent": "reviewer",
            }
        ]
    )
    context = _tool_context(catalog, runner=None, agents=_reviewer_catalog())
    tool = _skill_tool()
    outcome = await tool.handler(context, {"skill": "review"})
    assert not outcome.ok
    error_text = outcome.message
    assert "子代理" in error_text or "runner" in error_text


# --------------------------------------------------------------------------- #
# 6. handler：background → 后台子代理派发
# --------------------------------------------------------------------------- #


async def test_skill_with_background_spawns_background_subagent() -> None:
    """skill 有 ``background: true``——handler 派发后台子代理，返回句柄。

    handler 不检查 background 字段，直接返回渲染正文，后台子代理永远不派生。
    """
    catalog = SkillCatalog(
        [
            {
                "name": "bg",
                "description": "d",
                "instructions": "后台跑的任务。",
                "background": True,
            }
        ]
    )
    runner = _FakeRunner()
    context = _tool_context(catalog, runner=runner, agents=_reviewer_catalog())
    tool = _skill_tool()
    outcome = await tool.handler(context, {"skill": "bg"})
    assert outcome.ok
    assert len(runner.requests) == 1
    mode, request = runner.requests[0]
    assert mode == "background"
    assert request.goal == "后台跑的任务。"
    assert outcome.data.get("status") == "forked"
    assert outcome.data.get("background") is True
    assert outcome.data.get("agent_id") == "bg-agent-1"


async def test_skill_with_agent_and_background_spawns_bg_subagent_of_type() -> None:
    """skill 有 ``agent: reviewer`` + ``background: true``——后台派发 reviewer 子代理。

    agent 与 background 同时声明时，后台子代理用的是声明的 agent 类型，不是默认类型。
    """
    catalog = SkillCatalog(
        [
            {
                "name": "review-bg",
                "description": "d",
                "instructions": "后台评审。",
                "agent": "reviewer",
                "background": True,
            }
        ]
    )
    runner = _FakeRunner()
    context = _tool_context(catalog, runner=runner, agents=_reviewer_catalog())
    tool = _skill_tool()
    outcome = await tool.handler(context, {"skill": "review-bg"})
    assert outcome.ok
    mode, request = runner.requests[0]
    assert mode == "background"
    assert request.agent_name == "reviewer"
    assert outcome.data.get("background") is True


async def test_skill_with_background_no_spawn_background_errors() -> None:
    """skill 有 ``background: true`` 但 runner 没有 ``spawn_background``——明确报错。

    不报错，要么 AttributeError 崩溃，要么静默回退到前台派发（用户以为后台跑）。
    """

    class _NoBackgroundRunner:
        async def __call__(self, request, *, agent_id: str = ""):
            return SubAgentResult(ok=True, summary="ok")

    catalog = SkillCatalog(
        [
            {
                "name": "bg",
                "description": "d",
                "instructions": "body",
                "background": True,
            }
        ]
    )
    context = _tool_context(
        catalog, runner=_NoBackgroundRunner(), agents=_reviewer_catalog()
    )
    tool = _skill_tool()
    outcome = await tool.handler(context, {"skill": "bg"})
    assert not outcome.ok


# --------------------------------------------------------------------------- #
# 7. handler：无字段 → 保持主线程注入
# --------------------------------------------------------------------------- #


async def test_skill_without_agent_or_background_returns_prompt() -> None:
    """skill 没声明 ``agent``/``background``——handler 返回渲染正文，不派生子代理。

    handler 无条件派生，每个普通 skill 都会派生子代理，行为全变。
    """
    catalog = SkillCatalog(
        [{"name": "plain", "description": "d", "instructions": "直接执行这段说明。"}]
    )
    runner = _FakeRunner()
    context = _tool_context(catalog, runner=runner, agents=_reviewer_catalog())
    tool = _skill_tool()
    outcome = await tool.handler(context, {"skill": "plain"})
    assert outcome.ok
    assert len(runner.requests) == 0
    # 主线程注入：返回文本里包含渲染后的正文
    assert "直接执行这段说明。" in outcome.output
    assert outcome.data.get("status") != "forked"


async def test_skill_with_background_false_returns_prompt() -> None:
    """skill 有 ``background: false``——等同没写，返回渲染正文。

    handler 把 false 当成 True 派生，声明了 background:false 的 skill 也后台跑。
    """
    catalog = SkillCatalog(
        [
            {
                "name": "fg",
                "description": "d",
                "instructions": "前台执行。",
                "background": False,
            }
        ]
    )
    runner = _FakeRunner()
    context = _tool_context(catalog, runner=runner, agents=_reviewer_catalog())
    tool = _skill_tool()
    outcome = await tool.handler(context, {"skill": "fg"})
    assert outcome.ok
    assert len(runner.requests) == 0
    assert "前台执行。" in outcome.output


# --------------------------------------------------------------------------- #
# 8. discover_skills 携带
# --------------------------------------------------------------------------- #


def test_discover_skills_carries_agent_and_background(tmp_path: Path) -> None:
    """``discover_skills`` 发现的 skill 带上 agent 和 background。

    发现层丢了字段，目录层再正确也没用——数据在源头就没了。
    """
    from blank_api.services.skills_defs import discover_skills

    _write_skill(
        tmp_path,
        "review",
        "---\nname: review\ndescription: 评审\nagent: reviewer\nbackground: true\n---\n评审代码。\n",
    )
    env = {"BLANK_USER_SKILLS_DIR": str(tmp_path.parent / "no-user")}
    discovery = discover_skills(tmp_path, env=env)
    assert "review" in discovery.entries
    definition = discovery.entries["review"]
    assert definition.agent == "reviewer"
    assert definition.background is True
