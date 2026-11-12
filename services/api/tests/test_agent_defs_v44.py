"""S1：文件型子代理定义（.blank/agents）与 spawn_agent(subagent_type)。

对齐 Claude Code 的 ``.claude/agents`` 契约，并钉住我们自己的边界口径：

1. **两层发现**：用户级 agents 目录 + 项目 ``.blank/agents``，同名项目胜；
   同层里更深的文件覆盖更浅的；
2. **坏文件点名、好文件照常**：无 frontmatter / 无 name 静默跳过；声明了 name
   但校验失败（缺 description、名字非法、坏 YAML、超 1 MiB）必须进错误清单，
   单个坏文件不能拖垮整个目录；
3. **工具只收窄**：``tools`` 是白名单且与只读/隔离基线取交集，写工具不会因为
   定义里写了就出现；``tools`` 的字段缺省与 ``"*"`` 都是不限，显式 ``null`` /
   假值是「没有工具」；``disallowedTools: "*"`` 按上游 ``VW`` 归一成
   ``undefined``，即等同于不写这条限制；
4. **maxTurns**：正整数进请求步数（受全局上限约束），非法值忽略并告警；
5. **skills**：frontmatter 声明的 Skill 在派发时预加载进子代理启动消息
   （``command-message`` 标记头 + 正文），缺名只告警不阻断派发；续聊按上游
   ``kDs`` 的 carriedPreloads 口径跳过上一轮已经带上的正文；
6. **未支持字段可见**：model / permissionMode / hooks 等不生效的字段进
   ``agent_ignored``，不能悄悄吞掉让用户以为配置起了作用；
7. **真的接通**：运行时发现目录、系统提示列出类型、spawn_agent 解析类型并把
   定义正文挂进子代理系统提示、``agents.loaded`` 与 ``subagent.started`` 进事件。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from blank_api.db import get_session_factory
from blank_api.services.agent.graph import initial_messages
from blank_api.services.agent.runtime import AgentRuntime, RunRequest
from blank_api.services.agents_defs import (
    BUILTIN_AGENT,
    BUILTIN_FORK,
    BUILTIN_GENERAL_PURPOSE,
    MAX_FILE_BYTES,
    MAX_INSTRUCTIONS_CHARS,
    UNSUPPORTED_FIELDS,
    discover_agents,
    load_definition,
)
from blank_api.services.models.base import ChatMessage
from blank_api.services.models.registry import build_adapter
from blank_api.services.subagent import (
    ISOLATED_TOOLS,
    READ_ONLY_TOOLS,
    SubAgentRequest,
    SubAgentResult,
    SubAgentRunner,
    _child_allowed_tools,
)
from blank_api.services.tools.delegate import delegate_tools
from blank_api.services.tools.skills import (
    SkillCatalog,
    _skill,
    preload_skills,
    skill_loading_header,
)

from tests.test_agent_v04 import (  # noqa: F401  (env 是夹具，故意导入)
    call,
    create_provider,
    create_project,
    create_task,
    env,
)
from tests.test_background_subagent_v116 import FakeRecorder, SlowAdapter, _session


# --------------------------------------------------------------------------- #
# 帮助：写定义、发现目录、抓取子代理看到的提示
# --------------------------------------------------------------------------- #
def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _agent(
    name: str = "reviewer",
    description: str = "只读审查",
    body: str = "先读再判。",
    extra: str = "",
) -> str:
    return f"---\nname: {name}\ndescription: {description}\n{extra}---\n{body}"


def _discover(root: Path, user_dir: Path | None = None):
    env = {"BLANK_USER_AGENTS_DIR": str(user_dir or (root.parent / "no-user-agents"))}
    return discover_agents(root, env=env)


def _tool_map() -> dict:
    return {item.name: item for item in delegate_tools()}


def _context(root: Path, runner, catalog=None) -> SimpleNamespace:
    context = SimpleNamespace(
        root=Path(root),
        task_id="task-v44",
        subagent_runner=runner,
    )
    if catalog is not None:
        context.agents = catalog
    return context


def _messages_text(messages) -> str:
    parts = []
    for item in messages:
        if isinstance(item, dict):
            parts.append(str(item.get("content") or ""))
        else:
            parts.append(str(getattr(item, "content", "") or ""))
    return "\n".join(parts)


def _contents(messages) -> list[str]:
    return [
        str(item.get("content") or "")
        if isinstance(item, dict)
        else str(getattr(item, "content", "") or "")
        for item in messages
    ]


class CaptureAdapter(SlowAdapter):
    """记录子代理每次调用收到的系统提示与工具表。"""

    def __init__(self, text: str = "子代理结论") -> None:
        super().__init__(0.01, text=text)
        self.calls: list[list] = []
        self.specs: list[list[str]] = []

    async def bind_tools(self, specs, messages, **kwargs):
        self.calls.append(list(messages))
        self.specs.append(
            [item if isinstance(item, str) else getattr(item, "name", "") for item in specs]
        )
        return await super().bind_tools(specs, messages, **kwargs)


class RecordingRunner:
    """只记录请求，不真跑子代理；用于判「工具层把什么交给了运行器」。"""

    def __init__(self) -> None:
        self.requests: list[SubAgentRequest] = []

    async def __call__(self, request: SubAgentRequest, **kwargs) -> SubAgentResult:
        self.requests.append(request)
        return SubAgentResult(ok=True, summary="完成", agent_id="agent-fake")

    async def fan_out(self, requests, *, concurrency=3, merge=False):
        self.requests.extend(requests)
        return [
            SubAgentResult(
                ok=True,
                summary=f"结论 {index}",
                parallel_group="par-v44",
                agent_path=f"par-{index}",
                thread_id=f"thread-{index}",
            )
            for index in range(len(requests))
        ]


# --------------------------------------------------------------------------- #
# 1. 两层发现与覆盖顺序
# --------------------------------------------------------------------------- #
def test_project_layer_wins_and_deeper_file_overrides(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    user = tmp_path / "user-agents"
    _write(user / "a.md", _agent(name="shared", description="用户层"))
    _write(root / ".blank/agents/a.md", _agent(name="shared", description="项目层"))
    _write(root / ".blank/agents/sub/deep.md", _agent(name="shared", description="深层"))

    catalog = _discover(root, user)

    definition = catalog.get("shared")
    assert definition is not None
    # 同一层里更深的文件后写入，覆盖浅层；项目层又覆盖用户层。
    assert definition.description == "深层"
    assert definition.source == "project"


def test_user_layer_loads_when_project_has_no_definition(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    user = tmp_path / "user-agents"
    _write(user / "helper.md", _agent(name="helper", description="用户级助手"))
    root.mkdir()

    catalog = _discover(root, user)

    assert catalog.get("helper").source == "user"
    assert catalog.names() == [BUILTIN_AGENT, BUILTIN_FORK, "helper"]
    assert [item.name for item in catalog.custom()] == ["helper"]


# --------------------------------------------------------------------------- #
# 2. 校验：静默跳过 vs 点名
# --------------------------------------------------------------------------- #
def test_files_without_declaration_are_skipped_silently(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    _write(root / ".blank/agents/plain.md", "只是一段说明，没有 frontmatter。")
    _write(root / ".blank/agents/noname.md", "---\ndescription: 没有名字\n---\n正文")
    _write(root / ".blank/agents/empty.md", "")

    catalog = _discover(root)

    assert catalog.custom() == []
    assert catalog.errors == []


@pytest.mark.parametrize(
    ("name_value", "needle"),
    [
        ('"-reviewer"', "不能以"),
        ('"a：b"', "不能包含"),
        ('"a:b"', "不能包含"),
        ("123", "必须是字符串"),
        ('"   "', "name 为空"),
    ],
)
def test_invalid_name_is_named_in_errors(tmp_path: Path, name_value: str, needle: str) -> None:
    root = tmp_path / "ws"
    _write(
        root / ".blank/agents/bad.md",
        f"---\nname: {name_value}\ndescription: 描述\n---\n正文",
    )

    catalog = _discover(root)

    assert catalog.custom() == []
    assert len(catalog.errors) == 1
    assert needle in catalog.errors[0], catalog.errors


def test_missing_or_empty_description_is_an_error(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    _write(root / ".blank/agents/a.md", "---\nname: a\n---\n正文")
    _write(root / ".blank/agents/b.md", '---\nname: b\ndescription: "  "\n---\n正文')

    catalog = _discover(root)

    assert catalog.custom() == []
    assert len(catalog.errors) == 2
    assert all("description" in item for item in catalog.errors), catalog.errors


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("---\nname: [unclosed\n---\n正文", "YAML"),
        ("---\nname: a\ndescription: d\n正文没有闭合", "没有闭合"),
        ("---\n- a\n- b\n---\n正文", "键值映射"),
    ],
)
def test_broken_frontmatter_never_loads_and_is_named(
    tmp_path: Path, text: str, needle: str
) -> None:
    root = tmp_path / "ws"
    _write(root / ".blank/agents/bad.md", text)

    catalog = _discover(root)

    assert catalog.custom() == []
    assert catalog.errors and needle in catalog.errors[0], catalog.errors


def test_oversized_file_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    body = "x" * (MAX_FILE_BYTES + 1)
    _write(root / ".blank/agents/big.md", _agent(body=body))

    catalog = _discover(root)

    assert catalog.custom() == []
    assert catalog.errors and str(MAX_FILE_BYTES) in catalog.errors[0]


def test_one_bad_file_does_not_take_down_the_catalog(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    _write(root / ".blank/agents/good.md", _agent(name="good"))
    _write(root / ".blank/agents/bad.md", "---\nname: bad\n---\n缺 description")

    catalog = _discover(root)

    assert catalog.get("good") is not None
    assert catalog.get("bad") is None
    assert len(catalog.errors) == 1


def test_long_instructions_are_clipped_with_a_marker(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    _write(root / ".blank/agents/long.md", _agent(body="y" * (MAX_INSTRUCTIONS_CHARS + 50)))

    catalog = _discover(root)

    definition = catalog.get("reviewer")
    assert definition is not None
    assert definition.instructions.endswith("…（定义正文过长，已截断）")
    assert len(definition.instructions) >= MAX_INSTRUCTIONS_CHARS


# --------------------------------------------------------------------------- #
# 3. tools / disallowedTools / maxTurns / 未支持字段
# --------------------------------------------------------------------------- #
def test_tools_accepts_comma_string_and_list(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    _write(
        root / ".blank/agents/one.md",
        _agent(name="one", extra="tools: read_file, search_code\n"),
    )
    _write(
        root / ".blank/agents/two.md",
        _agent(name="two", extra="tools:\n  - read_file\n  - search_code\n"),
    )

    catalog = _discover(root)

    assert catalog.get("one").tools == ("read_file", "search_code")
    assert catalog.get("two").tools == ("read_file", "search_code")


def test_tools_star_means_unrestricted(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    _write(root / ".blank/agents/all.md", _agent(extra='tools: "*"\n'))

    catalog = _discover(root)

    assert catalog.get("reviewer").tools is None


def test_invalid_tools_type_fails_closed_to_no_tools(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    _write(root / ".blank/agents/bad.md", _agent(extra="tools: 42\n"))

    catalog = _discover(root)

    definition = catalog.get("reviewer")
    # 类型不认识时不能解释成「不限」：那会让一个坏字段变成最大授权。
    assert definition.tools == ()
    assert any("tools" in item for item in catalog.errors), catalog.errors


def test_non_string_tool_item_is_ignored_with_warning(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    _write(
        root / ".blank/agents/mixed.md",
        _agent(extra="tools:\n  - read_file\n  - 3\n"),
    )

    catalog = _discover(root)

    assert catalog.get("reviewer").tools == ("read_file",)
    assert any("非字符串项" in item for item in catalog.errors), catalog.errors


def test_tool_specs_split_on_spaces_and_keep_parentheses(tmp_path: Path) -> None:
    """上游 ``VW`` 复用 ``Wd``：空格与逗号都切，括号内保持一条。"""

    root = tmp_path / "ws"
    _write(
        root / ".blank/agents/spec.md",
        _agent(extra="tools: 'read_file Bash(git add, git commit)'\n"),
    )

    catalog = _discover(root)

    assert catalog.get("reviewer").tools == (
        "read_file",
        "Bash(git add, git commit)",
    )


def test_empty_tools_line_means_no_tools(tmp_path: Path) -> None:
    """显式 ``tools:``（YAML null）在上游是空表，不是「不限」。"""

    root = tmp_path / "ws"
    _write(root / ".blank/agents/empty.md", _agent(name="empty", extra="tools:\n"))
    _write(root / ".blank/agents/absent.md", _agent(name="absent"))

    catalog = _discover(root)

    assert catalog.get("empty").tools == ()
    assert catalog.get("absent").tools is None


def test_wildcard_inside_a_tool_list_collapses(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    _write(
        root / ".blank/agents/mixed.md",
        _agent(extra="tools:\n  - read_file\n  - '*'\n"),
    )

    catalog = _discover(root)

    assert catalog.get("reviewer").tools is None


def test_falsy_tools_value_means_no_tools(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    _write(root / ".blank/agents/off.md", _agent(extra="tools: false\n"))

    catalog = _discover(root)

    assert catalog.get("reviewer").tools == ()
    assert not any("tools" in item for item in catalog.errors), catalog.errors


def test_skills_accepts_comma_string_and_list(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    _write(root / ".blank/agents/one.md", _agent(name="one", extra="skills: deploy, review\n"))
    _write(
        root / ".blank/agents/two.md",
        _agent(name="two", extra="skills:\n  - deploy\n  - review\n"),
    )

    catalog = _discover(root)

    assert catalog.get("one").skills == ("deploy", "review")
    assert catalog.get("two").skills == ("deploy", "review")


def test_skills_absent_and_null_are_both_empty(tmp_path: Path) -> None:
    """上游 ``OI``：``e == null ? [] : fyn(e)``，缺省键与显式 null 同义。"""

    root = tmp_path / "ws"
    _write(root / ".blank/agents/absent.md", _agent(name="absent"))
    _write(root / ".blank/agents/nil.md", _agent(name="nil", extra="skills:\n"))

    catalog = _discover(root)

    assert catalog.get("absent").skills == ()
    assert catalog.get("nil").skills == ()
    assert not any("skills" in item for item in catalog.errors), catalog.errors


def test_invalid_skills_type_fails_closed_with_a_skill_worded_warning(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ws"
    _write(root / ".blank/agents/bad.md", _agent(name="bad", extra="skills: 42\n"))
    _write(
        root / ".blank/agents/mixed.md",
        _agent(name="mixed", extra="skills:\n  - deploy\n  - 3\n"),
    )

    catalog = _discover(root)

    assert catalog.get("bad").skills == ()
    assert catalog.get("mixed").skills == ("deploy",)
    assert any("skills" in item and "没有 Skill" in item for item in catalog.errors), catalog.errors
    assert any("非字符串项" in item for item in catalog.errors), catalog.errors


def test_skills_star_is_a_literal_name_not_a_wildcard(tmp_path: Path) -> None:
    """``tools`` 的 ``*`` 是不限；``skills`` 的 ``*`` 只是一个解析不到的名字。"""

    root = tmp_path / "ws"
    _write(root / ".blank/agents/all.md", _agent(extra='skills: "*"\n'))

    catalog = _discover(root)

    assert catalog.get("reviewer").skills == ("*",)


def test_skills_field_is_audited_not_reported_as_unsupported(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    _write(root / ".blank/agents/reviewer.md", _agent(extra="skills: deploy\n"))

    definition = _discover(root).get("reviewer")

    assert "skills" not in UNSUPPORTED_FIELDS
    assert definition.ignored == ()
    assert definition.as_dict()["skills"] == ["deploy"]


def test_disallowed_tools_star_means_no_restriction(tmp_path: Path) -> None:
    """上游 ``VW`` 把 ``*`` 变成 ``undefined``：禁止列表整条被省略。"""

    root = tmp_path / "ws"
    _write(root / ".blank/agents/deny.md", _agent(extra='disallowedTools: "*"\n'))
    _write(
        root / ".blank/agents/mixed.md",
        _agent(name="mixed", extra="disallowedTools:\n  - run_shell\n  - '*'\n"),
    )

    catalog = _discover(root)

    assert catalog.get("reviewer").disallowed_tools == ()
    assert catalog.get("mixed").disallowed_tools == ()
    assert "disallowedTools" not in catalog.get("reviewer").as_dict()


def test_max_turns_must_be_positive_integer(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    _write(root / ".blank/agents/ok.md", _agent(name="ok", extra="maxTurns: 6\n"))
    _write(root / ".blank/agents/zero.md", _agent(name="zero", extra="maxTurns: 0\n"))
    _write(root / ".blank/agents/bool.md", _agent(name="bool", extra="maxTurns: true\n"))

    catalog = _discover(root)

    assert catalog.get("ok").max_turns == 6
    assert catalog.get("zero").max_turns is None
    assert catalog.get("bool").max_turns is None
    assert sum("maxTurns" in item for item in catalog.errors) == 2


def test_unsupported_fields_are_reported_not_applied(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    extra = "".join(f"{key}: 1\n" for key in UNSUPPORTED_FIELDS)
    path = _write(root / ".blank/agents/full.md", _agent(extra=extra))

    definition, problems = load_definition(path)

    assert problems == []
    assert set(definition.ignored) == set(UNSUPPORTED_FIELDS)
    audit = definition.as_dict()
    assert set(audit["ignored"]) == set(UNSUPPORTED_FIELDS)
    # 审计视图只指认文件与字数，不抄正文。
    assert "先读再判" not in json.dumps(audit, ensure_ascii=False)
    assert str(tmp_path) not in audit["path"]


# --------------------------------------------------------------------------- #
# 4. 工具池只收窄
# --------------------------------------------------------------------------- #
def test_child_tools_narrow_but_never_widen() -> None:
    default = SubAgentRequest(goal="g")
    assert _child_allowed_tools(default) == set(READ_ONLY_TOOLS)

    isolated = SubAgentRequest(goal="g", isolate=True)
    assert _child_allowed_tools(isolated) == set(ISOLATED_TOOLS)

    narrowed = SubAgentRequest(goal="g", allowed_tools=("read_file", "run_shell"))
    # 非隔离派发里写工具不会因为定义里列了就出现。
    assert _child_allowed_tools(narrowed) == {"read_file"}

    widened = SubAgentRequest(goal="g", isolate=True, allowed_tools=("read_file", "run_shell"))
    assert _child_allowed_tools(widened) == {"read_file", "run_shell"}

    denied = SubAgentRequest(goal="g", disallowed_tools=("read_file",))
    assert "read_file" not in _child_allowed_tools(denied)

    all_denied = SubAgentRequest(goal="g", disallowed_tools=("*",))
    assert _child_allowed_tools(all_denied) == set()

    # 直接传 "*" 白名单时按集合交集处理，结果是什么工具都没有（fail-closed）。
    star = SubAgentRequest(goal="g", allowed_tools=("*",))
    assert _child_allowed_tools(star) == set()


# --------------------------------------------------------------------------- #
# 5. spawn_agent / spawn_agents 解析类型
# --------------------------------------------------------------------------- #
async def test_spawn_agent_rejects_unknown_type_and_lists_available(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    _write(root / ".blank/agents/reviewer.md", _agent())
    catalog = _discover(root)
    runner = RecordingRunner()
    tool = _tool_map()["spawn_agent"]

    outcome = await tool.handler(
        _context(root, runner, catalog),
        {"goal": "看看", "subagent_type": "nope", "run_in_background": False},
    )

    assert outcome.ok is False
    assert "未知子代理类型" in outcome.message
    assert "reviewer" in outcome.message
    assert runner.requests == []


async def test_without_catalog_only_builtins_are_known(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    runner = RecordingRunner()
    tool = _tool_map()["spawn_agent"]

    outcome = await tool.handler(
        _context(root, runner),
        {"goal": "看看", "subagent_type": "reviewer", "run_in_background": False},
    )

    assert outcome.ok is False
    assert outcome.message == (
        "未知子代理类型 'reviewer'。可用类型：" + f"{BUILTIN_AGENT}, {BUILTIN_FORK}"
    )

    default = await tool.handler(
        _context(root, runner), {"goal": "默认类型", "run_in_background": False}
    )
    assert default.ok is True
    assert runner.requests[-1].agent_name == BUILTIN_AGENT
    assert runner.requests[-1].instructions == ""


async def test_spawn_agent_max_turns_sets_step_budget(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    _write(root / ".blank/agents/six.md", _agent(name="six", extra="maxTurns: 6\n"))
    _write(root / ".blank/agents/huge.md", _agent(name="huge", extra="maxTurns: 99\n"))
    catalog = _discover(root)
    runner = RecordingRunner()
    tool = _tool_map()["spawn_agent"]

    await tool.handler(
        _context(root, runner, catalog),
        {"goal": "六步", "subagent_type": "six", "run_in_background": False},
    )
    await tool.handler(
        _context(root, runner, catalog),
        {"goal": "超上限", "subagent_type": "huge", "run_in_background": False},
    )
    await tool.handler(
        _context(root, runner, catalog),
        {"goal": "显式步数", "subagent_type": "six", "max_steps": 2, "run_in_background": False},
    )

    assert [item.max_steps for item in runner.requests] == [6, 8, 2]


async def test_spawn_agents_resolves_type_per_task_and_rejects_unknown_batch(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ws"
    _write(root / ".blank/agents/reviewer.md", _agent())
    catalog = _discover(root)
    runner = RecordingRunner()
    tool = _tool_map()["spawn_agents"]

    outcome = await tool.handler(
        _context(root, runner, catalog),
        {
            "tasks": [
                {"goal": "审查", "subagent_type": "reviewer"},
                {"goal": "默认"},
            ]
        },
    )
    assert outcome.ok is True, outcome.message
    assert [item.agent_name for item in runner.requests] == ["reviewer", BUILTIN_AGENT]
    assert runner.requests[0].instructions == "先读再判。"

    runner.requests.clear()
    rejected = await tool.handler(
        _context(root, runner, catalog),
        {"tasks": [{"goal": "好任务"}, {"goal": "坏任务", "subagent_type": "nope"}]},
    )
    # 一个未知类型整批拒绝：不能先跑一半再回来说有个名字不认识。
    assert rejected.ok is False
    assert "未知子代理类型" in rejected.message
    assert runner.requests == []


# --------------------------------------------------------------------------- #
# 6. 真跑：正文进子代理系统提示、工具表收窄、事件留痕、续聊不退回默认
# --------------------------------------------------------------------------- #
async def test_definition_body_and_tool_narrowing_reach_the_real_child(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    _write(
        root / ".blank/agents/reviewer.md",
        _agent(
            body="先读再判。",
            extra="tools: read_file\nmodel: sonnet\n",
        ),
    )
    catalog = _discover(root)
    adapter = CaptureAdapter("子代理结论")
    session = _session(root, adapter, task_id="task-v44")
    runner = SubAgentRunner().bind(session)
    tool = _tool_map()["spawn_agent"]
    try:
        outcome = await tool.handler(
            _context(root, runner, catalog),
            {"goal": "审查入口", "subagent_type": "reviewer", "run_in_background": False},
        )
    finally:
        await runner.aclose("task-v44")

    assert outcome.ok is True, outcome.message
    assert outcome.data.get("agent_type") == "reviewer"

    assert adapter.calls, "子代理必须真的调用模型"
    prompt = _messages_text(adapter.calls[0])
    assert "以下是子代理类型「reviewer」的专用说明" in prompt
    assert "先读再判。" in prompt
    # 定义里的 tools 只收窄：子代理只拿到 read_file。
    assert set(adapter.specs[0]) == {"read_file"}

    started = [payload for event, payload in session.recorder.events if event == "subagent.started"]
    assert started, session.recorder.types()
    assert started[0]["agent_type"] == "reviewer"
    assert started[0]["agent_tools"] == ["read_file"]
    assert started[0]["agent_ignored"] == ["model"]


def _skill_catalog(*entries: tuple[str, str]) -> SkillCatalog:
    return SkillCatalog(
        [
            {"name": name, "description": f"{name} 说明", "instructions": body}
            for name, body in entries
        ]
    )


async def test_declared_skills_are_preloaded_before_the_goal(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    _write(root / ".blank/agents/reviewer.md", _agent(extra="skills: deploy\n"))
    catalog = _discover(root)
    adapter = CaptureAdapter("子代理结论")
    session = _session(root, adapter, task_id="task-x1")
    session.context.skills = _skill_catalog(("deploy", "部署步骤：先备份数据库。"))
    runner = SubAgentRunner().bind(session)
    tool = _tool_map()["spawn_agent"]
    try:
        outcome = await tool.handler(
            _context(root, runner, catalog),
            {"goal": "部署预发", "subagent_type": "reviewer", "run_in_background": False},
        )
    finally:
        await runner.aclose("task-x1")

    assert outcome.ok is True, outcome.message
    contents = _contents(adapter.calls[0])
    header = skill_loading_header("deploy")
    preload = next(index for index, text in enumerate(contents) if text.startswith(header))
    goal = next(index for index, text in enumerate(contents) if "部署预发" in text)
    # 上游顺序：system → 预加载 meta 消息 → 用户目标。
    assert contents[0] != contents[preload]
    assert preload == 1, contents[:3]
    assert preload < goal
    assert "部署步骤：先备份数据库。" in contents[preload]
    # 目录是拷贝：子代理加载过的 Skill 不能让父会话以为自己也见过了。
    assert session.context.skills.loaded == set()
    assert [payload["skill"] for event, payload in session.recorder.events if event == "skill.preloaded"] == ["deploy"]
    started = [payload for event, payload in session.recorder.events if event == "subagent.started"]
    assert started[0]["agent_skills"] == ["deploy"]


async def test_missing_declared_skill_warns_but_still_spawns(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    _write(root / ".blank/agents/reviewer.md", _agent(extra="skills: nope\n"))
    catalog = _discover(root)
    adapter = CaptureAdapter("子代理结论")
    session = _session(root, adapter, task_id="task-x2")
    session.context.skills = _skill_catalog(("deploy", "部署步骤。"))
    runner = SubAgentRunner().bind(session)
    tool = _tool_map()["spawn_agent"]
    try:
        outcome = await tool.handler(
            _context(root, runner, catalog),
            {"goal": "看看", "subagent_type": "reviewer", "run_in_background": False},
        )
    finally:
        await runner.aclose("task-x2")

    # 缺名不阻断派发：上游同样只记一行 warning。
    assert outcome.ok is True, outcome.message
    failed = [payload for event, payload in session.recorder.events if event == "skill.preload.failed"]
    assert failed and failed[0]["skill"] == "nope"
    assert "was not found" in failed[0]["error"]
    assert "was not found" not in _messages_text(adapter.calls[0])
    assert [payload["skill"] for event, payload in session.recorder.events if event == "skill.preloaded"] == []


async def test_continuation_does_not_repeat_a_carried_preload(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    _write(root / ".blank/agents/reviewer.md", _agent(extra="skills: deploy\n"))
    catalog = _discover(root)
    adapter = CaptureAdapter("子代理结论")
    session = _session(root, adapter, task_id="task-x3")
    session.context.skills = _skill_catalog(("deploy", "部署步骤：先备份数据库。"))
    runner = SubAgentRunner().bind(session)
    tool = _tool_map()["spawn_agent"]
    try:
        outcome = await tool.handler(
            _context(root, runner, catalog),
            {"goal": "第一轮", "subagent_type": "reviewer", "run_in_background": False},
        )
        assert outcome.ok is True, outcome.message
        second = await runner.send_message(outcome.data["agent_id"], "第二轮")
    finally:
        await runner.aclose("task-x3")

    assert second.ok is True, second.error
    header = skill_loading_header("deploy")
    assert _messages_text(adapter.calls[0]).count(header) == 1
    # 上一轮已经把正文带进历史：续聊再贴一遍就是重复占上下文（上游 kDs）。
    assert _messages_text(adapter.calls[1]).count(header) == 1
    events = [payload for event, payload in session.recorder.events if event == "skill.preloaded"]
    assert len(events) == 1


async def test_preloaded_skill_counts_as_loaded_for_the_child(tmp_path: Path) -> None:
    catalog = _skill_catalog(("deploy", "部署步骤。"))
    loaded, problems = preload_skills(
        catalog, ("deploy",), project_dir=str(tmp_path), session_id="task-x4"
    )

    assert problems == []
    assert loaded and loaded[0][0] == "deploy"
    assert catalog.loaded == {"deploy"}
    outcome = await _skill(
        SimpleNamespace(skills=catalog, root=str(tmp_path), task_id="task-x4"),
        {"skill": "deploy"},
    )
    assert outcome.ok is True
    assert outcome.data.get("already_loaded") is True


async def test_continuation_keeps_definition_and_tool_boundary(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    _write(
        root / ".blank/agents/reviewer.md",
        _agent(body="先读再判。", extra="tools: read_file\n"),
    )
    catalog = _discover(root)
    adapter = CaptureAdapter("第一轮")
    session = _session(root, adapter, task_id="task-v44")
    runner = SubAgentRunner().bind(session)
    try:
        definition = catalog.get("reviewer")
        request = SubAgentRequest(
            goal="第一轮审查",
            agent_name=definition.name,
            instructions=definition.instructions,
            allowed_tools=definition.tools,
            disallowed_tools=definition.disallowed_tools,
        )
        first = await runner(request, agent_id="agent-v44")
        assert first.ok is True
        second = await runner.send_message("agent-v44", "接着看第二个入口")
    finally:
        await runner.aclose("task-v44")

    assert second.ok is True, second.error
    assert len(adapter.calls) == 2
    # 续聊是同一个子代理：第二轮仍带着定义正文，工具表也没有退回默认。
    second_prompt = _messages_text(adapter.calls[1])
    assert "以下是子代理类型「reviewer」的专用说明" in second_prompt
    assert "先读再判。" in second_prompt
    assert set(adapter.specs[1]) == {"read_file"}


# --------------------------------------------------------------------------- #
# 7. 系统提示清单
# --------------------------------------------------------------------------- #
def test_initial_messages_lists_custom_agent_types_only() -> None:
    messages = initial_messages(
        "任务",
        [],
        agent_catalog=[
            {"name": "reviewer", "description": "只读审查", "source": "project"},
            {"name": "architect", "description": "出方案", "source": "user"},
            {"name": BUILTIN_AGENT, "description": "通用", "source": "built-in"},
        ],
    )

    system = messages[0]["content"]
    assert "可用子代理类型" in system
    assert "- reviewer：只读审查" in system
    assert "- architect：出方案" in system
    # 只有内置类型时不值得再列一行：工具说明已经写了默认值。
    assert f"- {BUILTIN_AGENT}：通用" not in system


def test_initial_messages_without_custom_types_has_no_agent_block() -> None:
    messages = initial_messages(
        "任务",
        [],
        agent_catalog=[{"name": BUILTIN_AGENT, "description": "通用", "source": "built-in"}],
    )

    assert "可用子代理类型" not in messages[0]["content"]


# --------------------------------------------------------------------------- #
# 8. 运行时接线：发现、事件、提示、工具全链路
# --------------------------------------------------------------------------- #
async def test_runtime_discovers_agents_lists_them_and_spawns_by_type(
    env, workspace: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BLANK_USER_AGENTS_DIR", str(tmp_path / "empty-user-agents"))
    _write(
        workspace / ".blank/agents/reviewer.md",
        _agent(body="先读再判。", extra="tools: read_file\nmaxTurns: 2\n"),
    )
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["审查完成"])
    env.mock.reset(
        [
            {
                "content": "派一个审查员",
                "tool_calls": [
                    call(
                        "spawn_agent",
                        {
                            "goal": "审查 main.py",
                            "subagent_type": "reviewer",
                            "run_in_background": False,
                        },
                    )
                ],
            },
            {"content": "审查完成：没有发现问题"},
            {"content": "任务完成"},
        ]
    )

    evidence = await AgentRuntime(get_session_factory()).run(
        RunRequest(
            task_id=task_id,
            provider_id=provider_id,
            provider_name="mock-vendor",
            adapter=build_adapter(await _provider_row(provider_id), env.mock.factory),
            root=workspace,
            goal="按项目自定义类型做一次审查",
            acceptance_criteria=["审查完成"],
            max_steps=5,
        )
    )

    assert evidence["task"]["status"] == "succeeded", evidence["task"]

    loaded = [item for item in evidence["timeline"] if item["type"] == "agents.loaded"]
    assert loaded, [item["type"] for item in evidence["timeline"]]
    assert loaded[0]["payload"]["count"] == 3
    names = {item["name"] for item in loaded[0]["payload"]["agents"]}
    assert {"reviewer", BUILTIN_AGENT, BUILTIN_FORK} <= names

    # 主代理第一问的系统提示里必须有清单；没有它，模型不知道能按名字派。
    parent_prompt = _messages_text(env.mock.requests[0]["messages"])
    assert "可用子代理类型" in parent_prompt
    assert "reviewer" in parent_prompt

    # 子代理那一问（第 2 次模型调用）必须拿到定义正文。
    child_prompt = _messages_text(env.mock.requests[1]["messages"])
    assert "以下是子代理类型「reviewer」的专用说明" in child_prompt
    assert "先读再判。" in child_prompt

    started = [item for item in evidence["timeline"] if item["type"] == "subagent.started"]
    assert started, [item["type"] for item in evidence["timeline"]]
    assert started[0]["payload"]["agent_type"] == "reviewer"
    assert started[0]["payload"]["agent_tools"] == ["read_file"]
    # maxTurns=2 决定子代理的步数上限，而不是工具层默认的 4。
    assert started[0]["payload"]["max_steps"] == 2

    completed = [
        item
        for item in evidence["timeline"]
        if item["type"] == "tool.completed" and item["payload"].get("tool") == "spawn_agent"
    ]
    assert completed and completed[0]["payload"]["ok"] is True


async def _provider_row(provider_id: str):
    from blank_api.models.model_provider import ModelProvider

    async with get_session_factory()() as session:
        return await session.get(ModelProvider, provider_id)
