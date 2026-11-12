"""AA 批：Skill frontmatter 的 ``paths`` 条件激活（对齐 Claude 的 conditionalSkills）。

上游锚点：``y1r`` 把 ``paths`` 归一化（字符串按逗号拆、花括号内不拆，列表拍平、
非字符串项丢弃，尾部 ``/**`` 去掉；空表或全是 ``**`` 就当没有）,加载时这类 Skill
不进清单（``conditionalSkills``），文件工具碰到匹配路径时由 ``p1r`` 激活
（``ignore().add(paths).ignores(path)``）并加入 ``dynamicSkills``。

本机口径：
1. 解析与上游同形，``paths`` 不再是"只看不用"的字段；
2. 匹配是 gitignore 近似：不带 ``/`` 的按任意层级匹配、带 ``/`` 的锚在根、
   ``**`` 跨目录、``*`` / ``?`` 不跨；``!`` 取反按顺序生效；绝对路径与 ``..`` 一律不匹配；
3. 激活前：清单里没有、``skill`` 工具按名字也拿不到（不许猜）；激活后两者都放开；
4. 触发只看成功落盘/读取的文件工具的台账路径（glob / search_code 的 pattern 不算路径）；
5. 台账重放幂等：同一 Skill 只激活一次，审批恢复重建目录后能靠检查点把状态找回来；
6. agent 定义里显式点名的 Skill 不受条件闸门拦截（作者已经明确说了要用）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from blank_api.db import get_session_factory
from blank_api.services.agent.runtime import AgentRuntime, RunRequest
from blank_api.services.models.registry import build_adapter
from blank_api.services.skills_defs import (
    _path_list,
    discover_skills,
    match_skill_paths,
)
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.skills import (
    SkillCatalog,
    activate_from_tool_results,
    preload_skills,
    render_skill_activation,
    skill_tools,
    touched_paths,
)

from tests.test_agent_v04 import (  # noqa: F401  (env 是夹具，故意导入)
    call,
    create_provider,
    create_project,
    create_task,
    env,
)
from tests.test_skills_disk_v45 import _messages_text, _provider_row, _write_skill


def _context(catalog: SkillCatalog | None) -> ToolContext:
    return ToolContext(root=Path("."), task_id="task-paths", session_factory=None, skills=catalog)


def _catalog(**extra) -> SkillCatalog:
    return SkillCatalog(
        [
            {
                "name": "py-rules",
                "description": "Python 改动规范",
                "instructions": "改完先跑 pytest。",
                "paths": ["src/**"],
                **extra,
            },
            {"name": "deploy", "description": "上线步骤", "instructions": "先跑全量。"},
        ]
    )


# --------------------------------------------------------------------------- #
# 1. 解析
# --------------------------------------------------------------------------- #
def test_paths_frontmatter_is_parsed_and_no_longer_ignored(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "py-rules",
        "---\nname: py-rules\ndescription: Python 规范\npaths: src/**, tests/**\n---\n改完先跑 pytest。",
    )

    item = discover_skills(tmp_path).entries["py-rules"]

    assert item.paths == ("src", "tests")
    assert item.conditional is True
    assert "paths" not in item.ignored
    audit = item.as_dict()
    assert audit["paths"] == ["src", "tests"]
    assert audit["conditional"] is True


def test_paths_split_on_commas_outside_braces() -> None:
    assert _path_list("a/**, *.{ts,tsx}") == (("a", "*.{ts,tsx}"), "")


def test_paths_list_flattens_and_drops_non_strings() -> None:
    patterns, warning = _path_list(["src/**", 3, "lib/**"])

    assert patterns == ("src", "lib")
    assert "非字符串" in warning


def test_paths_without_any_real_pattern_means_unconditional() -> None:
    assert _path_list("**") == ((), "")
    assert _path_list(["**", "/**"]) == ((), "")
    assert _path_list(None) == ((), "")
    # 只要还有一条真规则就仍是条件技能，"**"本身也随之留下。
    assert _path_list(["src/**", "**"]) == (("src", "**"), "")


def test_invalid_paths_shape_is_a_warning_not_a_crash() -> None:
    patterns, warning = _path_list({"a": 1})

    assert patterns == ()
    assert "字符串" in warning


# --------------------------------------------------------------------------- #
# 2. 匹配
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("patterns", "path", "expected"),
    [
        (["src"], "src/a/b.py", True),
        (["src"], "apps/a.py", False),
        (["api/**"], "api/v1/x.py", True),
        (["api/**"], "web/api/x.py", False),
        (["*.md"], "docs/readme.md", True),
        (["*.md"], "docs/keep.py", False),
        (["**/*.py"], "a/b/c.py", True),
        (["**/*.py"], "c.py", True),
        (["src/**/test_*"], "src/x/y/test_a.py", True),
        (["docs/**", "!docs/keep.md"], "docs/keep.md", False),
        (["docs/**", "!docs/keep.md"], "docs/other.md", True),
        (["src"], "srcx/a.py", False),
        (["*"], "a/b.py", True),
    ],
)
def test_path_matching_semantics(patterns, path, expected) -> None:
    assert match_skill_paths(patterns, path) is expected


def test_absolute_and_parent_paths_never_match() -> None:
    assert match_skill_paths(["*.py"], r"C:\repo\a.py") is False
    assert match_skill_paths(["*.py"], "/repo/a.py") is False
    assert match_skill_paths(["**"], "../a.py") is False
    assert match_skill_paths(["**"], "src/../a.py") is False


# --------------------------------------------------------------------------- #
# 3. 目录可见性与激活
# --------------------------------------------------------------------------- #
def test_conditional_skill_is_hidden_until_a_matching_path_is_touched() -> None:
    catalog = _catalog()

    assert [item["name"] for item in catalog.listing()] == ["deploy"]
    assert catalog.model_names() == ["deploy"]
    assert catalog.get("py-rules") is None
    assert catalog.model_available() is True

    assert catalog.touch(["docs/readme.md"]) == []
    assert [item["name"] for item in catalog.listing()] == ["deploy"]

    activated = catalog.touch(["src/app.py"])

    assert [item["name"] for item in activated] == ["py-rules"]
    assert activated[0]["path"] == "src/app.py"
    assert [item["name"] for item in catalog.listing()] == ["py-rules", "deploy"]
    assert "py-rules" in catalog.model_names()
    assert catalog.get("py-rules") is not None
    # 幂等：再重放一遍台账不会再宣布一次。
    assert catalog.touch(["src/app.py"]) == []


def test_touched_paths_only_counts_successful_file_tools() -> None:
    results = [
        {"tool": "read_file", "ok": True, "data": {"path": "src/a.py"}},
        {"tool": "glob", "ok": True, "data": {"path": "src/b.py"}},
        {"tool": "edit_file", "ok": False, "data": {"path": "src/c.py"}},
        {"tool": "edit_file", "ok": True, "data": {"path": "src/d.py"}},
        {"tool": "notebook_edit", "ok": True, "data": {"path": "nb.ipynb"}},
    ]

    assert touched_paths(results) == ["src/a.py", "src/d.py", "nb.ipynb"]
    assert activate_from_tool_results(_catalog(), results)[0]["name"] == "py-rules"


async def test_skill_tool_refuses_the_hidden_name_then_loads_after_activation() -> None:
    catalog = _catalog()
    tool = skill_tools()[0]

    refused = await tool.handler(_context(catalog), {"skill": "py-rules"})

    assert refused.ok is False
    assert "没有名为「py-rules」" in refused.message
    # 报错里列出的可用名字不能把没激活的条件技能带出来。
    assert "py-rules" not in refused.message.split("当前可用：")[1]

    catalog.touch(["src/app.py"])
    loaded = await tool.handler(_context(catalog), {"skill": "py-rules"})

    assert loaded.ok is True
    assert "改完先跑 pytest。" in loaded.output


def test_preload_ignores_the_conditional_gate() -> None:
    # agent 定义里点名要的 Skill 是作者显式声明，不该因为 paths 而加载不到。
    loaded, problems = preload_skills(_catalog(), ["py-rules"])

    assert problems == []
    assert loaded and loaded[0][0] == "py-rules"
    assert "改完先跑 pytest。" in loaded[0][1]


def test_activation_notice_names_the_skill_and_the_matched_file() -> None:
    text = render_skill_activation(
        [{"name": "py-rules", "description": "Python 改动规范", "path": "src/a.py"}]
    )

    assert "<system-reminder>" in text
    assert "- py-rules：Python 改动规范（匹配 src/a.py）" in text
    assert "skill 工具" in text


# --------------------------------------------------------------------------- #
# 4. 真实运行时：读文件 → 下一问里出现「现在可以用了」→ 能加载
# --------------------------------------------------------------------------- #
async def test_runtime_announces_conditional_skill_after_touching_a_matching_file(
    env, workspace: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BLANK_USER_SKILLS_DIR", str(tmp_path / "empty-user-skills"))
    _write_skill(
        workspace,
        "py-rules",
        "---\nname: py-rules\ndescription: Python 改动规范\npaths: src/**\n---\n改完先跑 pytest。",
    )
    (workspace / "src").mkdir(exist_ok=True)
    (workspace / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")

    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["改完 src/app.py"])
    env.mock.reset(
        [
            # 第一步是意图判定：create_plan 自己也要问一次模型，这条只给计划、不出工具，
            # 否则真正的工具序列会整体前移一格，激活与加载的先后关系就测不到了。
            {"content": '{"intent": "act", "steps": ["读文件", "加载规范", "改完"]}'},
            {"content": "先看一眼", "tool_calls": [call("read_file", {"path": "src/app.py"})]},
            {"content": "加载规范", "tool_calls": [call("skill", {"skill": "py-rules"})]},
            {"content": "已按规范改完"},
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
            goal="改一下 src/app.py",
            acceptance_criteria=["改完 src/app.py"],
            max_steps=6,
        )
    )

    assert evidence["task"]["status"] == "succeeded", evidence["task"]

    main_index = next(
        index
        for index, request in enumerate(env.mock.requests)
        if "skill" in {item["function"]["name"] for item in request.get("tools", [])}
    )
    first_prompt = _messages_text(env.mock.requests[main_index]["messages"])
    assert "- py-rules" not in first_prompt
    assert "可用 Skill" not in first_prompt

    after_read = "\n".join(
        _messages_text(request["messages"]) for request in env.mock.requests[main_index:]
    )
    assert "现在可以调用" in after_read
    assert "py-rules" in after_read
    assert "匹配 src/app.py" in after_read
    assert "已加载 Skill「py-rules」" in after_read
    assert "改完先跑 pytest。" in after_read

    activated = [item for item in evidence["timeline"] if item["type"] == "skills.activated"]
    assert activated, [item["type"] for item in evidence["timeline"]]
    assert activated[0]["payload"]["skills"] == [{"skill": "py-rules", "path": "src/app.py"}]
