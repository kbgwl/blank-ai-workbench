"""否定式指令：用户说「不要动工具」时，不能因为句子里有动作词就去干活。"""

from __future__ import annotations

import asyncio

from blank_api.services.agent.directives import (
    NONE,
    NO_TOOLS,
    READ_ONLY,
    directive_mode,
    documentation_only,
    listing_only,
)
from blank_api.services.agent.dispatch import classify
from blank_api.services.agent.graph import _list_only_summary, create_plan_node
from blank_api.services.agent.intent import INTENT_ACT, INTENT_ANSWER, guess_intent
from blank_api.services.agent.supervisor import observe


def test_directive_mode_recognizes_no_tool_requests():
    assert directive_mode("只回复 OK，不要读取或修改任何文件") == NO_TOOLS
    assert directive_mode("不要调用任何工具，直接说结论") == NO_TOOLS
    assert directive_mode("不要读取文件，凭你的知识回答") == NO_TOOLS


def test_directive_mode_recognizes_read_only_requests():
    assert directive_mode("不要修改代码，帮我看看哪里错") == READ_ONLY
    assert directive_mode("不要执行命令，只分析") == READ_ONLY
    assert directive_mode("只解释一下这段代码") == READ_ONLY


def test_directive_mode_does_not_turn_reminders_into_prohibitions():
    # 「不要忘了运行测试」是在催办，不是在禁止运行。
    assert directive_mode("不要忘了运行测试") == NONE
    assert directive_mode("帮我改 README") == NONE
    # 「不要改变对外 API 名称」是修复约束，不是「不要改代码」。
    # 旧实现里的裸「改」会匹配「改变」，把整轮锁成只读，代码一处不改必失败。
    assert directive_mode("修复导出错误，不要改变对外 API 名称") == NONE


def test_out_of_scope_constraint_is_not_readonly():
    # B7 真机：shopbench 任务末尾写「不要修改工作区之外的文件」，
    # 旧实现命中 _NO_WRITE，整轮 0 改动却 succeeded。范围约束必须放行工作区内写入。
    goal = (
        "基于附件里的商品资料搭建一个可运行的 Web 工作台；"
        "不要修改工作区之外的文件。"
    )
    assert directive_mode(goal) == NONE
    assert guess_intent(goal) == INTENT_ACT
    assert observe(goal).read_only is False
    # 真正禁止修改任何文件时仍要锁只读。
    assert directive_mode("不要修改任何文件，只分析") == READ_ONLY


def test_file_whitelist_and_named_file_protection_are_not_readonly():
    # 基准题的真实约束：只许改列出的文件，其余文件不要动——这是白名单，不是整轮只读。
    whitelist = (
        "整理当前目录，把文本复制到 整理/文本/。"
        "约束（必须遵守）：只允许创建或修改这些文件：整理/文本/a.txt、整理说明.md。"
        "其余文件（尤其测试文件）一律不要改动。"
    )
    assert directive_mode(whitelist) == NONE
    # 单文件保护：任务要生成 汇总.md，同时要求 data.csv 不动。
    assert directive_mode("读取 data.csv，生成 汇总.md；不要修改 data.csv") == NONE
    assert directive_mode("不要修改 src/main.py，补齐它的测试") == NONE
    # 真正禁止修改任何文件时仍要锁只读。
    assert directive_mode("不要修改任何文件，只分析") == READ_ONLY
    assert directive_mode("不要修改代码，帮我看看哪里错") == READ_ONLY


def test_documentation_only_needs_a_doc_target_and_an_explicit_boundary():
    assert documentation_only(
        "在 README 增加 slugify / word_count / parse_minutes 三个函数的中文示例，不改动源码",
        ["每个函数有示例", "未修改 src 目录"],
    )
    assert documentation_only("只改 README，补一段用法说明") is True
    # 只提到 README 不够：修 README 里的代码示例也可能要改源码。
    assert documentation_only("在 README 增加示例并修正 src/cli_tools/text.py") is False
    assert documentation_only("在 src/cli_tools/text.py 增加 truncate，补测试") is False


def test_listing_only_detects_file_lists_but_not_content_requests():
    assert listing_only("不要修改任何文件，只列出这个目录里有哪些文件") is True
    assert listing_only("列一下目录结构") is True
    assert listing_only("这个目录里有哪些文件") is True
    # 要内容/用途/分析，就不是“只要路径清单”，不能被收窄工具。
    assert listing_only("列出所有文件并说明每个文件的用途") is False
    assert listing_only("列出 README 的内容") is False
    assert listing_only("列出三种排序算法") is False


def test_negated_action_words_do_not_become_act():
    assert guess_intent("只回复 OK，不要读取或修改任何文件") == INTENT_ANSWER
    # 只读执行仍是 act：可以读、可以列举，只是写/执行被 readonly 闸门拒掉。
    assert guess_intent("不要修改代码，帮我看看哪里错") == INTENT_ACT
    assert guess_intent("只解释一下这段代码") == INTENT_ACT
    assert guess_intent("帮我改 README") == INTENT_ACT


