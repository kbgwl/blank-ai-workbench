"""S2：磁盘 Skill 定义（.blank/skills/<name>/SKILL.md）。

对齐 Claude Code 的 ``.claude/skills`` 契约，并钉住我们自己的边界口径：

1. **两层发现**：用户级 skills 目录 + 项目 ``.blank/skills``；同名项目胜，
   调用名就是目录名（Claude 的 ``/commit`` 来自 ``commit/SKILL.md``）；
2. **坏 frontmatter 降级而不是跳过**：Claude 在 YAML 坏掉时丢掉全部字段但仍加载
   文件——名字回落到目录名、说明回落到正文第一行；问题必须进告警；
3. **跳过口径**：没有 SKILL.md、非文件、超 1 MiB、保留目录 ``synced`` 都不加载，
   单个坏文件不拖垮其余 Skill；
4. **disable-model-invocation**：不进模型清单、``skill`` 工具拒绝调用；
   ``user-invocable``、``allowed-tools`` / ``disallowed-tools`` 只解析留审计；
5. **合并顺序**：库 → 用户级 → 项目级；不可调用的同名项不能盖掉可调用的那份；
6. **真的接通**：运行时发现目录、清单进系统提示、``skills.loaded`` 事件、
   ``skill`` 工具加载正文、只有用户可调用时工具从工具表里拿掉。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from blank_api.db import get_session_factory
from blank_api.services.agent.graph import initial_messages
from blank_api.services.agent.runtime import AgentRuntime, RunRequest
from blank_api.services.models.registry import build_adapter
from blank_api.services.skills_defs import (
    MAX_BODY_CHARS,
    MAX_FILE_BYTES,
    _skill_file,
    discover_skills,
    load_skill,
    merge_skill_entries,
)
from blank_api.services.subagent import SubAgentRequest, SubAgentRunner
from blank_api.services.tools.skills import SkillCatalog, skill_tools

from tests.test_agent_defs_v44 import CaptureAdapter
from tests.test_agent_v04 import (  # noqa: F401  (env 是夹具，故意导入)
    call,
    create_provider,
    create_project,
    create_task,
    env,
)
from tests.test_background_subagent_v116 import FakeRecorder, _session


# --------------------------------------------------------------------------- #
# 帮助：写 Skill、发现、跑工具、拼提示
# --------------------------------------------------------------------------- #
def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _skill(
    name: str = "deploy",
    description: str = "上线流程",
    body: str = "先跑 pytest。",
    extra: str = "",
) -> str:
    return f"---\nname: {name}\ndescription: {description}\n{extra}---\n{body}"


def _write_skill(root: Path, dir_name: str, text: str) -> Path:
    return _write(root / ".blank" / "skills" / dir_name / "SKILL.md", text)


def _discover(root: Path, user_dir: Path | None = None):
    env_map = {"BLANK_USER_SKILLS_DIR": str(user_dir or (root.parent / "no-user-skills"))}
    return discover_skills(root, env=env_map)


def _context(catalog) -> SimpleNamespace:
    return SimpleNamespace(
        root=Path("."),
        task_id="task-v45",
        skills=catalog,
        recorder=FakeRecorder(),
    )


def _tool():
    return next(item for item in skill_tools() if item.name == "skill")


def _messages_text(messages) -> str:
    parts = []
    for item in messages:
        if isinstance(item, dict):
            parts.append(str(item.get("content") or ""))
        else:
            parts.append(str(getattr(item, "content", "") or ""))
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# 1. 发现、层级与跳过口径
# --------------------------------------------------------------------------- #
def test_project_and_user_layers_load_and_project_wins(tmp_path: Path) -> None:
    user_dir = tmp_path / "user-skills"
    _write(user_dir / "deploy" / "SKILL.md", _skill(description="用户版上线"))
    _write(user_dir / "lint" / "SKILL.md", _skill(name="lint", description="用户 lint"))
    _write_skill(tmp_path, "deploy", _skill(description="项目版上线"))

    found = _discover(tmp_path, user_dir)

    assert sorted(found.entries) == ["deploy", "lint"]
    assert found.entries["deploy"].description == "项目版上线"
    assert found.entries["deploy"].source == "project"
    assert found.entries["lint"].source == "user"


def test_directory_without_skill_md_is_skipped_silently(tmp_path: Path) -> None:
    (tmp_path / ".blank" / "skills" / "empty").mkdir(parents=True)
    _write_skill(tmp_path, "real", _skill())

    found = _discover(tmp_path)

    assert list(found.entries) == ["real"]
    assert found.errors == []


class _CaseSensitiveDir:
    """大小写敏感文件系统的替身：精确名字永远找不到，只能走目录项扫描。

    Windows 上 ``dir / "SKILL.md"`` 对 ``Skill.MD`` 本身就会命中，直接建文件再断言
    是恒真判据（换掉大小写匹配逻辑也照样绿）。把"精确路径"挡掉、只留 iterdir，
    匹配规则才真的可失败——这正是 Linux/macOS 上要靠的兜底。
    """

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def __truediv__(self, name: str) -> Path:
        return self._directory / "__absent__" / name

    def iterdir(self):
        return self._directory.iterdir()


def test_skill_md_match_is_case_insensitive(tmp_path: Path) -> None:
    directory = tmp_path / ".blank" / "skills" / "deploy"
    _write(directory / "Skill.MD", _skill())

    stand_in = _CaseSensitiveDir(directory)
    # 装置自检：替身必须真的挡掉精确路径，否则又退回恒真判据。
    assert (stand_in / "Skill.MD").is_file() is False
    found = _skill_file(stand_in)  # type: ignore[arg-type]

    assert found is not None and found.name == "Skill.MD"


def test_reserved_synced_directory_is_skipped(tmp_path: Path) -> None:
    _write_skill(tmp_path, "synced", _skill())
    _write_skill(tmp_path, "real", _skill())

    found = _discover(tmp_path)

    assert "synced" not in found.entries
    assert "real" in found.entries
    assert any("保留目录名" in item for item in found.warnings)


def test_claude_native_directory_is_not_read(tmp_path: Path) -> None:
    """我们只读 .blank/skills；仓库里的 .claude/skills 不被顺带加载。"""
    _write(
        tmp_path / ".claude" / "skills" / "old" / "SKILL.md",
        _skill(name="old", description="Claude 原生目录"),
    )

    found = _discover(tmp_path)

    assert found.entries == {}


def test_oversized_file_is_skipped_but_others_load(tmp_path: Path) -> None:
    _write_skill(tmp_path, "huge", "x" * (MAX_FILE_BYTES + 1))
    _write_skill(tmp_path, "real", _skill())

    found = _discover(tmp_path)

    assert "huge" not in found.entries
    assert "real" in found.entries
    assert any(str(MAX_FILE_BYTES) in item for item in found.errors)


def test_missing_directory_yields_an_empty_discovery(tmp_path: Path) -> None:
    found = _discover(tmp_path)

    assert found.entries == {}
    assert found.errors == []
    assert found.warnings == []


# --------------------------------------------------------------------------- #
# 2. 名字、说明与坏 frontmatter 的降级
# --------------------------------------------------------------------------- #
def test_invocation_name_is_the_directory_name_and_frontmatter_is_display_only(
    tmp_path: Path,
) -> None:
    _write_skill(tmp_path, "deploy", _skill(name="deploy-v2"))

    found = _discover(tmp_path)
    item = found.entries["deploy"]

    assert item.name == "deploy"
    assert item.display_name == "deploy-v2"
    assert item.as_dict()["displayName"] == "deploy-v2"


def test_description_falls_back_to_the_first_body_line(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "verify",
        "---\nname: verify\n---\n\n  # 先跑全量回归\n第二行不参与说明",
    )

    found = _discover(tmp_path)

    assert found.entries["verify"].description == "先跑全量回归"


def test_description_is_clipped_to_one_line_of_constant_cost(tmp_path: Path) -> None:
    long_line = "很长的说明" * 100
    _write_skill(tmp_path, "verify", f"---\nname: verify\n---\n{long_line}")

    found = _discover(tmp_path)

    assert len(found.entries["verify"].description) == 120
    assert found.entries["verify"].description.endswith("…")


@pytest.mark.parametrize(
    ("text", "needle", "expected"),
    [
        ("---\nname: [\n---\n正文仍在", "不是合法 YAML", "正文仍在"),
        ("---\nname: broken\n正文仍在", "没有闭合", "name: broken"),
        ("---\n- just\n- a list\n---\n正文仍在", "必须是键值映射", "正文仍在"),
    ],
)
def test_broken_frontmatter_degrades_instead_of_skipping(
    tmp_path: Path, text: str, needle: str, expected: str
) -> None:
    """Claude 在 YAML 坏掉时仍加载，只是字段全丢；我们同样降级并告警。

    没有闭合分隔符时整段都是正文，所以说明回落到文件里的第一行
    （``name: broken``）；有闭合符时说明来自闭合符之后的第一行。
    """
    _write_skill(tmp_path, "fallback", text)

    found = _discover(tmp_path)
    item = found.entries["fallback"]

    assert item.name == "fallback"
    assert item.description == expected
    assert "正文仍在" in item.instructions
    assert any(needle in warning for warning in found.warnings)


def test_body_longer_than_the_cap_is_clipped_with_a_marker(tmp_path: Path) -> None:
    _write_skill(tmp_path, "huge-body", "---\nname: huge-body\n---\n" + "x" * (MAX_BODY_CHARS + 50))

    found = _discover(tmp_path)
    body = found.entries["huge-body"].instructions

    assert body.endswith("…（Skill 正文过长，已截断）")
    assert len(body) < MAX_BODY_CHARS + 100


def test_single_file_entry_point_reads_the_parent_directory(tmp_path: Path) -> None:
    path = _write_skill(tmp_path, "deploy", _skill())

    item, errors, warnings = load_skill(path)

    assert errors == []
    assert warnings == []
    assert item is not None and item.name == "deploy"


# --------------------------------------------------------------------------- #
# 3. 字段：disable-model-invocation / user-invocable / tools / 未支持字段
# --------------------------------------------------------------------------- #
def test_disable_model_invocation_keeps_it_out_of_the_model_catalogue(
    tmp_path: Path,
) -> None:
    _write_skill(
        tmp_path,
        "user-only",
        _skill(name="user-only", extra="disable-model-invocation: true\n"),
    )
    _write_skill(tmp_path, "deploy", _skill())

    found = _discover(tmp_path)
    catalog = SkillCatalog([item.as_entry() for item in found.entries.values()])

    assert set(catalog.names()) == {"user-only", "deploy"}
    assert catalog.model_names() == ["deploy"]
    assert [item["name"] for item in catalog.listing()] == ["deploy"]
    assert found.entries["user-only"].as_dict()["modelInvocable"] is False


async def test_skill_tool_rejects_a_disable_model_invocation_skill() -> None:
    catalog = SkillCatalog(
        [
            {
                "name": "user-only",
                "instructions": "只许用户自己跑",
                "model_invocable": False,
            }
        ]
    )
    context = _context(catalog)

    outcome = await _tool().handler(context, {"skill": "user-only"})

    assert outcome.ok is False
    assert "disable-model-invocation" in outcome.message
    assert "不要用别的方式复刻" in outcome.message
    assert catalog.loaded == set()
    assert not [item for item in context.recorder.events if item[0] == "skill.invoked"]


async def test_user_invocable_false_is_recorded_but_does_not_hide_it_from_the_model(
    tmp_path: Path,
) -> None:
    _write_skill(tmp_path, "internal", _skill(name="internal", extra="user-invocable: false\n"))

    found = _discover(tmp_path)
    item = found.entries["internal"]
    catalog = SkillCatalog([item.as_entry()])

    assert item.user_invocable is False
    assert item.model_invocable is True
    assert catalog.model_available() is True
    outcome = await _tool().handler(_context(catalog), {"skill": "internal"})
    assert outcome.ok is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [("true", False), ("false", True), ("no", True), ("yes", False)],
)
def test_disable_model_invocation_accepts_boolean_strings(
    tmp_path: Path, value: str, expected: bool
) -> None:
    _write_skill(
        tmp_path,
        "flag",
        _skill(name="flag", extra=f"disable-model-invocation: {value}\n"),
    )

    found = _discover(tmp_path)

    assert found.entries["flag"].model_invocable is expected


def test_tool_lists_are_parsed_and_kept_for_audit(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "deploy",
        _skill(
            extra="allowed-tools: read_file, search_code\ndisallowedTools: [run_shell]\n"
        ),
    )

    found = _discover(tmp_path)
    item = found.entries["deploy"]

    assert item.allowed_tools == ("read_file", "search_code")
    assert item.disallowed_tools == ("run_shell",)
    audit = item.as_dict()
    assert audit["allowedTools"] == ["read_file", "search_code"]
    assert audit["disallowedTools"] == ["run_shell"]


def test_allowed_tools_expand_skill_and_project_placeholders(tmp_path: Path) -> None:
    """上游只替换 allowed-tools 里的两个占位符，并把路径统一成正斜杠。"""

    _write_skill(
        tmp_path,
        "deploy",
        _skill(
            extra=(
                "allowed-tools: 'read_file, ${CLAUDE_SKILL_DIR}/scripts/run.py, "
                "${CLAUDE_PROJECT_DIR}/bin/tool'\n"
                "disallowedTools: ['${CLAUDE_SKILL_DIR}/x']\n"
            )
        ),
    )

    found = _discover(tmp_path)
    item = found.entries["deploy"]
    skill_dir = str(tmp_path / ".blank" / "skills" / "deploy").replace("\\", "/")
    project = str(tmp_path).replace("\\", "/")

    assert item.allowed_tools == (
        "read_file",
        f"{skill_dir}/scripts/run.py",
        f"{project}/bin/tool",
    )
    # disallowed-tools 不参与替换：上游只对 allowedTools 做这一步。
    assert item.disallowed_tools == ("${CLAUDE_SKILL_DIR}/x",)
    assert item.as_dict()["allowedTools"] == list(item.allowed_tools)


def test_user_layer_skills_resolve_project_dir_to_the_workspace(tmp_path: Path) -> None:
    """用户层的 Skill 也用当前工作区当 ``${CLAUDE_PROJECT_DIR}``，不是用户目录。"""

    user_dir = tmp_path / "user-skills"
    _write(
        user_dir / "lint" / "SKILL.md",
        _skill(name="lint", extra="allowed-tools: ['${CLAUDE_PROJECT_DIR}/lint.py']\n"),
    )

    found = _discover(tmp_path, user_dir)

    assert found.entries["lint"].allowed_tools == (
        f"{str(tmp_path).replace(chr(92), '/')}/lint.py",
    )


def test_tool_specs_split_on_spaces_outside_parentheses(tmp_path: Path) -> None:
    """上游 Wd：逗号和空格都切，括号里的空格/逗号保持完整。"""

    _write_skill(
        tmp_path,
        "deploy",
        _skill(extra="allowed-tools: 'read_file Bash(git add, git commit) search_code'\n"),
    )

    found = _discover(tmp_path)

    assert found.entries["deploy"].allowed_tools == (
        "read_file",
        "Bash(git add, git commit)",
        "search_code",
    )


def test_tool_list_drops_non_string_entries_instead_of_the_whole_field(
    tmp_path: Path,
) -> None:
    """上游 fyn 只过滤掉非字符串项，剩下的照常解析而不是整段作废。"""

    _write_skill(
        tmp_path,
        "deploy",
        _skill(extra="allowed-tools: ['read_file', 42, 'search_code']\n"),
    )

    found = _discover(tmp_path)

    assert found.entries["deploy"].allowed_tools == ("read_file", "search_code")
    assert any("非字符串" in item for item in found.warnings), found.warnings


def test_wildcard_tool_spec_collapses_the_list(tmp_path: Path) -> None:
    """上游 fyn：列表里出现 ``*`` 时整段归一成 ``["*"]``。"""

    _write_skill(
        tmp_path,
        "deploy",
        _skill(extra="disallowedTools: ['run_shell', '*']\n"),
    )

    found = _discover(tmp_path)

    assert found.entries["deploy"].disallowed_tools == ("*",)


def test_falsy_tool_list_is_empty_without_a_warning(tmp_path: Path) -> None:
    """``allowed-tools: false`` 在上游是「没有工具」，不是坏形状。"""

    _write_skill(tmp_path, "deploy", _skill(extra="allowed-tools: false\n"))

    found = _discover(tmp_path)

    assert found.entries["deploy"].allowed_tools == ()
    assert not any("allowed-tools" in item for item in found.warnings), found.warnings


def test_invalid_tool_shape_is_a_warning_not_a_crash(tmp_path: Path) -> None:
    _write_skill(tmp_path, "deploy", _skill(extra="allowed-tools: {a: 1}\n"))

    found = _discover(tmp_path)

    assert found.entries["deploy"].allowed_tools == ()
    assert any("allowed-tools" in item for item in found.warnings)


def test_unsupported_fields_are_reported_not_applied(tmp_path: Path) -> None:
    # paths 已不再是"只看不用"的字段（AA 批起按条件激活生效），effort 也已生效
    # （加载正文时展开 ${CLAUDE_EFFORT}），这里只钉余下仍不生效的。
    _write_skill(
        tmp_path,
        "deploy",
        _skill(extra="model: sonnet\ncontext: fork\n"),
    )

    found = _discover(tmp_path)
    item = found.entries["deploy"]

    assert set(item.ignored) == {"model", "context"}
    audit = item.as_dict()
    assert "sonnet" not in str(audit)
    assert audit["ignored"] == ["model", "context"]


# --------------------------------------------------------------------------- #
# 4. 合并顺序
# --------------------------------------------------------------------------- #
def test_merge_lets_later_layers_win_but_never_hides_an_invocable_skill() -> None:
    db = [{"name": "deploy", "instructions": "库版", "description": "库"}]
    user = [{"name": "deploy", "instructions": "用户版", "description": "用户"}]
    project = [
        {
            "name": "deploy",
            "instructions": "项目版",
            "description": "项目",
            "model_invocable": False,
        }
    ]

    merged = merge_skill_entries(db, user, project)

    # 项目里那一份只许用户调用，不能把库里可调用的同名项盖掉。
    assert merged == [{**user[0]}]


def test_merge_keeps_plain_later_wins_when_both_are_invocable() -> None:
    db = [{"name": "deploy", "instructions": "库版", "description": "库"}]
    project = [{"name": "deploy", "instructions": "项目版", "description": "项目"}]

    merged = merge_skill_entries(db, project)

    assert merged[0]["instructions"] == "项目版"
    assert merged[0]["description"] == "项目"


def test_catalog_defaults_db_entries_to_model_invocable() -> None:
    catalog = SkillCatalog([{"name": "deploy", "instructions": "库版"}])

    assert catalog.model_available() is True
    assert [item["name"] for item in catalog.listing()] == ["deploy"]


# --------------------------------------------------------------------------- #
# 5. 系统提示与运行时接通
# --------------------------------------------------------------------------- #
def test_system_prompt_lists_only_model_invocable_skills() -> None:
    content = initial_messages(
        "上线",
        [],
        skill_catalog=[{"name": "deploy", "description": "上线步骤"}],
    )[0]["content"]

    assert "可用 Skill" in content
    assert "- deploy：上线步骤" in content


async def test_runtime_discovers_disk_skills_lists_and_loads_them(
    env, workspace: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BLANK_USER_SKILLS_DIR", str(tmp_path / "empty-user-skills"))
    _write_skill(
        workspace,
        "verify",
        "---\nname: verify\ndescription: 项目验证流程\n---\n先跑全量回归，再看失败用例。",
    )
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["验证完成"])
    env.mock.reset(
        [
            {"content": "先加载验证 Skill", "tool_calls": [call("skill", {"skill": "verify"})]},
            {"content": "已按验证流程执行"},
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
            goal="按项目的验证流程检查一遍",
            acceptance_criteria=["验证完成"],
            max_steps=5,
        )
    )

    assert evidence["task"]["status"] == "succeeded", evidence["task"]

    loaded = [item for item in evidence["timeline"] if item["type"] == "skills.loaded"]
    assert loaded, [item["type"] for item in evidence["timeline"]]
    assert loaded[0]["payload"]["count"] == 1
    assert loaded[0]["payload"]["skills"][0]["name"] == "verify"
    assert loaded[0]["payload"]["skills"][0]["description"] == "项目验证流程"

    # 运行时先可能跑一次意图判定；主代理那一问以"工具表里有 skill"为标志。
    main_index = next(
        index
        for index, request in enumerate(env.mock.requests)
        if "skill" in {item["function"]["name"] for item in request.get("tools", [])}
    )
    parent_prompt = _messages_text(env.mock.requests[main_index]["messages"])
    assert "可用 Skill" in parent_prompt
    assert "- verify：项目验证流程" in parent_prompt
    # 正文只在 skill 工具里加载，不能提前进系统提示。
    assert "先跑全量回归" not in parent_prompt

    after_tool = "\n".join(
        _messages_text(request["messages"]) for request in env.mock.requests[main_index:]
    )
    assert "已加载 Skill「verify」" in after_tool
    assert "先跑全量回归，再看失败用例。" in after_tool

    completed = [
        item
        for item in evidence["timeline"]
        if item["type"] == "tool.completed" and item["payload"].get("tool") == "skill"
    ]
    assert completed and completed[0]["payload"]["ok"] is True


async def test_runtime_removes_the_skill_tool_when_only_user_only_skills_exist(
    env, workspace: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BLANK_USER_SKILLS_DIR", str(tmp_path / "empty-user-skills"))
    _write_skill(
        workspace,
        "user-only",
        "---\nname: user-only\ndescription: 只许用户跑\ndisable-model-invocation: true\n---\n别让模型看见。",
    )
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["完成"])
    env.mock.reset([{"content": "完成"}])

    await AgentRuntime(get_session_factory()).run(
        RunRequest(
            task_id=task_id,
            provider_id=provider_id,
            provider_name="mock-vendor",
            adapter=build_adapter(await _provider_row(provider_id), env.mock.factory),
            root=workspace,
            goal="随便做点事",
            acceptance_criteria=["完成"],
            max_steps=3,
        )
    )

    assert all(
        "skill" not in {item["function"]["name"] for item in request.get("tools", [])}
        for request in env.mock.requests
    )
    assert all(
        "user-only" not in _messages_text(request["messages"])
        for request in env.mock.requests
    )


# --------------------------------------------------------------------------- #
# 6. 子代理的工具表也要守同一条边界
# --------------------------------------------------------------------------- #
async def test_subagent_drops_the_skill_tool_when_none_is_model_invocable(
    workspace: Path,
) -> None:
    """父目录里只有 user-only Skill 时，子代理看不到加载入口。

    子代理拿的是父目录的拷贝；不把"模型不可调用"这一条带下去的话，
    子代理会拿到一个对谁都说"不可调用"的工具，白白多一轮试错。
    """
    adapter = CaptureAdapter("子代理结论")
    session = _session(workspace, adapter, task_id="task-v45")
    session.context = SimpleNamespace(
        root=workspace,
        task_id="task-v45",
        skills=SkillCatalog(
            [{"name": "user-only", "instructions": "只许用户跑", "model_invocable": False}]
        ),
    )
    runner = SubAgentRunner().bind(session)
    try:
        result = await runner(SubAgentRequest(goal="看看入口"))
    finally:
        await runner.aclose("task-v45")

    assert result.ok is True, result.error
    assert adapter.specs, "子代理必须真的调用模型"
    assert "skill" not in adapter.specs[0]
    assert "read_file" in adapter.specs[0]


async def test_subagent_keeps_the_skill_tool_when_a_model_invocable_one_exists(
    workspace: Path,
) -> None:
    adapter = CaptureAdapter("子代理结论")
    session = _session(workspace, adapter, task_id="task-v45")
    session.context = SimpleNamespace(
        root=workspace,
        task_id="task-v45",
        skills=SkillCatalog([{"name": "deploy", "instructions": "上线流程"}]),
    )
    runner = SubAgentRunner().bind(session)
    try:
        result = await runner(SubAgentRequest(goal="看看入口"))
    finally:
        await runner.aclose("task-v45")

    assert result.ok is True, result.error
    assert adapter.specs, "子代理必须真的调用模型"
    assert "skill" in adapter.specs[0]


async def _provider_row(provider_id: str):
    from blank_api.models.model_provider import ModelProvider

    async with get_session_factory()() as session:
        return await session.get(ModelProvider, provider_id)
