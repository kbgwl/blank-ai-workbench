"""命令执行沙箱。

DockerSandbox 把命令放进一次性容器执行：工作区是唯一可写挂载点，默认无网络，
CPU/内存/进程数/文件大小/时长都有上限，超时后强制删除容器以清干净进程树。
Docker 是可选加固档：探测不到可用容器后端时默认退回 LocalSandbox（Job Object 强制
内存/进程数/进程树，文件与网络边界如实标注未强制）。要求"只准容器"的部署设
BLANK_SANDBOX_ALLOW_LOCAL=0，那时没有容器就拒绝执行。
"""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field

from blank_api.services.log_safety import redact
from blank_api.services.sandbox import boundary, docker_probe, windows_job
from blank_api.services.sandbox.policy import SandboxPolicy

MAX_OUTPUT_CHARS = 20000


def join_for_shell(command: str | list[str]) -> str:
    """把 argv 拼成"这层壳自己看得懂"的一行。

    本机档走的是 `create_subprocess_shell`，Windows 上就是 `cmd.exe`（由 COMSPEC 决定）。
    而 `shlex.join` 用的是 POSIX 引号规则：`E:\\app\\python.exe` 会被包成**单引号**，
    cmd 不认单引号，于是整条命令被当成一个文件名，回一句「文件名、目录名或卷标语法
    不正确」。实测（#145）：真模型跑评测样例 T01，验证命令是
    `python -m pytest -q`，解释器被换成本机 venv 的绝对路径后就撞上这条；
    而 T03 的 `npm test` 两个 token 都不需要引用，反而正常——**同一层壳，一半能跑一半不能**。
    Windows 上必须用 `subprocess.list2cmdline`，它与 CreateProcess 的解析规则一致。
    """
    if isinstance(command, str):
        return command
    if sys.platform == "win32":
        return subprocess.list2cmdline(list(command))
    return shlex.join(command)


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
    # 这一次执行**实际被什么强制**的结构化事实（Job 限额、边界强度）。
    # 界面与证据包读这个字段，不读 notes：notes 是给人看的，会被改文案；
    # 把判据建在文案上，改一句话就能把"未强制"说成"已强制"。
    enforcement: dict[str, object] = field(default_factory=dict)

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


