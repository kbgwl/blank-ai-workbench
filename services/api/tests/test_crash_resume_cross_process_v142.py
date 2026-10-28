# -*- coding: utf-8 -*-
"""#140 跨进程崩溃恢复：杀掉真 worker 进程，另一个进程从检查点续跑。

装置形状（与仓库外探针 `E:/tmp/m90/worker/probe_worker.py` 同构，这里落成用例）：
1. 进程内起真 HTTP 桩模型（OpenAI chat/completions）。第一次调用返回 run_shell；
   收到带 tool 消息的调用时写 marker 并挂住——此时工具已执行、检查点已由 worker 写下。
2. 父进程建库/项目/供应商/任务/queued run，启动 worker 进程 A。
3. 看到 marker 后 TerminateProcess 掉 A（不跑 finally、不写终态）。
4. 等租约过期，启动 worker 进程 B；B 认领同一 run（attempts=2）并从检查点续跑。
5. 断言：任务完成、`progress.txt` 只多一行、`run_shell` 只执行一次、
   `task.resumed` 带 crash_recovery=True。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from sqlalchemy import func, select

from blank_api.db import Base, get_engine, get_session_factory
from blank_api.models.checkpoint import AgentCheckpoint
from blank_api.models.model_provider import ModelProvider
from blank_api.models.project import Project, Workspace
from blank_api.models.run import AgentRun
from blank_api.models.task import AgentEvent, Task, ToolCall
from blank_api.services.agent.queue import utc_now
from blank_api.services.crypto import encrypt_secret

pytestmark = pytest.mark.asyncio


class _StubModel:
    """跨进程时不能 monkeypatch 模型客户端，只能真起一个 HTTP 桩。"""

    def __init__(self, marker: Path) -> None:
        self.marker = marker
        self.release = threading.Event()
        self.calls: list[dict] = []
        self.lock = threading.Lock()

    def respond(self, payload: dict) -> dict:
        messages = payload.get("messages") or []
        has_tool = any(isinstance(item, dict) and item.get("role") == "tool" for item in messages)
        with self.lock:
            self.calls.append({"index": len(self.calls), "has_tool": has_tool})
        if has_tool:
            if not self.marker.exists():
                self.marker.write_text("blocked\n", encoding="utf-8")
                # 挂到父进程把 worker 杀掉：这一刻返回不返回都无所谓了。
                self.release.wait(timeout=120)
                return {"content": "崩溃点之前那次调用不该走到这里。"}
            return {"content": "崩溃续跑完成：progress.txt 已追加一行。"}
        return {
            "content": json.dumps(
                {"intent": "act", "steps": ["往 progress.txt 追加一行"]}, ensure_ascii=False
            ),
            "tool_calls": [
                {
                    "id": f"call_{uuid.uuid4().hex[:8]}",
                    "type": "function",
                    "function": {
                        "name": "run_shell",
                        "arguments": json.dumps(
                            {"command": "echo crash-probe>>progress.txt"}, ensure_ascii=False
                        ),
                    },
                }
            ],
        }


def _serve(stub: _StubModel) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("content-length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            message = stub.respond(payload)
            body = json.dumps(
                {
                    "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": "stub-coder",
                    "choices": [
                        {
                            "index": 0,
                            "message": message,
                            "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 120, "completion_tokens": 20, "total_tokens": 140},
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass  # 对面进程已经被杀，写不回去是预期内的

        def log_message(self, *args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


async def _seed(workspace: Path, base_url: str) -> dict[str, str]:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    stamp = uuid.uuid4().hex[:8]
    ids = {
        "project": f"p-crash-{stamp}",
        "workspace": f"w-crash-{stamp}",
        "provider": f"pv-crash-{stamp}",
        "task": f"t-crash-{stamp}",
        "run": f"r-crash-{stamp}",
    }
    async with get_session_factory()() as session:
        session.add(Project(id=ids["project"], name="crash", description=""))
        await session.flush()
        session.add(
            Workspace(
                id=ids["workspace"],
                project_id=ids["project"],
                label="crash",
                path=str(workspace),
                is_active=True,
            )
        )
        session.add(
            ModelProvider(
                id=ids["provider"],
                name=f"stub-{stamp}",
                kind="openai_compatible",
                base_url=base_url,
                api_key_encrypted=encrypt_secret("SyntheticNotARealKey0001"),
                default_model="stub-coder",
                supports_tools=True,
                enabled=True,
            )
        )
        session.add(
            Task(
                id=ids["task"],
                project_id=ids["project"],
                provider_id=ids["provider"],
                title="往 progress.txt 追加一行",
                goal="往 progress.txt 追加一行",
                status="draft",
                max_steps=4,
            )
        )
        session.add(
            AgentRun(
                id=ids["run"],
                task_id=ids["task"],
                status="queued",
                request_payload={"permission_mode": "default"},
            )
        )
        await session.commit()
    return ids


def _start_worker(wid: str, workspace: Path, log_path: Path) -> subprocess.Popen:
    src = str(Path(__file__).resolve().parents[1] / "src")
    env = dict(os.environ)
    env.update(
        {
            "BLANK_WORKER_ONCE": "1",
            "BLANK_WORKER_LEASE_SECONDS": "2",
            "BLANK_WORKER_ID": wid,
            "PYTHONPATH": src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""),
            "PYTHONUTF8": "1",
        }
    )
    env.setdefault(
        "BLANK_SECRET_KEY",
        base64.urlsafe_b64encode(b"blank-test-only-key-0123456789ab").decode(),
    )
    handle = open(log_path, "wb")
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", "-m", "blank_api.worker"],
        cwd=str(workspace),
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    proc._log_handle = handle  # type: ignore[attr-defined]
    return proc


async def _wait_for(predicate, timeout: float, interval: float = 0.2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = await predicate()
        if value:
            return value
        await asyncio.sleep(interval)
    return None


async def _run_row(run_id: str) -> AgentRun | None:
    async with get_session_factory()() as session:
        return await session.get(AgentRun, run_id)


async def _count(model, where) -> int:
    async with get_session_factory()() as session:
        return int(
            (await session.execute(select(func.count()).select_from(model).where(where))).scalar_one()
        )


async def _event_payloads(task_id: str, kind: str) -> list[dict]:
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(AgentEvent.payload).where(
                    AgentEvent.task_id == task_id, AgentEvent.type == kind
                )
            )
        ).scalars().all()
    return [dict(item or {}) for item in rows]


def _tail(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-4000:]
    except OSError:
        return "<无日志>"


async def test_a_killed_worker_is_resumed_by_another_process(tmp_path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    marker = tmp_path / "stub-blocked.marker"
    stub = _StubModel(marker)
    server = _serve(stub)
    base_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    ids = await _seed(workspace, base_url)
    log_a = tmp_path / "worker-a.log"
    log_b = tmp_path / "worker-b.log"
    proc_a = None
    proc_b = None
    try:
        proc_a = _start_worker("worker-A", workspace, log_a)

        async def _marker_seen() -> bool:
            return marker.exists()

        assert await _wait_for(_marker_seen, timeout=90), (
            "装置自检：worker-A 没走到工具结果之后的模型调用，日志：" + _tail(log_a)
        )

        # 检查点必须是被杀进程自己写下的，否则「续跑」没有可续之物。
        checkpoints = await _count(AgentCheckpoint, AgentCheckpoint.task_id == ids["task"])
        crashed = await _run_row(ids["run"])
        assert checkpoints > 0, "装置自检：杀之前库里没有检查点"
        assert (crashed.status, crashed.attempts) == ("running", 1), (
            f"装置自检：杀之前 run 应是 running/attempts=1，实际 {crashed.status}/{crashed.attempts}"
        )

        proc_a.kill()
        proc_a.wait(timeout=15)

        async def _lease_stale() -> bool:
            row = await _run_row(ids["run"])
            return row is not None and (
                row.lease_expires_at is None or row.lease_expires_at < utc_now()
            )

        assert await _wait_for(_lease_stale, timeout=30), "worker-A 死后租约没有过期，B 无法接管"

        proc_b = _start_worker("worker-B", workspace, log_b)
        try:
            rc_b = await asyncio.to_thread(proc_b.wait, 180)
        except subprocess.TimeoutExpired:
            proc_b.kill()
            pytest.fail("worker-B 在 180 秒内没有退出：" + _tail(log_b))
        assert rc_b == 0, f"worker-B 退出码 {rc_b}：" + _tail(log_b)

        final = await _run_row(ids["run"])
        async with get_session_factory()() as session:
            task = await session.get(Task, ids["task"])
        assert (final.status, final.attempts) == ("succeeded", 2), (
            f"续跑后 run 应为 succeeded/attempts=2，实际 {final.status}/{final.attempts}；日志：" + _tail(log_b)
        )
        assert task.status == "succeeded", f"任务未完成：{task.status}；日志：" + _tail(log_b)

        progress = workspace / "progress.txt"
        lines = (
            [line for line in progress.read_text(encoding="utf-8").splitlines() if line.strip()]
            if progress.exists()
            else []
        )
        assert lines == ["crash-probe"], f"工具应恰好执行一次：{lines}"

        shell_calls = await _count(
            ToolCall, (ToolCall.task_id == ids["task"]) & (ToolCall.name == "run_shell")
        )
        assert shell_calls == 1, f"run_shell 工具账应为 1 条，实际 {shell_calls}"

        resumed = await _event_payloads(ids["task"], "task.resumed")
        assert len(resumed) == 1, f"task.resumed 应恰好 1 条：{resumed}"
        assert resumed[0].get("crash_recovery") is True, f"续跑必须标成崩溃恢复：{resumed[0]}"
        assert await _count(AgentEvent, (AgentEvent.task_id == ids["task"]) & (AgentEvent.type == "task.started")) == 1, (
            "task.started 只该有一次：重新开跑会再写一条"
        )

        # 桩这边：第一次规划、崩溃点那次带工具结果、B 续跑后再带一次工具结果。
        assert len(stub.calls) >= 3, f"模型调用次数不对：{stub.calls}"
        assert stub.calls[1]["has_tool"] is True, f"第二次调用应带工具结果：{stub.calls}"
        assert stub.calls[-1]["has_tool"] is True, f"最后一次调用应带工具结果：{stub.calls}"
    finally:
        for proc in (proc_a, proc_b):
            if proc is None:
                continue
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
            handle = getattr(proc, "_log_handle", None)
            if handle is not None:
                handle.close()
        stub.release.set()
        server.shutdown()
        server.server_close()


def _marker_exists(path: Path) -> bool:
    return path.exists()
