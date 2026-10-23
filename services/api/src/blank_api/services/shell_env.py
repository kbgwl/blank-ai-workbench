"""Shell snapshot and environment policy.

Claude Code records a shell snapshot per thread so that a later command runs in
the same environment the model *saw* when it planned the command — same cwd,
same PATH, same virtualenv. Without it the model reads `python -V` output once
and then silently gets a different interpreter two steps later.

We keep the same shape but bound it:

* only an allowlist of variables is captured (never the whole environment),
* anything whose *name* looks like a credential is dropped even if allowlisted,
* the project virtualenv is detected and placed at the front of PATH,
* three explicit policies so an operator can narrow further.

The snapshot is inert data: it is recorded as an audit event and handed to
``run_shell`` as the child environment. Nothing here executes anything.
"""

from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from blank_api.services.workspace import mask_path

POLICY_INHERIT = "inherit"
POLICY_MINIMAL = "minimal"
POLICY_STRICT = "strict"
POLICIES = (POLICY_INHERIT, POLICY_MINIMAL, POLICY_STRICT)

# 最小可用集合：够跑 python/node/git，同时不把宿主机私货带进子进程。
MINIMAL_KEYS = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "COMSPEC",
    "TEMP",
    "TMP",
    "HOME",
    "USERPROFILE",
    "LANG",
    "LC_ALL",
    "PYTHONIOENCODING",
    "PYTHONPATH",
    "NODE_OPTIONS",
    "VIRTUAL_ENV",
)

# strict：只要 PATH 与语言运行时必需项，不继承任何用户自定义变量。
STRICT_KEYS = ("PATH", "PATHEXT", "SYSTEMROOT", "COMSPEC", "TEMP", "TMP")

# 名字像密钥就一律丢弃——allowlist 也会被误配，这里是最后一道闸。
SECRET_NAME = re.compile(
    r"(?i)(api[_-]?key|secret|token|passw|credential|private[_-]?key|session[_-]?key|cookie|auth)"
)
SECRET_VALUE = re.compile(r"(?i)^(?:sk-|ghp_|gho_|xox[baprs]-|akia|eyJ)[A-Za-z0-9_\-\.]{8,}")

MAX_PATH_ENTRIES = 40


def _policy_from_env(env: dict[str, str] | None = None) -> str:
    raw = str((env or os.environ).get("BLANK_SHELL_ENV_POLICY") or "").strip().lower()
    return raw if raw in POLICIES else POLICY_MINIMAL


def _looks_secret(name: str, value: str) -> bool:
    if SECRET_NAME.search(name):
        return True
    return bool(value and SECRET_VALUE.match(value))


def filter_env(env: dict[str, str], *, policy: str = POLICY_MINIMAL) -> dict[str, str]:
    """Project an environment down to the policy's allowlist, minus credentials."""
    if policy == POLICY_INHERIT:
        return {key: value for key, value in env.items() if value and not _looks_secret(key, value)}
    keep = STRICT_KEYS if policy == POLICY_STRICT else MINIMAL_KEYS
    return {
        key: env[key]
        for key in keep
        if env.get(key) and not _looks_secret(key, env[key])
    }


def detect_venv(root: Path) -> str:
    """Return the interpreter path of a project virtualenv, or ''."""
    for relative in (
        Path(".venv") / "Scripts" / "python.exe",
        Path(".venv") / "bin" / "python",
        Path("venv") / "Scripts" / "python.exe",
        Path("venv") / "bin" / "python",
    ):
        candidate = root / relative
        if candidate.exists():
            return str(candidate)
    return ""


def venv_bin_dir(interpreter: str) -> str:
    if not interpreter:
        return ""
    return str(Path(interpreter).parent)


@dataclass(frozen=True)
class ShellSnapshot:
    """Environment a thread plans against; recorded once per run."""

    cwd: str
    policy: str = POLICY_MINIMAL
    shell: str = ""
    python: str = ""
    venv: str = ""
    path_entries: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    created_at: str = ""
    # True 表示 venv 被前置进了 PATH，命令里的 python 会解析到项目解释器
    venv_pinned: bool = False

    def env_for(self, policy: str | None = None) -> dict[str, str]:
        """Child-process environment. `policy` may narrow, never widen.

        Widening would be a privilege escalation: a run started under `strict`
        must not gain variables just because a tool asked for `inherit`.
        """
        chosen = self.policy if not policy else policy
        order = {name: index for index, name in enumerate(POLICIES)}
        if order.get(chosen, 0) < order.get(self.policy, 0):
            chosen = self.policy
        env = dict(self.env)
        if chosen != self.policy:
            env = filter_env({**self.env, **os.environ}, policy=chosen)
        return env

    def as_dict(self) -> dict:
        """审计视图：不给变量值，也不给主机绝对路径。

        这份快照进事件流、证据包和全文索引。一条 Windows 开发机的 PATH 有二十几条，
        里面全是 `C:\\Users\\<用户名>\\...` 这种目录——原样入账等于把操作者的装机结构和
        用户名广播给所有能看到这个任务的人。掩码只有一处口径：`workspace.mask_path`
        （和治理快照、指令层引用共用同一个函数）；本机要看真路径就打开 expose_workspace_paths。
        """
        return {
            "cwd": mask_path(self.cwd),
            "policy": self.policy,
            "shell": mask_path(self.shell) if self.shell else "",
            "python": mask_path(self.python) if self.python else "",
            "venv": mask_path(self.venv) if self.venv else "",
            "venv_pinned": self.venv_pinned,
            "path_entries": [mask_path(item) for item in self.path_entries],
            "env_names": sorted(self.env),
            "created_at": self.created_at,
        }


def capture(
    root: str | Path,
    *,
    env: dict[str, str] | None = None,
    policy: str | None = None,
) -> ShellSnapshot:
    """Take a snapshot of the environment commands will actually run in."""
    source = dict(env if env is not None else os.environ)
    chosen = policy or _policy_from_env(source)
    workspace = Path(root).resolve()
    venv = detect_venv(workspace)
    filtered = filter_env(source, policy=chosen)

    pinned = False
    bindir = venv_bin_dir(venv)
    if bindir:
        current = filtered.get("PATH", "")
        parts = [item for item in current.split(os.pathsep) if item]
        if bindir not in parts:
            parts.insert(0, bindir)
            filtered["PATH"] = os.pathsep.join(parts)
            pinned = True
        filtered.setdefault("VIRTUAL_ENV", str(Path(venv).parent.parent))

    entries = tuple(item for item in filtered.get("PATH", "").split(os.pathsep) if item)
    shell = source.get("COMSPEC") or source.get("SHELL") or ""
    return ShellSnapshot(
        cwd=str(workspace),
        policy=chosen,
        shell=shell,
        python=venv or sys.executable,
        venv=venv,
        path_entries=entries[:MAX_PATH_ENTRIES],
        env=filtered,
        created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        venv_pinned=pinned,
    )