def test_supervisor_marks_no_tools_and_read_only():
    no_tools = observe("只回复 OK，不要读取或修改任何文件")
    assert no_tools.intent == "answer"
    assert no_tools.no_tools is True
    assert no_tools.needs_execution is False

    read_only = observe("不要修改代码，帮我看看哪里错")
    assert read_only.intent == "execute"
    assert read_only.read_only is True
    assert read_only.no_tools is False
    assert read_only.needs_execution is True


def test_dispatch_classifies_negated_requests_by_the_user_boundary():
    assert classify("只回复 OK，不要读取或修改任何文件")[0] == "answer"
    assert classify("不要修改代码，帮我看看哪里错")[0] == "execute"


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def add(self, kind: str, node: str, payload: dict | None = None) -> None:
        self.events.append((kind, payload or {}))


class _NoToolSession:
    no_tools = True

    def __init__(self) -> None:
        self.recorder = _Recorder()

    async def ask(self, state: dict, prompt: str, node: str) -> dict:
        assert prompt == ""
        return {
            "text": "OK",
            "messages": [*state.get("messages", []), {"role": "assistant", "content": "OK"}],
            "usage": {"completion_tokens": 1},
        }


class _ReadOnlySession:
    """模型可能判 act/advise；硬指令必须把交付拉回只读执行并锁住写工具。"""

    chat_only = False
    no_tools = False
    scenario = "dev"
    readonly = False
    max_steps = 200

    class _Context:
        turn_state: dict = {}

    def __init__(self) -> None:
        self.recorder = _Recorder()
        self.context = self._Context()
        self.turn_effort = ""
        self.effort_reminder = ""
        self.effort_warning = ""
        self.goal = "不要修改任何文件，只列出这个目录里有哪些文件"
        self.goal_spec: dict = {}

    async def ask(self, state: dict, prompt: str, node: str) -> dict:
        return {
            # 模型误判成 advise；硬指令必须把它掰回 act（只读执行），
            # 否则会走“给判断”的交付格式，列不出用户要的清单。
            "text": '{"intent":"advise","note":"列出目录","steps":["列出工作区目录文件"],"answer":""}',
            "messages": [*state.get("messages", []), {"role": "assistant", "content": "计划"}],
            "usage": {"completion_tokens": 3},
            "tool_calls": [],
        }


def test_no_tools_turn_answers_directly_without_calling_tools():
    session = _NoToolSession()
    update = asyncio.run(
        create_plan_node(
            {"task_id": "task-no-tools", "messages": [{"role": "user", "content": "只回复 OK"}]},
            session,  # type: ignore[arg-type]
        )
    )

    assert update["direct_answer"] is True
    assert update["plan"] == []
    assert update["summary"] == "OK"


def test_read_only_directive_overrides_model_act_and_limits_tools():
    session = _ReadOnlySession()
    update = asyncio.run(
        create_plan_node(
            {"task_id": "task-read-only", "messages": [{"role": "user", "content": session.goal}]},
            session,  # type: ignore[arg-type]
        )
    )

    # 模型判成 advise，但用户明确说不要修改：仍是只读执行，交付走正常总结。
    assert session.readonly is True
    assert session.max_steps <= 8
    assert update["readonly"] is True
    assert update["direct_answer"] is False
    assert update["pending"] == []
    assert any(
        payload.get("reason") == "read_only_directive" and payload.get("to") == "act"
        for kind, payload in session.recorder.events
        if kind == "intent.overridden"
    )


def test_list_only_summary_is_built_from_tool_results_not_model_prose():
    state = {
        "tool_results": [
            {
                "tool": "list_files",
                "ok": True,
                "data": {
                    "files": [
                        {"name": "app.js", "path": "app.js", "is_dir": False, "size": 10},
                        {"name": "src", "path": "src", "is_dir": True},
                        {"name": "main.py", "path": "src/main.py", "is_dir": False, "size": 20},
                    ]
                },
            }
        ]
    }
    text = _list_only_summary(state)
    assert text.startswith("工作区文件：")
    assert "- `app.js`（10 字节）" in text
    assert "- `src/`" in text
    assert "  - `main.py`（20 字节）" in text
    assert "总结" not in text


async def test_no_tools_turn_stores_flag(client, tmp_path):
    project = (await client.post("/api/v1/projects", json={"name": "只回复"})).json()
    (tmp_path / "repo").mkdir()
    await client.post(
        f"/api/v1/projects/{project['id']}/workspaces",
        json={"path": str(tmp_path / "repo")},
    )
    thread = (
        await client.post(
            f"/api/v1/projects/{project['id']}/threads",
            json={"title": "只回复测试"},
        )
    ).json()
    response = await client.post(
        f"/api/v1/threads/{thread['id']}/turns",
        json={
            "title": "只回复 OK",
            "goal": "只回复 OK，不要读取或修改任何文件",
            "no_tools": True,
            "auto_start": False,
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["goal_spec"]["no_tools"] is True
