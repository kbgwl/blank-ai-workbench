"""无工作目录的纯聊天：不选目录也能说话，但不给任何碰电脑的工具。"""

from __future__ import annotations

import asyncio

from blank_api.services.agent.chat import chat_root
from blank_api.services.agent.dispatcher import decide_dispatch
from blank_api.services.agent.graph import create_plan_node


def test_chat_only_dispatches_without_workspace():
    decision = decide_dispatch(
        {"intent": "answer", "reason": "寒暄", "needs_task": True},
        project_id="project-1",
        scenario="dev",
        has_workspace=False,
        chat_only=True,
    )

    assert decision["decision"] == "immediate"
    assert decision["auto_start"] is True
    assert decision["conditions"]["workspace_bound"] is True
    assert any("纯聊天" in reason for reason in decision["reasons"])


def test_no_workspace_without_chat_only_still_waits():
    decision = decide_dispatch(
        {"intent": "execute", "reason": "改代码", "needs_task": True},
        project_id="project-1",
        scenario="dev",
        has_workspace=False,
        chat_only=False,
    )

    assert decision["decision"] == "wait_user"
    assert decision["auto_start"] is False
    assert decision["conditions"]["workspace_bound"] is False


def test_chat_root_does_not_escape_its_base():
    path = chat_root("../../evil task")

    assert path.name == "eviltask"
    assert ".." not in path.name


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def add(self, kind: str, node: str, payload: dict | None = None) -> None:
        self.events.append((kind, payload or {}))


class _ChatSession:
    """只实现纯聊天分支真正用到的东西；多一个属性都不该被读到。"""

    chat_only = True

    def __init__(self) -> None:
        self.recorder = _Recorder()

    async def ask(self, state: dict, prompt: str, node: str) -> dict:
        assert prompt == "", "纯聊天不该再追加一条合成的用户消息"
        return {
            "text": "闭包是函数和它捕获的环境。",
            "messages": [
                *state.get("messages", []),
                {"role": "assistant", "content": "闭包是函数和它捕获的环境。"},
            ],
            "usage": {"completion_tokens": 10},
        }


def test_chat_only_create_plan_answers_directly_without_tools():
    session = _ChatSession()
    update = asyncio.run(
        create_plan_node(
            {"task_id": "task-chat", "messages": [{"role": "user", "content": "什么是闭包"}]},
            session,  # type: ignore[arg-type]
        )
    )

    assert update["direct_answer"] is True
    assert update["plan"] == []
    assert update["plan_steps"] == []
    assert "闭包" in update["summary"]
    assert any(kind == "chat.answered" for kind, _ in session.recorder.events)


async def test_chat_only_turn_stores_flag_even_when_project_has_workspace(client, tmp_path):
    project = (
        await client.post("/api/v1/projects", json={"name": "纯聊天测试"})
    ).json()
    (tmp_path / "repo").mkdir()
    bound = await client.post(
        f"/api/v1/projects/{project['id']}/workspaces",
        json={"path": str(tmp_path / "repo")},
    )
    assert bound.status_code == 201

    thread = (
        await client.post(
            f"/api/v1/projects/{project['id']}/threads",
            json={"title": "随便聊聊"},
        )
    ).json()
    response = await client.post(
        f"/api/v1/threads/{thread['id']}/turns",
        json={
            "title": "你好",
            "goal": "你好",
            "chat_only": True,
            "auto_start": False,
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["goal_spec"]["chat_only"] is True
    # 纯聊天不能借用项目活动目录：这条属性必须能穿过 normalize_goal 保留下来。
    assert body["goal_spec"].get("dispatch") is not None


async def test_workspace_turn_is_not_chat_only(client, tmp_path):
    project = (
        await client.post("/api/v1/projects", json={"name": "选了目录"})
    ).json()
    (tmp_path / "repo").mkdir()
    await client.post(
        f"/api/v1/projects/{project['id']}/workspaces",
        json={"path": str(tmp_path / "repo")},
    )
    thread = (
        await client.post(
            f"/api/v1/projects/{project['id']}/threads",
            json={"title": "有目录"},
        )
    ).json()
    response = await client.post(
        f"/api/v1/threads/{thread['id']}/turns",
        json={
            "title": "改 README",
            "goal": "改 README",
            "chat_only": False,
            "auto_start": False,
        },
    )

    assert response.status_code == 201, response.text
    assert "chat_only" not in response.json()["goal_spec"]
