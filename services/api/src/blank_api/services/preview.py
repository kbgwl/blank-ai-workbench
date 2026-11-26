# -*- coding: utf-8 -*-
"""工作区应用预览：按需启动一个长驻的 ``node <file>`` 服务。

与 ``ui_smoke`` 的区别是生命周期：冒烟跑完就杀，预览要活到用户点停止或
TTL 到期（默认 10 分钟）。安全边界与冒烟一致，并额外做了三件事：

* **只认 ``package.json`` 的 ``scripts.start`` 且必须是 ``node <文件>``**；
  不执行 npm、不跑任意 shell；
* **只绑 127.0.0.1 + 随机端口**，环境变量走白名单，密钥类变量一律不进；
* **挂 Windows Job Object**（内存/进程数/退出清理由内核强制），停止时先关 Job
  再按进程树兜底；部署要求"只准容器"（``BLANK_SANDBOX_ALLOW_LOCAL=0``）时
  直接拒绝，不退回宿主裸跑。
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from blank_api.services.agent.ui_smoke import _free_port, _start_script, _wait_http
from blank_api.services.sandbox import windows_job
from blank_api.services.sandbox.policy import SandboxPolicy
from blank_api.services.sandbox.runner import kill_process_tree
from blank_api.services.tools.browser import node_exe

PREVIEW_TTL_SECONDS = 600
STARTUP_TIMEOUT = 12.0
LOG_TAIL_CHARS = 1200


class PreviewError(RuntimeError):
    """启动/停止预览失败。``code`` 直接进 API 错误响应。"""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass
class PreviewSession:
    project_id: str
    root: Path
    script: Path
    port: int
    url: str
    proc: asyncio.subprocess.Process
    started_at: float
    expires_at: float
    isolation: str
    notes: list[str] = field(default_factory=list)
    job: object | None = None
    log_path: Path = Path()
    expire_task: asyncio.Task | None = None


def _log_tail(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-LOG_TAIL_CHARS:].strip()


def _clean_env(port: int) -> dict[str, str]:
    """宿主进程只带运行必需品与回环端口；任何 KEY/TOKEN/SECRET 都别想进去。"""
    keep = ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE", "COMSPEC", "PATHEXT", "LANG", "LC_ALL")
    env = {key: value for key, value in os.environ.items() if key.upper() in keep}
    env.update({"PORT": str(port), "HOST": "127.0.0.1", "NODE_ENV": "test"})
    return env


class PreviewManager:
    def __init__(self, policy: SandboxPolicy | None = None) -> None:
        self._policy = policy or SandboxPolicy()
        self._sessions: dict[str, PreviewSession] = {}
        self._lock = asyncio.Lock()

    async def start(self, project_id: str, root: str | Path) -> dict:
        async with self._lock:
            existing = self._sessions.get(project_id)
            if existing is not None:
                if existing.proc.returncode is None:
                    return self._out(existing)
                await self._stop_locked(project_id)

            if not self._policy.allow_local:
                raise PreviewError(
                    "preview_isolation_unavailable",
                    "部署要求只准容器执行；预览会话暂不支持容器长驻，已拒绝启动，不会退回宿主裸跑。",
                    409,
                )

            root_path = Path(root).resolve()
            script = _start_script(root_path)
            if script is None:
                raise PreviewError(
                    "preview_start_not_supported",
                    "只支持 package.json 的 scripts.start 为 `node <文件>` 的项目；不执行 npm 或任意命令。",
                    400,
                )
            node = node_exe()
            if not node:
                raise PreviewError("preview_node_missing", "本机没有可用的 node，无法启动预览。", 503)

            port = _free_port()
            log_dir = root_path / ".blank"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / "preview.log"
            creationflags = 0x00000200 if sys.platform == "win32" else 0
            with open(log_path, "wb") as log:
                proc = await asyncio.create_subprocess_exec(
                    node,
                    str(script),
                    cwd=str(root_path),
                    env=_clean_env(port),
                    stdout=log,
                    stderr=asyncio.subprocess.STDOUT,
                    creationflags=creationflags,
                )

            notes: list[str] = []
            job = None
            try:
                job = windows_job.create_job(
                    "blank-preview-" + uuid.uuid4().hex[:8],
                    memory_bytes=windows_job.parse_size(self._policy.memory),
                    active_process_limit=self._policy.pids or 0,
                    cpu_cpus=self._policy.cpus,
                    io_bps=self._policy.io_bps,
                )
                if job.ok:
                    assigned, assign_error = windows_job.assign_pid(job, proc.pid)
                    if not assigned:
                        notes.append(f"Job Object 未挂上：{assign_error or '未知原因'}")
                else:
                    notes.append(f"Job Object 不可用：{job.error or '未知原因'}")
                    job = None
            except Exception as exc:  # noqa: BLE001 - Job 失败只是少一层内核限额，不该让预览起不来
                notes.append(f"Job Object 初始化失败：{exc}")
                job = None

            loop = asyncio.get_running_loop()
            if not await _wait_http(port, loop.time() + STARTUP_TIMEOUT):
                await self._kill(proc, job)
                tail = _log_tail(log_path)
                raise PreviewError(
                    "preview_start_failed",
                    f"服务在 {STARTUP_TIMEOUT:.0f} 秒内没有监听 127.0.0.1:{port}。日志尾部：{tail or '（无输出）'}",
                    502,
                )

            started = time.time()
            session = PreviewSession(
                project_id=project_id,
                root=root_path,
                script=script,
                port=port,
                url=f"http://127.0.0.1:{port}/",
                proc=proc,
                started_at=started,
                expires_at=started + PREVIEW_TTL_SECONDS,
                isolation="local-job" if job is not None else "local",
                notes=notes
                + [
                    "只绑 127.0.0.1；内存/进程数/生命周期由 Windows Job Object 强制。",
                    "文件与网络边界未由系统强制；预览服务以当前用户权限运行。",
                ],
                job=job,
                log_path=log_path,
            )
            self._sessions[project_id] = session
            session.expire_task = asyncio.create_task(self._expire(project_id, PREVIEW_TTL_SECONDS))
            return self._out(session)

    async def status(self, project_id: str) -> dict:
        session = self._sessions.get(project_id)
        if session is None:
            return {"status": "stopped"}
        if session.proc.returncode is not None:
            exit_code = session.proc.returncode
            tail = _log_tail(session.log_path)
            await self._stop_locked(project_id)
            return {"status": "stopped", "exit_code": exit_code, "log_tail": tail}
        return self._out(session)

    async def stop(self, project_id: str) -> dict:
        async with self._lock:
            session = await self._stop_locked(project_id)
        if session is None:
            return {"status": "stopped"}
        return {"status": "stopped", "log_tail": _log_tail(session.log_path)}

    async def shutdown(self) -> None:
        for project_id in list(self._sessions):
            await self.stop(project_id)

    async def _stop_locked(self, project_id: str) -> PreviewSession | None:
        session = self._sessions.pop(project_id, None)
        if session is None:
            return None
        if session.expire_task is not None:
            session.expire_task.cancel()
            session.expire_task = None
        await self._kill(session.proc, session.job)
        return session

    async def _kill(self, proc: asyncio.subprocess.Process, job: object | None) -> None:
        if job is not None:
            try:
                windows_job.close_job(job)
            except Exception:  # noqa: BLE001 - 关句柄失败还有 taskkill 兜底
                pass
        if proc.returncode is None:
            try:
                await kill_process_tree(proc.pid)
            except Exception:  # noqa: BLE001 - 进程可能已自行退出
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except (asyncio.TimeoutError, ProcessLookupError):
                pass

    async def _expire(self, project_id: str, seconds: float) -> None:
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return
        await self.stop(project_id)

    def _out(self, session: PreviewSession) -> dict:
        return {
            "status": "running",
            "url": session.url,
            "port": session.port,
            "started_at": datetime.fromtimestamp(session.started_at, timezone.utc).isoformat(),
            "expires_at": datetime.fromtimestamp(session.expires_at, timezone.utc).isoformat(),
            "isolation": session.isolation,
            "notes": session.notes,
            "log_tail": _log_tail(session.log_path) if session.proc.returncode is not None else "",
        }


preview_manager = PreviewManager()
