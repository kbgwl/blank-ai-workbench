"""命令执行沙箱。

DockerSandbox 把命令放进一次性容器执行：工作区是唯一可写挂载点，默认无网络，
CPU/内存/进程数/文件大小/时长都有上限，超时后强制删除容器以清干净进程树。
Docker 不可用时默认拒绝执行；只有显式开启 SANDBOX_ALLOW_LOCAL 才允许本地开发回退。
"""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
import time
import uuid
from dataclasses import dataclass, field

from blank_api.services.log_safety import redact
from blank_api.services.sandbox.policy import SandboxPolicy

MAX_OUTPUT_CHARS = 20000


class SandboxUnavailable(RuntimeError):
    """The configured isolated execution backend is not available."""


@dataclass(slots=True)
class SandboxResult:
    command: str
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    backend: str
    truncated: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def output(self) -> str:
        text = self.stdout if not self.stderr else f"{self.stdout}\n{self.stderr}".strip()
        return redact(text)


def _clip(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text, False
    return text[-MAX_OUTPUT_CHARS:] + "\n…（输出已截断）", True


class DockerSandbox:
    name = "docker"

    def __init__(self, policy: SandboxPolicy | None = None) -> None:
        self.policy = policy or SandboxPolicy()

    async def run(
        self,
        command: str | list[str],
        *,
        workspace: str,
        env: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> SandboxResult:
        script = command if isinstance(command, str) else shlex.join(command)
        timeout = timeout_seconds or self.policy.timeout_seconds
        container = f"blank-sandbox-{uuid.uuid4().hex[:8]}"
        flags = self.policy.docker_flags(container, os.fspath(workspace))
        for key, value in self.policy.filter_env(env or {}).items():
            flags += ["--env", f"{key}={value}"]
        flags += [self.policy.image, "sh", "-c", script]

        started = time.perf_counter()
        proc = await asyncio.create_subprocess_exec(
            *flags,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={key: value for key, value in os.environ.items() if key.startswith("DOCKER_")},
        )
        timed_out = False
        try:
            raw_out, raw_err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            timed_out = True
            await self._force_remove(container)
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            killed_out, killed_err = await proc.communicate()
            raw_out = killed_out
            raw_err = (killed_err.decode("utf-8", "replace") + "\n命令超过时间限制，容器已被强制删除").encode()
        duration = int((time.perf_counter() - started) * 1000)

        stdout, trunc_out = _clip(raw_out.decode("utf-8", "replace"))
        stderr, trunc_err = _clip(raw_err.decode("utf-8", "replace"))
        notes = [f"沙箱超时上限 {timeout:g}s"] if timed_out else []
        return SandboxResult(
            command=redact(script),
            exit_code=None if timed_out else proc.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration,
            timed_out=timed_out,
            backend=self.name,
            truncated=trunc_out or trunc_err,
            notes=notes,
        )

    async def _force_remove(self, container: str) -> None:
        await docker_rm(container)

    async def cleanup(self) -> int:
        """删除本工具遗留的沙箱容器，返回清理数量；用于验收「任务结束后不存在残留进程」。"""
        return await cleanup_containers()


async def docker_rm(container: str) -> None:
    proc = await asyncio.create_subprocess_exec("docker", "rm", "-f", container,
                                               stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await proc.wait()


async def cleanup_containers() -> int:
    proc = await asyncio.create_subprocess_exec(
        "docker", "ps", "-q", "--filter", "label=blank.sandbox=1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    raw, _ = await proc.communicate()
    ids = [line.strip() for line in raw.decode().splitlines() if line.strip()]
    for container_id in ids:
        await docker_rm(container_id)
    return len(ids)


class LocalSandbox:
    """无 Docker 时的退路：直接在本机子进程执行，只做时长与进程树清理，不做隔离。"""

    name = "local"

    def __init__(self, policy: SandboxPolicy | None = None) -> None:
        self.policy = policy or SandboxPolicy()

    async def run(
        self,
        command: str | list[str],
        *,
        workspace: str,
        env: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> SandboxResult:
        script = command if isinstance(command, str) else shlex.join(command)
        timeout = timeout_seconds or self.policy.timeout_seconds
        base = {key: value for key, value in os.environ.items() if key in self.policy.env_allowlist}
        base.update(self.policy.filter_env(env or {}))
        started = time.perf_counter()
        creationflags = 0x00000200 if sys.platform == "win32" else 0
        proc = await asyncio.create_subprocess_shell(
            script,
            cwd=workspace,
            env=base,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=creationflags,
        )
        timed_out = False
        try:
            raw_out, raw_err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            timed_out = True
            await kill_process_tree(proc.pid)
            raw_out, raw_err = b"", "命令超过时间限制，进程树已被终止".encode()
        duration = int((time.perf_counter() - started) * 1000)
        stdout, trunc_out = _clip(raw_out.decode("utf-8", "replace"))
        stderr, trunc_err = _clip(raw_err.decode("utf-8", "replace"))
        return SandboxResult(
            command=redact(script),
            exit_code=None if timed_out else proc.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration,
            timed_out=timed_out,
            backend=self.name,
            truncated=trunc_out or trunc_err,
            notes=["本地模式无隔离，仅用于开发机"],
        )

    async def cleanup(self) -> int:
        return 0


async def kill_process_tree(pid: int) -> None:
    if sys.platform == "win32":
        proc = await asyncio.create_subprocess_exec(
            "taskkill", "/PID", str(pid), "/T", "/F",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()
        return
    import signal

    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def get_sandbox(policy: SandboxPolicy | None = None):
    """按策略返回沙箱实现；生产配置不会静默降级到宿主机。"""
    chosen = policy or SandboxPolicy()
    if not chosen.enabled:
        return LocalSandbox(chosen)
    if shutil_which("docker") is None:
        if chosen.allow_local:
            return LocalSandbox(chosen)
        raise SandboxUnavailable("Docker 沙箱不可用；如需开发机直连执行，请显式设置 BLANK_SANDBOX_ALLOW_LOCAL=1")
    return DockerSandbox(chosen)


def shutil_which(name: str) -> str | None:
    from shutil import which

    return which(name)
