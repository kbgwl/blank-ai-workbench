"""#114：记忆类型化与来源引用。

对标 Claude Code 的四类记忆（user / feedback / project / reference）：

* 模型可以**显式**写一条带类型的长期记忆（``memory_write``），与任务结束时自动
  固化共用同一个文件、同一份去重账本、同一个字符上限；
* 检索可以按类型过滤；旧的无标记条目按 project 读——它们本来就是项目知识，
  不能因为写得早就在 project 检索里消失；
* 每条结果都带 ``source_task``：来源任务写到完整 id，旧文件里的 8 位前缀照实读，
  不补全不编造；
* 类型化不是绕开权限：计划模式下 ``memory_write`` 仍被拒绝（它会改持久状态）。
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from blank_api.services.context_curator import curate
from blank_api.services.memory import MEMORY_TYPES, MemoryStore
from blank_api.services.permissions import DENY, MODE_PLAN, READ_ONLY_TOOLS, PermissionPolicy
from blank_api.services.tools import build_default_tools
from blank_api.services.tools.memory_tools import _memory_search, _memory_write
from blank_api.services.tools.registry import DEFERRED_TOOLS

from tests.test_agent_v04 import (  # noqa: F401  (复用阶段3 的夹具)
    API,
    call,
    create_project,
    create_provider,
    create_task,
    env,
)

TASK_ID = "0b6c1d2e-1111-4222-8333-444455556666"


def _store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(tmp_path / "memory")


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir(exist_ok=True)
    return root


# --------------------------------------------------------------------------- #
# 1. 存储：类型标记、完整来源、去重与旧格式兼容
# --------------------------------------------------------------------------- #
def test_remember_writes_typed_bullet_with_full_source(tmp_path: Path) -> None:
    store = _store(tmp_path)
    workspace = _workspace(tmp_path)
    result = store.remember(
        workspace,
        memory_type="feedback",
        content="别在测试里连真库",
        task_id=TASK_ID,
        source="上一轮驳回",
    )
    assert result.added == ["[feedback] 别在测试里连真库"]

    text = store.load(workspace)
    assert "- [feedback] 别在测试里连真库" in text
    assert f"任务 {TASK_ID}" in text
    assert "来源 上一轮驳回" in text

    hits = store.search(workspace, "测试 真库")
    assert hits and hits[0].bullet == "别在测试里连真库"
    assert hits[0].memory_type == "feedback"
    assert hits[0].source_task == TASK_ID
    payload = hits[0].as_dict()
    assert payload["type"] == "feedback"
    assert payload["source_task"] == TASK_ID
    assert payload["task_id"] == TASK_ID


def test_search_filters_by_type_and_legacy_reads_as_project(tmp_path: Path) -> None:
    store = _store(tmp_path)
    workspace = _workspace(tmp_path)
    store.consolidate(workspace, task_id=TASK_ID, summary="登录接口超时已修复")
    store.remember(workspace, memory_type="feedback", content="测试别连生产库", task_id=TASK_ID)
    store.remember(workspace, memory_type="reference", content="看板在 grafana 面板", task_id=TASK_ID)

    all_hits = store.search(workspace, "登录 真库 看板", limit=10)
    assert {hit.memory_type for hit in all_hits} == {"project", "feedback", "reference"}

    # 旧条目没有类型标记：按 project 读，不能因为格式老就在 project 检索里消失。
    legacy = store.search(workspace, "登录 超时", memory_type="project")
    assert legacy and all(hit.memory_type == "project" for hit in legacy)
    assert any("连接" not in hit.bullet and "登录" in hit.bullet for hit in legacy)

    feedback = store.search(workspace, "登录 真库 看板", memory_type="feedback")
    assert [hit.bullet for hit in feedback] == ["测试别连生产库"]

    reference = store.search(workspace, "登录 真库 看板", memory_type="reference")
    assert [hit.bullet for hit in reference] == ["看板在 grafana 面板"]

    with pytest.raises(ValueError):
        store.search(workspace, "登录", memory_type="recent")


def test_remember_and_consolidation_share_one_dedupe_ledger(tmp_path: Path) -> None:
    store = _store(tmp_path)
    workspace = _workspace(tmp_path)

    first = store.remember(workspace, memory_type="user", content="偏好极简中文回复")
    assert first.added and first.skipped == 0
    again = store.remember(workspace, memory_type="user", content="  偏好极简中文回复  ")
    assert again.added == [] and again.skipped == 1

    # 自动固化那一路的去重没有被动过：同一句结论第二次仍然只记一笔。
    assert store.consolidate(workspace, task_id=TASK_ID, summary="结论一句话").added
    duplicate = store.consolidate(workspace, task_id=TASK_ID, summary="结论一句话")
    assert duplicate.added == [] and duplicate.skipped == 1

    text = store.load(workspace)
    assert text.count("偏好极简中文回复") == 1
    assert text.count("结论一句话") == 1


def test_remember_rejects_bad_input_without_writing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    workspace = _workspace(tmp_path)
    with pytest.raises(ValueError):
        store.remember(workspace, memory_type="diary", content="x")
    with pytest.raises(ValueError):
        store.remember(workspace, memory_type="user", content="   ")
    assert not store.path_for(workspace).exists()
    assert set(MEMORY_TYPES) == {"user", "feedback", "project", "reference"}


def test_curator_labels_each_hit_with_its_type(tmp_path: Path) -> None:
    store = _store(tmp_path)
    workspace = _workspace(tmp_path)
    store.remember(workspace, memory_type="feedback", content="登录相关测试别连真库", task_id=TASK_ID)

    result = curate(root=workspace, goal="登录测试", memory=store)
    assert "共享项目记忆" in result.text
    assert "feedback" in result.text
    assert result.memory_hits[0]["type"] == "feedback"
    assert result.memory_hits[0]["source_task"] == TASK_ID


# --------------------------------------------------------------------------- #
# 2. 工具：模型可写、可筛、坏输入不落盘、不进计划模式
# --------------------------------------------------------------------------- #
async def test_memory_write_handler_persists_and_reports_duplicates(tmp_path: Path) -> None:
    store = _store(tmp_path)
    context = SimpleNamespace(root=_workspace(tmp_path), task_id=TASK_ID, memory=store)

    written = await _memory_write(context, {"type": "user", "content": "偏好简短回复", "source": "用户原话"})
    assert written.ok and written.data["type"] == "user" and written.data["added"] == 1

    repeated = await _memory_write(context, {"type": "user", "content": "偏好简短回复"})
    assert repeated.ok
    assert repeated.data["added"] == 0 and repeated.data["skipped"] == 1

    found = await _memory_search(context, {"query": "简短 回复", "type": "user"})
    assert found.ok and found.data["total"] == 1
    assert found.data["hits"][0]["type"] == "user"
    assert found.data["hits"][0]["source_task"] == TASK_ID
    assert "偏好简短回复" in found.output
    assert "[user｜" in found.output

    wrong_type = await _memory_search(context, {"query": "简短", "type": "recent"})
    assert wrong_type.ok is False and "类型" in wrong_type.message

    assert (await _memory_write(context, {"type": "diary", "content": "x"})).ok is False
    assert (await _memory_write(context, {"type": "user", "content": "  "})).ok is False
    assert store.load(context.root).count("偏好简短回复") == 1


def test_memory_tools_are_registered_and_not_plan_readonly() -> None:
    tools = {tool.name: tool for tool in build_default_tools()}
    assert "memory_write" in tools
    assert tools["memory_write"].risk_level == "low"
    assert "memory_write" in DEFERRED_TOOLS
    # 写长期记忆会改持久状态：计划模式只读，不能借"低风险"溜进去。
    assert "memory_write" not in READ_ONLY_TOOLS

    policy = PermissionPolicy(mode=MODE_PLAN)
    verdict = policy.decide(tools["memory_write"], {"type": "user", "content": "x"}, "low")
    assert verdict.outcome == DENY
    assert verdict.rule == "mode:plan"


# --------------------------------------------------------------------------- #
# 3. 端到端：模型写一次 → 同任务检索带来源 → 换任务重复写不再落一笔
# --------------------------------------------------------------------------- #
def _tool_done(events: list[dict], tool: str) -> list[dict]:
    return [
        event["payload"]
        for event in events
        if event["type"] == "tool.completed" and event["payload"].get("tool") == tool
    ]


async def test_model_writes_a_typed_memory_and_finds_it_with_source(
    env, workspace: Path, tmp_path: Path, monkeypatch
):  # noqa: F811
    memory_dir = tmp_path / "memory-e2e"
    monkeypatch.setenv("BLANK_MEMORY_DIR", str(memory_dir))
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["记住这条偏好"])

    env.mock.reset(
        [
            {"content": "执行计划：\n1. 记一条偏好"},
            {
                "content": "记下来",
                "tool_calls": [
                    call(
                        "memory_write",
                        {"type": "user", "content": "偏好极简中文回复", "source": "用户原话"},
                    )
                ],
            },
            {
                "content": "查一下",
                "tool_calls": [call("memory_search", {"query": "极简 中文 回复", "type": "user"})],
            },
            {"content": "已完成"},
            {"content": "已完成"},
        ]
    )
    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    written = _tool_done(events, "memory_write")
    assert len(written) == 1 and written[0]["ok"] is True, events
    assert "极简中文回复" in written[0]["summary"]

    searched = _tool_done(events, "memory_search")
    assert len(searched) == 1 and searched[0]["ok"] is True, events
    assert "偏好极简中文回复" in searched[0]["summary"]
    assert "[user｜" in searched[0]["summary"]

    store = MemoryStore(memory_dir)
    text = store.load(workspace)
    assert "- [user] 偏好极简中文回复" in text
    assert f"任务 {task_id}" in text
    assert "来源 用户原话" in text

    # 换一个任务再写同一句：账本上仍只有一笔。
    second = await create_task(env, project_id, ["再说一遍"])
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 再记一次"},
            {
                "content": "再说一遍",
                "tool_calls": [
                    call("memory_write", {"type": "user", "content": "偏好极简中文回复"})
                ],
            },
            {"content": "已完成"},
        ]
    )
    resp = await env.client.post(f"{API}/tasks/{second}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    events = (await env.client.get(f"{API}/tasks/{second}/events")).json()
    repeated = _tool_done(events, "memory_write")
    assert len(repeated) == 1 and "未重复写入" in repeated[0]["summary"], repeated
    assert store.load(workspace).count("- [user] 偏好极简中文回复") == 1
