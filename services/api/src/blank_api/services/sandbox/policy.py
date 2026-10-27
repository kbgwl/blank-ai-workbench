"""沙箱策略：资源上限、网络开关与环境变量白名单。

配置来自 BLANK_ 前缀环境变量，便于在不改动应用配置类的情况下按部署环境收紧。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

_SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "AUTH")

DEFAULT_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "PYTHONUNBUFFERED",
    "PYTHONDONTWRITEBYTECODE",
    "NODE_ENV",
    "CI",
    "GIT_CONFIG",
    # Windows 进程的运行必需品，不是用户私货：缺 SYSTEMROOT 时 Winsock 初始化会直接
    # 失败（WinError 10106），沙箱里的 Python 连本机端口都建不出来。M2 的 P-NET 探针
    # 第一次跑就撞上这条——把"环境被削坏"误读成"网络被强制"是最危险的假阳性。
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "USERPROFILE",
)


def _env(name: str, default: str) -> str:
    return os.getenv(f"BLANK_{name}", default).strip()


@dataclass(slots=True)
class SandboxPolicy:
    image: str = field(default_factory=lambda: _env("SANDBOX_IMAGE", "blank-sandbox:dev"))
    enabled: bool = field(default_factory=lambda: _env("SANDBOX_ENABLED", "true").lower() in {"1", "true", "yes"})
    allow_local: bool = field(default_factory=lambda: _env("SANDBOX_ALLOW_LOCAL", "false").lower() in {"1", "true", "yes"})
    # 单次越界宿主执行的部署开关。默认关：模型传了也不执行，交给人工在部署时显式打开。
    allow_host_bypass: bool = field(
        default_factory=lambda: _env("SANDBOX_ALLOW_HOST_BYPASS", "false").lower() in {"1", "true", "yes"}
    )
    cpus: float = field(default_factory=lambda: float(_env("SANDBOX_CPUS", "1.0")))
    memory: str = field(default_factory=lambda: _env("SANDBOX_MEMORY", "512m"))
    pids: int = field(default_factory=lambda: int(_env("SANDBOX_PIDS", "128")))
    max_file_bytes: int = field(default_factory=lambda: int(_env("SANDBOX_MAX_FILE_KB", "262144")))
    tmpfs_size: str = field(default_factory=lambda: _env("SANDBOX_TMPFS", "128m"))
    writable_rootfs: bool = field(default_factory=lambda: _env("SANDBOX_READ_ONLY_ROOT", "true").lower() in {"1", "true", "yes"}) is False
    allow_network: bool = field(default_factory=lambda: _env("SANDBOX_NETWORK", "false").lower() in {"1", "true", "yes"})
    timeout_seconds: float = field(default_factory=lambda: float(_env("SANDBOX_TIMEOUT", "120")))
    env_allowlist: tuple[str, ...] = field(default_factory=lambda: _allowlist_from_env())

    def filter_env(self, env: dict[str, str]) -> dict[str, str]:
        """只透传白名单变量，且带密钥特征的变量一律拒绝进入沙箱。"""
        safe: dict[str, str] = {}
        for key, value in env.items():
            if key not in self.env_allowlist:
                continue
            if any(marker in key.upper() for marker in _SECRET_MARKERS):
                continue
            safe[key] = value
        return safe

    def docker_flags(self, container_name: str, workspace: str) -> list[str]:
        flags = [
            "docker",
            "run",
            "--rm",
            "--pull",
            "never",
            "--name",
            container_name,
            "--memory",
            self.memory,
            "--memory-swap",
            self.memory,
            "--cpus",
            str(self.cpus),
            "--pids-limit",
            str(self.pids),
            "--ulimit",
            f"nproc={self.pids}",
            "--ulimit",
            f"fsize={self.max_file_bytes}",
            "--tmpfs",
            f"/tmp:rw,size={self.tmpfs_size}",
            "--volume",
            f"{workspace}:/workspace:rw",
            "--workdir",
            "/workspace",
            "--network",
            "bridge" if self.allow_network else "none",
            "--label",
            "blank.sandbox=1",
        ]
        if not self.writable_rootfs:
            flags += ["--read-only", "--tmpfs", "/var/tmp:rw,size=16m"]
        return flags


def _allowlist_from_env() -> tuple[str, ...]:
    raw = _env("SANDBOX_ENV_ALLOWLIST", "")
    if not raw:
        return DEFAULT_ENV_ALLOWLIST
    extra = tuple(item.strip().upper() for item in raw.split(",") if item.strip())
    return tuple(dict.fromkeys(DEFAULT_ENV_ALLOWLIST + extra))


def host_bypass_enabled(policy: SandboxPolicy | None = None) -> bool:
    """单次越界宿主执行是否被部署显式打开。未打开时任何请求都必须拒绝执行。"""
    return bool((policy or SandboxPolicy()).allow_host_bypass)
