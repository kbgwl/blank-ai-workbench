"""S3：自定义斜杠命令（``.blank/commands/**/*.md``）。

对齐 Claude Code 的 ``.claude/commands`` 契约：

1. **命名**：嵌套文件 ``db/migrate.md`` → ``/db:migrate``；子目录里的
   ``SKILL.md`` 用目录名，根目录 ``SKILL.md`` 没有名字（跳过）；同一目录里
   既有 ``SKILL.md`` 又有别的 md 时只用 ``SKILL.md`` 并告警；
2. **发现**：用户层 + 项目层，同名项目胜；超 1 MiB 跳过；坏 frontmatter
   丢掉字段但保留正文（降级而不是跳过）；正文超 20,000 字截断；
3. **参数**：``$ARGUMENTS`` / ``$ARGUMENTS[n]`` / ``$n`` / ``$name``（按
   ``arguments`` 声明的次序，最长名字优先）/ ``\\$`` 转义；没有占位符时追加
   ``\\nARGUMENTS: <args>``；替换进去的值不会被二次展开；
4. **路径占位符**：``${CLAUDE_PROJECT_DIR}`` / ``${CLAUDE_SESSION_ID}`` /
   ``${CLAUDE_SKILL_DIR}``；
5. **两个入口**：用户输入 ``/name args`` 在创建任务/会话回合/插话时展开；
   模型经 ``skill`` 工具按名加载；``disable-model-invocation`` 只挡模型，
   ``user-invocable: false`` 只挡用户斜杠；
6. **可观察**：``commands.loaded``、``command.expanded`` 事件与
   ``GET /projects/{id}/commands`` 列表。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from blank_api.db import get_session_factory
from blank_api.models.task import Task
from blank_api.services.agent.runtime import AgentRuntime, RunRequest
from blank_api.services.commands_defs import (
    MAX_BODY_CHARS,
    MAX_FILE_BYTES,
    discover_commands,
    expand_user_input,
    split_slash,
    user_prompt_catalog,
)
from blank_api.services.models.registry import build_adapter
from blank_api.services.prompt_args import (
    argument_names,
    expand_arguments,
    render_prompt,
    split_args,
    substitute_paths,
)
from blank_api.services.tools.skills import SkillCatalog, skill_tools

from tests.test_agent_v04 import (  # noqa: F401  (env 等是夹具/工具，故意导入)
    API,
    call,
    create_project,
    create_provider,
    create_task,
    env,
)
from tests.test_background_subagent_v116 import FakeRecorder


# --------------------------------------------------------------------------- #
# 帮助
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _isolate_user_prompt_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """用户层目录指到空目录：本机 ``~/.blank`` 里的东西不能影响判据。"""
    empty = tmp_path / "_no-user-prompts"
    empty.mkdir()
    monkeypatch.setenv("BLANK_USER_COMMANDS_DIR", str(empty / "commands"))
    monkeypatch.setenv("BLANK_USER_SKILLS_DIR", str(empty / "skills"))
    yield


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _command(body: str = "按命令正文执行。", extra: str = "") -> str:
    return f"---\ndescription: 命令说明\n{extra}---\n{body}"


def _write_command(root: Path, relative: str, text: str) -> Path:
    return _write(root / ".blank" / "commands" / relative, text)


def _discover(root: Path, user_dir: Path | None = None):
    env_map = {"BLANK_USER_COMMANDS_DIR": str(user_dir or (root.parent / "no-user-commands"))}
    return discover_commands(root, env=env_map)


def _expand(root: Path, text: str, **kwargs):
    env_map = {"BLANK_USER_COMMANDS_DIR": str(root.parent / "no-user-commands")}
    return expand_user_input(text, root=root, env=env_map, **kwargs)


def _tool():
    return next(item for item in skill_tools() if item.name == "skill")


def _context(catalog) -> SimpleNamespace:
    return SimpleNamespace(
        root=Path("."),
        task_id="task-v46",
        skills=catalog,
        recorder=FakeRecorder(),
    )


def _messages_text(messages) -> str:
    parts = []
    for item in messages:
        if isinstance(item, dict):
            parts.append(str(item.get("content") or ""))
        else:
            parts.append(str(getattr(item, "content", "") or ""))
    return "\n".join(parts)


async def _provider_row(provider_id: str):
    from blank_api.models.model_provider import ModelProvider

    async with get_session_factory()() as session:
        return await session.get(ModelProvider, provider_id)


# --------------------------------------------------------------------------- #
# 1. 参数替换引擎（Claude 的 qW）
# --------------------------------------------------------------------------- #
def test_whole_arguments_and_positional_forms() -> None:
    text = "整串=$ARGUMENTS 第一=$0 第二=$1 括号=$ARGUMENTS[1]"
    assert expand_arguments(text, "alpha beta") == "整串=alpha beta 第一=alpha 第二=beta 括号=beta"


def test_out_of_range_forms_keep_the_literal_text() -> None:
    # 越界不是空串：Claude 保留原文；但越界不算"用过参数"（qW 的 F 仍为 false），
    # 所以没有别的占位符时仍会追加 `ARGUMENTS:`，与上游逐字节一致。
    assert expand_arguments("$5 与 $ARGUMENTS[3]", "only") == "$5 与 $ARGUMENTS[3]\nARGUMENTS: only"


def test_in_range_replacement_counts_as_used_and_skips_the_appendix() -> None:
    assert expand_arguments("$0", "only") == "only"


def test_named_arguments_use_declared_order_and_longest_name_wins() -> None:
    text = "$env -> $en"
    assert expand_arguments(text, "prod eu", names=("env", "en")) == "prod -> eu"


def test_named_argument_boundary_does_not_eat_a_longer_word() -> None:
    # `$en` 不能吃掉 `$envx` 的前缀：后面还跟着字母就不算占位符。
    assert expand_arguments("$en $envx", "a b", names=("en",)) == "a $envx"


def test_longest_name_wins_when_the_prefix_ends_on_a_non_word_char() -> None:
    # `$a` 与 `$a-b` 会在 `$a-b` 上正面相撞：只有先替换长名字才取得到第 2 个参数，
    # 反过来 `$a` 会把前缀吃掉、剩下 "-b"。
    assert expand_arguments("$a-b", "X Y", names=("a", "a-b")) == "Y"


def test_escaped_dollar_is_literal_and_backslash_is_consumed() -> None:
    assert expand_arguments("价格 \\$5，占位 $0", "x") == "价格 $5，占位 x"


def test_values_containing_placeholders_are_not_expanded_twice() -> None:
    assert expand_arguments("$ARGUMENTS", "$ARGUMENTS 原文") == "$ARGUMENTS 原文"
    # 更真实的一条：`$0` 取到的是第一个 shell 词 `$ARGUMENTS`，后一趟整串替换
    # 不能把它再展开成全部参数。
    assert expand_arguments("$0", "$ARGUMENTS 原文") == "$ARGUMENTS"


def test_missing_placeholder_appends_arguments_line() -> None:
    assert expand_arguments("按步骤做", "生产 环境") == "按步骤做\nARGUMENTS: 生产 环境"


def test_quoted_arguments_split_like_a_shell() -> None:
    assert split_args('one "two words" three') == ["one", "two words", "three"]
    # 引号不配对时退回按空白切，而不是抛错。
    assert split_args('one "two words') == ["one", '"two', "words"]


def test_no_arguments_leaves_every_placeholder_alone() -> None:
    assert render_prompt("$ARGUMENTS 与 $0", "") == "$ARGUMENTS 与 $0"
    assert render_prompt("$ARGUMENTS 与 $0", None) == "$ARGUMENTS 与 $0"


def test_argument_names_drop_blanks_and_pure_digits() -> None:
    assert argument_names(["env", " ", "2", 7]) == ("env",)
    assert argument_names("env region 3") == ("env", "region")
    assert argument_names(None) == ()


def test_path_placeholders_and_separators() -> None:
    text = "根=${CLAUDE_PROJECT_DIR} 会话=${CLAUDE_SESSION_ID} 目录=${CLAUDE_SKILL_DIR}"
    result = render_prompt(text, "", project_dir="C:\\work\\repo", session_id="s-1", skill_dir="C:\\work\\repo\\.blank\\commands")
    assert "根=C:/work/repo" in result
    assert "会话=s-1" in result
    assert "目录=C:/work/repo/.blank/commands" in result


def test_path_placeholders_stay_when_the_value_is_unknown() -> None:
    assert substitute_paths("${CLAUDE_SKILL_DIR}", project_dir="R") == "${CLAUDE_SKILL_DIR}"


# --------------------------------------------------------------------------- #
# 2. 发现与命名
# --------------------------------------------------------------------------- #
def test_nested_files_become_colon_joined_names(tmp_path: Path) -> None:
    _write_command(tmp_path, "db/migrate.md", _command())
    _write_command(tmp_path, "deploy.md", _command())

    found = _discover(tmp_path)

    assert sorted(found.entries) == ["db:migrate", "deploy"]
    assert found.entries["db:migrate"].name == "db:migrate"


def test_skill_md_names_its_directory_and_root_skill_md_is_skipped(tmp_path: Path) -> None:
    _write_command(tmp_path, "db/SKILL.md", _command(body="目录命令"))
    _write_command(tmp_path, "SKILL.md", _command(body="根目录没有名字"))

    found = _discover(tmp_path)

    assert list(found.entries) == ["db"]
    assert found.entries["db"].instructions == "目录命令"


def test_skill_md_match_is_case_insensitive(tmp_path: Path) -> None:
    # 单文件时名字本来就落到目录名，看不出大小写；同目录再放一个 md 才能观察到
    # 「小写 skill.md 仍然是那个盖住别人、并告警的文件」。
    _write_command(tmp_path, "release/skill.md", _command(body="小写也认"))
    _write_command(tmp_path, "release/extra.md", _command(body="应被盖住"))

    found = _discover(tmp_path)

    assert list(found.entries) == ["release"]
    assert found.entries["release"].instructions == "小写也认"
    assert any("markdown" in item for item in found.warnings), found.warnings


def test_directory_with_skill_md_ignores_other_markdown_and_warns(tmp_path: Path) -> None:
    _write_command(tmp_path, "db/migrate.md", _command(body="不该被用"))
    _write_command(tmp_path, "db/SKILL.md", _command(body="目录正文"))

    found = _discover(tmp_path)

    assert found.entries["db"].instructions == "目录正文"
    assert any("markdown" in item and "db" in item for item in found.warnings), found.warnings


def test_user_layer_loads_and_project_wins(tmp_path: Path) -> None:
    user_dir = tmp_path / "user-commands"
    _write(user_dir / "deploy.md", _command(body="用户版"))
    _write(user_dir / "lint.md", _command(body="用户 lint"))
    _write_command(tmp_path, "deploy.md", _command(body="项目版"))

    found = _discover(tmp_path, user_dir)

    assert found.entries["deploy"].instructions == "项目版"
    assert found.entries["deploy"].source == "project"
    assert found.entries["lint"].source == "user"


def test_oversized_command_is_skipped_but_others_still_load(tmp_path: Path) -> None:
    _write_command(tmp_path, "huge.md", "x" * (MAX_FILE_BYTES + 1))
    _write_command(tmp_path, "ok.md", _command())

    found = _discover(tmp_path)

    assert list(found.entries) == ["ok"]
    assert any(str(MAX_FILE_BYTES) in item for item in found.errors), found.errors


def test_broken_frontmatter_degrades_instead_of_skipping(tmp_path: Path) -> None:
    _write_command(tmp_path, "broken.md", "---\nname: [oops\n---\n第一行说明\n第二行")

    found = _discover(tmp_path)

    entry = found.entries["broken"]
    assert entry.description == "第一行说明"
    assert "第二行" in entry.instructions
    assert any("frontmatter" in item for item in found.warnings), found.warnings


def test_long_body_is_clipped(tmp_path: Path) -> None:
    _write_command(tmp_path, "long.md", "---\ndescription: 长\n---\n" + "a" * (MAX_BODY_CHARS + 50))

    entry = _discover(tmp_path).entries["long"]

    assert len(entry.instructions) <= MAX_BODY_CHARS + len("\n…（命令正文过长，已截断）")
    assert entry.instructions.endswith("…（命令正文过长，已截断）")


def test_frontmatter_knobs_are_parsed_and_audited(tmp_path: Path) -> None:
    _write_command(
        tmp_path,
        "deploy.md",
        "---\n"
        "description: 上线\n"
        "argument-hint: <环境> [版本]\n"
        "arguments: [env, version]\n"
        "allowed-tools: read_file, run_shell\n"
        "disallowed-tools:\n"
        "  - delete_file\n"
        "user-invocable: false\n"
        "---\n上线 $env\n",
    )

    entry = _discover(tmp_path).entries["deploy"]

    assert entry.argument_hint == "<环境> [版本]"
    assert entry.argument_names == ("env", "version")
    assert entry.allowed_tools == ("read_file", "run_shell")
    assert entry.disallowed_tools == ("delete_file",)
    assert entry.user_invocable is False
    assert entry.model_invocable is True

    audit = entry.as_dict()
    assert audit["argumentHint"] == "<环境> [版本]"
    assert audit["allowedTools"] == ["read_file", "run_shell"]
    assert audit["path"].startswith("<workspace:"), audit["path"]


def test_disable_model_invocation_only_hides_it_from_the_model(tmp_path: Path) -> None:
    _write_command(
        tmp_path,
        "secret.md",
        "---\ndescription: 只能人跑\ndisable-model-invocation: true\n---\n人跑的流程\n",
    )

    entry = _discover(tmp_path).entries["secret"]

    assert entry.model_invocable is False
    assert entry.user_invocable is True


# --------------------------------------------------------------------------- #
# 3. 用户侧展开
# --------------------------------------------------------------------------- #
def test_known_command_expands_with_arguments(tmp_path: Path) -> None:
    _write_command(tmp_path, "greet.md", _command(body="目标：$ARGUMENTS"))

    expanded, info = _expand(tmp_path, "/greet 世界")

    assert expanded == "目标：世界"
    assert info is not None
    assert info["name"] == "greet"
    assert info["kind"] == "command"
    assert info["args_chars"] == 2
    assert info["original_chars"] == len("/greet 世界")
    assert info["expanded_chars"] == len("目标：世界")


def test_command_forces_an_argument_list_through_named_placeholders(tmp_path: Path) -> None:
    _write_command(
        tmp_path,
        "ship.md",
        "---\narguments: [env, version]\n---\n把 $version 发到 $env\n",
    )

    expanded, _ = _expand(tmp_path, "/ship prod 1.2.0")

    assert expanded == "把 1.2.0 发到 prod"


def test_command_without_arguments_leaves_the_body_untouched(tmp_path: Path) -> None:
    _write_command(tmp_path, "status.md", _command(body="报告当前状态 $ARGUMENTS"))

    expanded, info = _expand(tmp_path, "/status")

    assert expanded == "报告当前状态 $ARGUMENTS"
    assert info is not None


def test_unknown_name_and_plain_text_are_returned_unchanged(tmp_path: Path) -> None:
    assert _expand(tmp_path, "/not-a-command x") == ("/not-a-command x", None)
    assert _expand(tmp_path, "普通消息 /deploy") == ("普通消息 /deploy", None)


def test_user_invocable_false_hides_the_slash_entry(tmp_path: Path) -> None:
    _write_command(tmp_path, "internal.md", "---\nuser-invocable: false\n---\n内部流程\n")

    assert _expand(tmp_path, "/internal") == ("/internal", None)


def test_command_beats_a_same_named_skill_for_user_input(tmp_path: Path) -> None:
    _write(tmp_path / ".blank" / "skills" / "deploy" / "SKILL.md", "---\ndescription: 技能版\n---\n技能正文\n")
    _write_command(tmp_path, "deploy.md", _command(body="命令正文"))

    catalog = user_prompt_catalog(tmp_path, env={"BLANK_USER_COMMANDS_DIR": str(tmp_path / "none"), "BLANK_USER_SKILLS_DIR": str(tmp_path / "none2")})
    assert catalog["deploy"]["kind"] == "command"
    assert catalog["deploy"]["instructions"] == "命令正文"

    expanded, info = _expand(tmp_path, "/deploy")
    assert expanded == "命令正文"
    assert info["kind"] == "command"


def test_project_and_session_placeholders_expand_in_the_body(tmp_path: Path) -> None:
    _write_command(tmp_path, "where.md", _command(body="根=${CLAUDE_PROJECT_DIR} 会话=${CLAUDE_SESSION_ID}"))

    expanded, _ = _expand(tmp_path, "/where", session_id="thread-9")

    assert str(tmp_path).replace("\\", "/") in expanded
    assert "会话=thread-9" in expanded


def test_skill_md_shorthand_also_works_as_a_slash_name(tmp_path: Path) -> None:
    _write_command(tmp_path, "db/SKILL.md", _command(body="迁移 ${CLAUDE_SKILL_DIR}"))

    expanded, info = _expand(tmp_path, "/db")

    assert info["name"] == "db"
    assert str((tmp_path / ".blank" / "commands" / "db")).replace("\\", "/") in expanded


def test_split_slash_requires_a_single_token_name() -> None:
    assert split_slash("/db:migrate up") == ("db:migrate", "up")
    assert split_slash(" /deploy\n生产 ") == ("deploy", "生产")
    assert split_slash("/path/to/file") is None
    assert split_slash("just text") is None


# --------------------------------------------------------------------------- #
# 4. 模型侧：清单与 skill 工具
# --------------------------------------------------------------------------- #
def test_catalog_lists_commands_and_honours_disable_model_invocation(tmp_path: Path) -> None:
    _write_command(tmp_path, "visible.md", _command())
    _write_command(tmp_path, "hidden.md", "---\ndescription: 人跑\ndisable-model-invocation: true\n---\n人跑\n")
    found = _discover(tmp_path)

    catalog = SkillCatalog([item.as_entry() for item in found.entries.values()])

    assert sorted(item["name"] for item in catalog.listing()) == ["visible"]
    assert catalog.get("visible")["kind"] == "command"


async def test_skill_tool_loads_a_command_body_and_refuses_a_hidden_one(tmp_path: Path) -> None:
    _write_command(tmp_path, "visible.md", _command(body="命令正文 $0"))
    _write_command(tmp_path, "hidden.md", "---\ndescription: 人跑\ndisable-model-invocation: true\n---\n人跑\n")
    found = _discover(tmp_path)
    catalog = SkillCatalog([item.as_entry() for item in found.entries.values()])

    loaded = await _tool().handler(_context(catalog), {"skill": "visible", "args": "参数"})
    refused = await _tool().handler(_context(catalog), {"skill": "hidden"})

    assert loaded.ok is True
    assert "命令正文 参数" in loaded.output
    assert refused.ok is False
    assert "disable-model-invocation" in refused.message


# --------------------------------------------------------------------------- #
# 5. 运行时接通
# --------------------------------------------------------------------------- #
async def test_runtime_loads_commands_and_lists_them(
    env, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BLANK_USER_COMMANDS_DIR", str(tmp_path / "empty-user-commands"))
    _write_command(
        workspace,
        "verify.md",
        "---\ndescription: 项目验证命令\n---\n先跑全量回归，再看失败用例。",
    )
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["验证完成"])
    env.mock.reset(
        [
            {"content": "先加载验证命令", "tool_calls": [call("skill", {"skill": "verify"})]},
            {"content": "已按验证命令执行"},
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
            goal="按项目的验证命令检查一遍",
            acceptance_criteria=["验证完成"],
            max_steps=5,
        )
    )

    assert evidence["task"]["status"] == "succeeded", evidence["task"]
    loaded = [item for item in evidence["timeline"] if item["type"] == "commands.loaded"]
    assert loaded, [item["type"] for item in evidence["timeline"]]
    assert loaded[0]["payload"]["count"] == 1
    assert loaded[0]["payload"]["commands"][0]["name"] == "verify"

    main_index = next(
        index
        for index, request in enumerate(env.mock.requests)
        if "skill" in {item["function"]["name"] for item in request.get("tools", [])}
    )
    parent_prompt = _messages_text(env.mock.requests[main_index]["messages"])
    assert "- verify：项目验证命令" in parent_prompt
    # 正文只在 skill 工具里加载，不能提前进系统提示。
    assert "先跑全量回归" not in parent_prompt

    after_tool = "\n".join(
        _messages_text(request["messages"]) for request in env.mock.requests[main_index:]
    )
    assert "已加载 Skill「verify」" in after_tool
    assert "先跑全量回归，再看失败用例。" in after_tool


async def test_runtime_keeps_disable_model_invocation_off_the_list(
    env, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BLANK_USER_COMMANDS_DIR", str(tmp_path / "empty-user-commands"))
    _write_command(workspace, "visible.md", "---\ndescription: 可见命令\n---\n可见正文\n")
    _write_command(
        workspace,
        "hidden.md",
        "---\ndescription: 隐藏命令\ndisable-model-invocation: true\n---\n隐藏正文\n",
    )
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["完成"])
    env.mock.reset([{"content": "看一下清单"}, {"content": "任务完成"}])

    await AgentRuntime(get_session_factory()).run(
        RunRequest(
            task_id=task_id,
            provider_id=provider_id,
            provider_name="mock-vendor",
            adapter=build_adapter(await _provider_row(provider_id), env.mock.factory),
            root=workspace,
            goal="看看有哪些命令",
            acceptance_criteria=["完成"],
            max_steps=4,
        )
    )

    main_index = next(
        index
        for index, request in enumerate(env.mock.requests)
        if "skill" in {item["function"]["name"] for item in request.get("tools", [])}
    )
    prompt = _messages_text(env.mock.requests[main_index]["messages"])
    assert "- visible：可见命令" in prompt
    assert "- hidden：" not in prompt
    assert "隐藏正文" not in prompt


# --------------------------------------------------------------------------- #
# 6. HTTP：创建任务 / 会话回合 / 插话 / 列表
# --------------------------------------------------------------------------- #
async def _events(env, task_id: str) -> list[dict]:
    response = await env.client.get(f"{API}/tasks/{task_id}/events")
    return response.json()


async def test_create_task_expands_the_command_into_the_goal(env, workspace: Path) -> None:
    _write_command(workspace, "greet.md", _command(body="向 $ARGUMENTS 问好"))
    project_id = await create_project(env, workspace)

    created = await env.client.post(
        f"{API}/projects/{project_id}/tasks",
        json={
            "title": "/greet 世界",
            "goal": "/greet 世界",
            "acceptance_criteria": ["完成"],
        },
    )

    assert created.status_code == 201, created.text
    task = created.json()
    assert task["goal"] == "向 世界 问好"
    # 标题保留用户原话：任务列表上要看到自己敲的那条。
    assert task["title"] == "/greet 世界"

    events = await _events(env, task["id"])
    expanded = [item for item in events if item["type"] == "command.expanded"]
    assert expanded, [item["type"] for item in events]
    payload = expanded[0]["payload"]
    assert payload["name"] == "greet"
    assert payload["kind"] == "command"
    assert payload["task_id"] == task["id"]
    assert payload["original_chars"] == len("/greet 世界")
    assert payload["expanded_chars"] == len("向 世界 问好")


async def test_unknown_slash_command_is_stored_verbatim(env, workspace: Path) -> None:
    project_id = await create_project(env, workspace)

    created = await env.client.post(
        f"{API}/projects/{project_id}/tasks",
        json={"title": "原样", "goal": "/maybe/later please", "acceptance_criteria": ["完成"]},
    )

    assert created.status_code == 201, created.text
    assert created.json()["goal"] == "/maybe/later please"
    events = await _events(env, created.json()["id"])
    assert not [item for item in events if item["type"] == "command.expanded"]


async def test_thread_turn_expands_the_command_into_the_goal(env, workspace: Path) -> None:
    _write_command(workspace, "review.md", _command(body="评审 $0"))
    project_id = await create_project(env, workspace)
    thread = (
        await env.client.post(f"{API}/projects/{project_id}/threads", json={"title": "命令会话"})
    ).json()

    turn = await env.client.post(
        f"{API}/threads/{thread['id']}/turns",
        json={"title": "/review main.py", "goal": "/review main.py", "auto_start": False},
    )

    assert turn.status_code == 201, turn.text
    assert turn.json()["goal"] == "评审 main.py"
    events = await _events(env, turn.json()["id"])
    assert [item["payload"]["name"] for item in events if item["type"] == "command.expanded"] == ["review"]


async def test_steer_expands_for_the_model_but_shows_the_raw_message(env, workspace: Path) -> None:
    _write_command(workspace, "hurry.md", _command(body="优先处理 $ARGUMENTS"))
    project_id = await create_project(env, workspace)
    task_id = await create_task(env, project_id, ["完成"])
    async with get_session_factory()() as session:
        row = await session.get(Task, task_id)
        row.status = "running"
        await session.commit()

    response = await env.client.post(
        f"{API}/tasks/{task_id}/steer", json={"message": "/hurry 登录页 bug"}
    )

    assert response.status_code == 200, response.text
    events = await _events(env, task_id)
    queued = next(item for item in events if item["type"] == "steer.queued")
    assert queued["payload"]["message"] == "/hurry 登录页 bug"
    assert queued["payload"]["expanded"] == "优先处理 登录页 bug"
    expanded = next(item for item in events if item["type"] == "command.expanded")
    assert expanded["payload"]["channel"] == "steer"
    assert expanded["payload"]["name"] == "hurry"

    # 读取侧：真正注入给模型的那条 user 消息必须是展开正文，否则用户看得见
    # 命令生效了、模型读到的却还是 `/hurry …`。
    from blank_api.services.agent.events import EventRecorder
    from blank_api.services.agent.graph import AgentSession, drain_steer
    from blank_api.services.tools.base import ToolContext

    session = AgentSession(
        adapter=None,  # type: ignore[arg-type]
        gateway=None,  # type: ignore[arg-type]
        context=ToolContext(root=workspace, task_id=task_id, session_factory=get_session_factory()),
        recorder=EventRecorder(get_session_factory(), task_id),
        session_factory=get_session_factory(),
        provider_id="provider-steer",
        provider_name="mock-vendor",
        goal="插话展开",
    )
    drained = await drain_steer(session, "call_model", step=1)
    assert len(drained) == 1
    content = str(drained[0]["content"])
    assert "优先处理 登录页 bug" in content
    assert "/hurry" not in content


async def test_project_commands_api_lists_commands_without_bodies(env, workspace: Path) -> None:
    _write_command(workspace, "deploy.md", "---\ndescription: 上线流程\nargument-hint: <环境>\n---\n部署正文不应出现在列表里\n")
    _write_command(workspace, "db/migrate.md", _command(body="迁移正文"))
    project_id = await create_project(env, workspace)

    response = await env.client.get(f"{API}/projects/{project_id}/commands")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == 2
    names = {item["name"]: item for item in body["commands"]}
    assert set(names) == {"deploy", "db:migrate"}
    assert names["deploy"]["argumentHint"] == "<环境>"
    assert "部署正文" not in response.text
    assert "迁移正文" not in response.text
