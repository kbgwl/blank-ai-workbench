"""Docker 后端到底能不能用：现场探一次，而不是看 PATH 里有没有那个文件。

立项理由记在 `docs/COST.md`：老口径是 `shutil.which("docker")`，它只回答"这个文件名在不在
PATH 里"。装了 Docker Desktop 但守护进程没起来是常态，那种机器上老口径一路把容器说成可用，
直到每条命令各自撞墙。这里把三种读数分开：未安装、已安装但守护进程没应答、真的能用。

三条铁律沿用 `services/sandbox/boundary.py`——同一类装置，同一套纪律：

1. **每个判定带阳性对照**。对照组就是"用一个已知能起的进程证明 spawn 这条路本身是通的"。
   对照失败 ⇒ 读数只能是 `probe_failed`，绝不许写 `not_installed` 或 `daemon_unreachable`：
   探针自己不可信的时候，"这台机器没装 docker"是一个假事实。
2. **测不到就降级，不猜**。读数不是 `ready` 就不许当容器后端用；`rc == 0` 也不算数，
   必须真的看见守护进程版本那一行——否则一个立刻退 0 的空壳也能把系统说成有容器。
3. **结论带时间戳**。缓存读数必须报得出 `age_seconds`，界面上不许把旧值当现值。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any

STATE_READY = "ready"
STATE_NOT_INSTALLED = "not_installed"
STATE_DAEMON_UNREACHABLE = "daemon_unreachable"
STATE_TIMEOUT = "timeout"
STATE_PROBE_FAILED = "probe_failed"
STATE_PENDING = "pending"

STATES = (
    STATE_READY,
    STATE_NOT_INSTALLED,
    STATE_DAEMON_UNREACHABLE,
    STATE_TIMEOUT,
    STATE_PROBE_FAILED,
    STATE_PENDING,
)

_STATE_LABEL = {
    STATE_READY: "容器后端可用（docker 守护进程有应答）",
    STATE_NOT_INSTALLED: "未安装：PATH 里没有 docker 可执行文件",
    STATE_DAEMON_UNREACHABLE: "已安装，但守护进程没有应答",
    STATE_TIMEOUT: "已安装，但探测超时",
    STATE_PROBE_FAILED: "探测装置自身不可信，不承诺容器可用",
    STATE_PENDING: "尚未探测",
}

# `docker version` 输出里守护进程那一段的标志行。只看退出码不够：客户端段也会退 0。
SERVER_MARKER = "Server Version"

PROBE_TIMEOUT_SECONDS = 5.0
CACHE_TTL_SECONDS = 60.0

_CREATE_NO_WINDOW = 0x08000000
_TIMEOUT_MARK = "__probe_timeout__"


def state_label(state: str) -> str:
    """文案只在这张表里定义一次，界面与 notice 都从这里取。"""
    return _STATE_LABEL.get(state, state)


@dataclass(slots=True)
class DockerProbe:
    """一次探测的全部事实。字段就是证据，别在 UI 里重新推导一遍。"""

    state: str = STATE_PENDING
    detail: str = ""
    exit_code: int | None = None
    command: str = ""
    control_ok: bool = True
    timeout_seconds: float = PROBE_TIMEOUT_SECONDS
    measured_at: float = 0.0

    @property
    def usable(self) -> bool:
        return self.state == STATE_READY

    @property
    def known_unavailable(self) -> bool:
        """探到过事实（区别于"还没探"）：装配层据此拒绝任务并念出成因。"""
        return self.state in (
            STATE_NOT_INSTALLED,
            STATE_DAEMON_UNREACHABLE,
            STATE_TIMEOUT,
            STATE_PROBE_FAILED,
        )

    @property
    def label(self) -> str:
        return state_label(self.state)

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["label"] = self.label
        out["usable"] = self.usable
        out["known_unavailable"] = self.known_unavailable
        out["age_seconds"] = round(time.time() - self.measured_at, 1) if self.measured_at else None
        return out


def _decode(raw: bytes) -> str:
    """中文 Windows 上 docker/系统消息按 GBK 写：一律 UTF-8 解码会把成因丢掉。"""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("gbk")
    except UnicodeDecodeError:
        return raw.decode("utf-8", "replace")


def _kill_tree(proc: subprocess.Popen[str]) -> None:
    """超时要真的把进程带走。

    `subprocess.run(timeout=…)` 超时时只杀直接子进程，然后照样等管道关——Windows 上
    `docker.bat` 这种"cmd 再拉一个孙进程"的载体就会白等到孙进程跑完。
    实测同一份桩（`ping -n 5` ≈4 秒）配 0.4 秒闸门：那种写法 3 次都是 4.06–4.08s，
    杀整棵树 3 次都是 0.63–0.65s。探测跑在线程里，多等的每一秒都是白占一个线程。
    """
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=2.0,
                encoding="gbk",
                errors="replace",
            )
        except Exception:  # noqa: BLE001 - 杀不掉也只能如实返回超时，不能让探针自己抛
            pass
    else:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
    try:
        proc.stdout.close()
        proc.stderr.close()
    except Exception:  # noqa: BLE001
        pass


def _spawn(argv: list[str], timeout_seconds: float) -> tuple[int | None, str, str, str]:
    """起一个进程并等它回话。返回 (退出码, stdout, stderr, 装置错误)。

    起不来与"起来了但拒了"是两种事实：前者是装置坏了（error 非空），后者是现场读数。
    """
    flags = _CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            creationflags=flags,
        )
    except Exception as exc:  # noqa: BLE001 - spawn 失败是装置事实，不是可恢复错误
        return None, "", "", f"{type(exc).__name__}: {exc}"
    try:
        raw_out, raw_err = proc.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        return None, "", "", _TIMEOUT_MARK
    except Exception as exc:  # noqa: BLE001
        _kill_tree(proc)
        return None, "", "", f"{type(exc).__name__}: {exc}"
    return proc.returncode, _decode(raw_out or b""), _decode(raw_err or b""), ""


def _first_line(text: str, limit: int = 200) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()[:limit]
    return ""


def probe_once(*, timeout_seconds: float = PROBE_TIMEOUT_SECONDS) -> DockerProbe:
    """探一次现场：对照进程 + PATH 里的 docker + 守护进程应答。"""
    control_rc, _, _, control_error = _spawn([sys.executable, "-c", "pass"], timeout_seconds)
    control_ok = control_rc == 0

    binary = shutil.which("docker")
    if binary is None:
        state = STATE_NOT_INSTALLED if control_ok else STATE_PROBE_FAILED
        detail = (
            "PATH 里找不到 docker 可执行文件"
            if control_ok
            else f"连对照进程都起不来（{control_error or f'rc={control_rc}'}），"
            "所以「没找到 docker」这个结论不可信"
        )
        return DockerProbe(
            state=state, detail=detail, exit_code=None, command="",
            control_ok=control_ok, timeout_seconds=timeout_seconds, measured_at=time.time(),
        )

    argv = [binary, "version"]
    rc, out, err, error = _spawn(argv, timeout_seconds)
    if error == _TIMEOUT_MARK:
        state, detail = STATE_TIMEOUT, f"docker version 超过 {timeout_seconds}s 没回话"
    elif error:
        state, detail = STATE_PROBE_FAILED, f"docker 这个可执行文件起不来：{error}"
    elif rc == 0 and SERVER_MARKER in out:
        state, detail = STATE_READY, _first_line(f"{SERVER_MARKER}{_server_line_tail(out)}")
    elif rc == 0:
        state, detail = (
            STATE_DAEMON_UNREACHABLE,
            "docker version 退 0 但输出里没有守护进程版本那一行——守护进程应答不成立",
        )
    else:
        state = STATE_DAEMON_UNREACHABLE
        # 成因优先取 stderr（docker 的报错写在那里）；空了才退回 stdout，再空才说"非零退出"。
        detail = _first_line(err) or _first_line(out) or "docker version 非零退出且无任何输出"

    if not control_ok:
        # 铁律 1：对照失败，任何"事实"读数都不能留下。
        detail = f"对照进程起不来（{control_error or f'rc={control_rc}'}），本次读数不作事实；原读数为「{detail}」"
        state = STATE_PROBE_FAILED

    return DockerProbe(
        state=state, detail=detail, exit_code=rc, command=" ".join(argv),
        control_ok=control_ok, timeout_seconds=timeout_seconds, measured_at=time.time(),
    )


def _server_line_tail(stdout: str) -> str:
    for line in (stdout or "").splitlines():
        if SERVER_MARKER in line:
            return line.split(SERVER_MARKER, 1)[1]
    return ""


_LOCK = threading.Lock()
_CACHE: tuple[float, DockerProbe] | None = None


def _in_running_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def probe(*, force: bool = False, ttl: float = CACHE_TTL_SECONDS,
          timeout_seconds: float = PROBE_TIMEOUT_SECONDS) -> DockerProbe:
    """带 TTL 的现场探测。缓存命中也要把 `age_seconds` 报出去，别把旧值当现值。"""
    global _CACHE
    with _LOCK:
        now = time.time()
        if _CACHE is not None and not force and now - _CACHE[0] < ttl:
            return _CACHE[1]
        result = probe_once(timeout_seconds=timeout_seconds)
        _CACHE = (time.time(), result)
        return result


def peek() -> DockerProbe | None:
    """只读缓存，绝不起进程。"""
    return _CACHE[1] if _CACHE else None


def probe_for_decision(*, ttl: float = CACHE_TTL_SECONDS) -> DockerProbe:
    """同步决策点用的读数：新鲜就返缓存；过期就重探——**但在事件循环里绝不重探**。

    在循环里同步起子进程会独占事件循环（`search_code` 那次把整个后端占死就是这一类），
    所以循环里只读缓存：没有缓存就返 `pending`，由启动任务与装配层补探。
    """
    cached = peek()
    if cached is not None:
        fresh = time.time() - (_CACHE[0] if _CACHE else 0.0) < ttl
        if fresh or _in_running_loop():
            return cached
    if _in_running_loop():
        return DockerProbe(state=STATE_PENDING, detail="还没探过；启动任务与任务装配层会补探")
    return probe(ttl=ttl)


async def refresh(*, force: bool = True, timeout_seconds: float = PROBE_TIMEOUT_SECONDS) -> DockerProbe:
    """异步补探：真起进程的那一下放到线程里，事件循环继续服务别的请求。"""
    return await asyncio.to_thread(probe, force=force, timeout_seconds=timeout_seconds)


async def ensure_fresh(*, ttl: float = CACHE_TTL_SECONDS,
                       timeout_seconds: float = PROBE_TIMEOUT_SECONDS) -> DockerProbe:
    """任务装配前保证读数不是"还没探过"：新鲜就返缓存，过期才起进程。

    每个任务都重探一次要付一条 `docker version`，而这件事一台机器上问一次就够；
    但装配层不能拿着 `pending` 做拒绝/放行的决定，所以这里补上那一次。
    """
    cached = peek()
    age = time.time() - _CACHE[0] if cached is not None and _CACHE else None
    if age is not None and age < ttl:
        return cached
    return await asyncio.to_thread(probe, timeout_seconds=timeout_seconds)


def invalidate() -> None:
    global _CACHE
    with _LOCK:
        _CACHE = None


def notice_suffix(current: DockerProbe | None = None) -> str:
    """拒绝任务时念得出的成因；文案口径与界面上那行读数必须同源。"""
    info = current if current is not None else probe_for_decision()
    return f"（后端探测读数：{state_label(info.state)}｜{info.detail}）"


__all__ = [
    "CACHE_TTL_SECONDS",
    "PROBE_TIMEOUT_SECONDS",
    "SERVER_MARKER",
    "STATE_DAEMON_UNREACHABLE",
    "STATE_NOT_INSTALLED",
    "STATE_PENDING",
    "STATE_PROBE_FAILED",
    "STATE_READY",
    "STATE_TIMEOUT",
    "STATES",
    "DockerProbe",
    "ensure_fresh",
    "invalidate",
    "notice_suffix",
    "peek",
    "probe",
    "probe_for_decision",
    "probe_once",
    "refresh",
    "state_label",
]