def _decode_host(raw: bytes) -> str:
    """宿主（local 后端）子进程输出的解码。

    中文 Windows 上 cmd 与系统消息写的是 GBK：一律按 UTF-8 解码会把"配额不足，无法处理此命令。"
    这类**恰好说明限额被内核拒了**的信息变成一串替换字符——等于把最关键的证据丢掉。
    UTF-8 解不动才退回 GBK，两者都解不动才用替换字符。容器里的输出仍是 UTF-8，不走这里。
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("gbk")
    except UnicodeDecodeError:
        return raw.decode("utf-8", "replace")


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
    """本机子进程执行：内核强制的部分由 Job Object 提供，其余只是策略级约束。

    M0 探针（`evals/win_isolation_probe.py`，结论见 docs/SANDBOX-WINDOWS-DESIGN.md §2.0）定了两件事：
    - Job 的内存上限、活跃进程数上限、`KILL_ON_JOB_CLOSE` 在非管理员下**真的被内核强制**（都有阳性对照）；
    - AppContainer 入口恒 575、`CreateRestrictedToken` 恒 998、`CreateProcessWithTokenW` 缺特权 →
      **文件与网络边界在这类机器上不可能由系统强制**。
    所以这个类的职责是：把前者做真，把后者说清。它不许在 notes 里写"已隔离"。
    """

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
        script = join_for_shell(command)
        timeout = timeout_seconds or self.policy.timeout_seconds
        base = {key: value for key, value in os.environ.items() if key in self.policy.env_allowlist}
        base.update(self.policy.filter_env(env or {}))
        started = time.perf_counter()
        creationflags = 0x00000200 if sys.platform == "win32" else 0

        memory_bytes = windows_job.parse_size(self.policy.memory)
        job = windows_job.create_job(
            "blank-sandbox-" + uuid.uuid4().hex[:8],
            memory_bytes=memory_bytes,
            active_process_limit=self.policy.pids or 0,
        )
        proc = await asyncio.create_subprocess_shell(
            script,
            cwd=workspace,
            env=base,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=creationflags,
        )
        # 挂 Job 必须在起进程之后立刻做：这中间它已经派生的进程不算在这条限额里（见 assign_pid 的说明）
        assigned, assign_error = (False, job.error) if not job.ok else windows_job.assign_pid(job, proc.pid)
        # 必须在 close_job 之前算：关句柄会把 job.handle 置 0，之后再读 Job 就永远是"没建成"，
        # 于是这条命令明明被内核限着 48MB，账上却写成"限额未强制"——反向说谎同样是谎。
        # 变异验证：把这一行挪到 close_job 之后，本文件三条 enforcement 断言立刻变红。
        enforcement = _enforcement(job, assigned, assign_error)
        timed_out = False
        try:
            raw_out, raw_err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            timed_out = True
            await kill_process_tree(proc.pid)
            raw_out, raw_err = b"", "命令超过时间限制，进程树已被终止".encode()
        finally:
            # 关句柄 = 沙箱生命周期结束。挂了 KILL_ON_JOB_CLOSE 时，还活着的整棵树一起走；
            # M0 的 P6.7 证明这条在内核层生效，对照（不挂 Job）的同种进程则活得好好的。
            windows_job.close_job(job)
        duration = int((time.perf_counter() - started) * 1000)
        stdout, trunc_out = _clip(_decode_host(raw_out))
        stderr, trunc_err = _clip(_decode_host(raw_err))
        return SandboxResult(
            command=redact(script),
            exit_code=None if timed_out else proc.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration,
            timed_out=timed_out,
            backend=self.name,
            truncated=trunc_out or trunc_err,
            notes=_enforcement_notes(enforcement),
            enforcement=enforcement,
        )

    async def cleanup(self) -> int:
        # Job 句柄在每次 run 的 finally 里已经关掉，进程树不会跨任务存活。
        # 这里返回 0 是事实，不是"还没实现"：跨进程残留需要 runner 化之后才谈得上枚举。
        return 0


def limits_not_applied(reason: str) -> dict[str, object]:
    """沙箱开关为关（命令直接跑在宿主进程上）时的账：每一项都写 False，并写清为什么。

    这条是给 `off` 档用的。没有它，界面只能拿 policy 里的配置值当生效值展示——
    设计稿 §1 里"展示值冒充生效值"那一条就是这么产生的。键集合与 `_enforcement` 保持一致，
    读的一方不用分两种形状。
    """
    cap = boundary.measure()
    return {
        "boundary_strength": cap.strength,
        "boundary_strength_label": cap.label,
        "boundary_measured_seconds_ago": cap.as_dict().get("age_seconds"),
        "boundary_apparatus_ok": cap.apparatus_ok,
        "limits_applied": False,
        "job_created": False,
        "job_assigned": False,
        "job_error": reason,
        "memory_enforced": False,
        "memory_bytes": 0,
        "pids_enforced": False,
        "pids_limit": 0,
        "tree_kill_enforced": False,
        "filesystem_enforced": False,
        "network_enforced": False,
        "cpu_enforced": False,
        "disk_enforced": False,
    }


def _enforcement(job: windows_job.Job, assigned: bool, assign_error: str) -> dict[str, object]:
    """把"哪几条真的被内核强制了"算成结构化事实。测不出来就写 unknown，不许默认成真。"""
    cap = boundary.measure()
    enforced = bool(job.ok and assigned)
    return {
        "boundary_strength": cap.strength,
        "boundary_strength_label": cap.label,
        "boundary_measured_seconds_ago": cap.as_dict().get("age_seconds"),
        "boundary_apparatus_ok": cap.apparatus_ok,
        "limits_applied": True,
        "job_created": job.ok,
        "job_assigned": assigned,
        "job_error": job.error or assign_error or "",
        "memory_enforced": enforced and "memory_bytes" in job.enforced,
        "memory_bytes": int(job.enforced.get("memory_bytes", 0)),
        "pids_enforced": enforced and "active_process_limit" in job.enforced,
        "pids_limit": int(job.enforced.get("active_process_limit", 0)),
        "tree_kill_enforced": enforced and "kill_on_close" in job.enforced,
        # 这两条由 M0 直接定死：拿不到 AC/受限令牌的机器上，OS 不会替我们拒文件与网络。
        "filesystem_enforced": cap.strength == "os_enforced",
        "network_enforced": cap.strength == "os_enforced",
        "cpu_enforced": False,
        "disk_enforced": False,
    }


def _enforcement_notes(enforcement: dict[str, object]) -> list[str]:
    out: list[str] = []
    if enforcement["memory_enforced"] and enforcement["pids_enforced"]:
        out.append("内存上限 {}MB、活跃进程上限 {} 由 Windows Job Object 强制（内核级，非我们数着玩）".format(
            round(int(enforcement["memory_bytes"]) / 1024 / 1024, 1), enforcement["pids_limit"]))
    elif enforcement["job_created"] and not enforcement["job_assigned"]:
        out.append("限额没生效：子进程没能挂进 Job（{}）".format(enforcement["job_error"]))
    else:
        out.append("限额未由内核强制：{}".format(enforcement["job_error"] or "本平台无 Job Object"))
    if enforcement["tree_kill_enforced"]:
        out.append("命令结束时整棵进程树被 Job 带走（KILL_ON_JOB_CLOSE 已生效）")
    if not enforcement["filesystem_enforced"]:
        out.append("文件边界未由系统强制：模型把路径藏在脚本字符串里时 OS 不会拒（边界强度={}）".format(
            enforcement["boundary_strength"]))
    if not enforcement["network_enforced"]:
        out.append("网络边界未由系统强制：默认全部放行，靠审批与审计")
    return out


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
    """按策略返回沙箱实现；生产配置不会静默降级到宿主机。

    容器后端能不能用由现场探测决定，不看 PATH 里有没有那个文件：装了 Docker 但守护进程
    没起来时返回 DockerSandbox，只会让每条命令各自撞一次墙（`docs/COST.md` 记的那条）。
    探测不到可用容器就退 LocalSandbox（产品默认）；`allow_local=False` 才是"只准容器"的
    部署选择，那种情况下没有容器只能拒绝。
    """
    chosen = policy or SandboxPolicy()
    if not chosen.enabled:
        return LocalSandbox(chosen)
    info = docker_probe.probe_for_decision()
    if info.usable:
        return DockerSandbox(chosen)
    if info.state == docker_probe.STATE_PENDING:
        # 还没探过（进程刚起来的瞬间、或探针没跑到的入口）。退回老口径：PATH 里有 docker
        # 就当容器后端——宁可少拦一次，也不要在没测过的情况下宣称"不可用"而拦住正常机器。
        if shutil_which("docker") is not None:
            return DockerSandbox(chosen)
    if chosen.allow_local:
        return LocalSandbox(chosen)
    raise SandboxUnavailable(
        "Docker 沙箱不可用"
        + docker_probe.notice_suffix(info)
        + "；本部署要求容器隔离（BLANK_SANDBOX_ALLOW_LOCAL=0）；"
        "如需本机受限额执行，请设置 BLANK_SANDBOX_ALLOW_LOCAL=1 或删除该变量"
    )


def shutil_which(name: str) -> str | None:
    from shutil import which

    return which(name)
