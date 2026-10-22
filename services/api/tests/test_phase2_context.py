"""第二阶段：上下文管家、记忆分层与并发、中文检索分词。"""

from __future__ import annotations

import threading
import traceback
from pathlib import Path

from blank_api.services.context_curator import curate
from blank_api.services.memory import MemoryStore
from blank_api.services.text_index import tokenize
from tests.test_agent_v04 import API, env  # noqa: F401
from tests.test_handoff_e2e_v12 import _successful_run, system_text


def test_chinese_tokens_need_no_spaces() -> None:
    tokens = tokenize("登录接口 login 超时")
    assert "登录" in tokens and "接口" in tokens and "超时" in tokens
    assert "login" in tokens
    # 虚词不单独成词，否则几乎每条记忆都会被命中
    assert "的" not in tokens


def test_memory_search_works_without_spaces(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory")
    workspace = tmp_path / "repo"
    workspace.mkdir()
    store.consolidate(
        workspace,
        task_id="t1",
        summary="登录接口超时的根因是连接池耗尽，已改成复用连接",
    )
    hits = store.search(workspace, "登录超时")
    assert hits and "连接池" in hits[0].bullet
    assert hits[0].kind == "任务结论"
    assert store.search(workspace, "支付回调幂等") == []


def test_memory_keeps_source_and_kind(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory")
    workspace = tmp_path / "repo"
    workspace.mkdir()
    store.consolidate(
        workspace,
        task_id="abcdef123456",
        title="修登录",
        summary="已修复",
        notes="- token 解析少了一个 await",
        facts=["验证命令 pytest -q → ok"],
        source="团队·交付群·后端",
    )
    text = store.load(workspace)
    assert "任务 abcdef12" in text
    assert "来源 团队·交付群·后端" in text
    assert "- 结果：已修复" in text
    assert "- 笔记：token 解析少了一个 await" in text
    hits = store.search(workspace, "token await")
    assert hits and hits[0].kind == "交接笔记"


def test_concurrent_consolidation_does_not_lose_bullets(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory")
    workspace = tmp_path / "repo"
    workspace.mkdir()
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            store.consolidate(
                workspace, task_id=f"task-{index}", summary=f"并发任务 {index} 的结论"
            )
        except BaseException as exc:  # noqa: BLE001 - 线程里的异常要带回主线程断言
            errors.append(traceback.format_exc())

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(24)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors, errors
    text = store.load(workspace)
    missing = [index for index in range(24) if f"并发任务 {index} 的结论" not in text]
    assert not missing, missing
    assert not list((tmp_path / "memory").glob("*.tmp"))


def test_curator_picks_memory_and_file_candidates(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "login_view.py").write_text("print('login')\n", encoding="utf-8")
    (workspace / "node_modules").mkdir()
    (workspace / "node_modules" / "login.js").write_text("x", encoding="utf-8")
    store = MemoryStore(tmp_path / "memory")
    store.consolidate(
        workspace, task_id="t1", summary="登录页必须带双因子校验", source="任务"
    )

    result = curate(root=workspace, goal="修复 login view 的登录页", memory=store)
    assert "共享项目记忆" in result.text
    assert "双因子" in result.text
    assert "login_view.py" in result.files
    assert all("node_modules" not in path for path in result.files)
    assert result.memory_hits and result.memory_hits[0]["source"] == "workspace"


def test_curator_respects_budget(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    store = MemoryStore(tmp_path / "memory")
    for index in range(6):
        store.consolidate(
            workspace,
            task_id=f"t{index}",
            summary=("很长的记忆条目" + str(index)) * 40,
        )
    result = curate(root=workspace, goal="很长的记忆条目", memory=store, max_chars=600)
    assert result.truncated is True
    assert len(result.text) <= 600


async def test_runtime_emits_context_curated_and_injects_it(env, workspace: Path) -> None:  # noqa: F811
    store = MemoryStore()
    store.consolidate(
        workspace,
        task_id="old-task",
        title="main.py 改造",
        summary="main.py 的运行测试必须保留 patched 标记",
        source="人工记录",
    )
    task_id, _provider, resp = await _successful_run(env, workspace)
    assert resp.status_code == 200, resp.text

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    curated = [event for event in events if event["type"] == "context.curated"]
    assert curated, [event["type"] for event in events]
    payload = curated[0]["payload"]
    assert payload["memory_hits"], payload
    assert any(item["kind"] == "任务结论" for item in payload["memory_hits"])
    assert "共享项目记忆" in system_text(env.mock.requests[0])
    assert "patched 标记" in system_text(env.mock.requests[0])
