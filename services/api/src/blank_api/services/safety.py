"""安全策略：客户决定 Agent 在这台机器上「能做什么」，而不只是「要不要问」。

这里刻意分成两个层次，因为混在一起时用户会被自己的配置困住：

* **能力边界（本模块）** —— 硬规则。能不能执行命令、能不能联网、能不能碰工作区
  以外的路径、哪些命令前缀一律禁止。命中即拒绝，人工审批也不能放行；要放行只能
  先去设置页改。这是给「客户」的那一层：管理员说不许，就真的不许。
* **审批节奏（permissions.py）** —— 软规则。这条命令要不要先问一遍、问过要不要记住。
  改的是节奏，不是边界。

判定顺序也必须是这个顺序：先答「能不能做」，再答「要不要问」。反过来会出现
「点了允许但还是失败」，比直接拒绝更难解释。

客户端没有 Docker 是常态（桌面版装机不带容器运行时），因此策略在这里是唯一的
安全承诺来源：Docker 可用时它叠加容器隔离，不可用时它最多做到策略级约束——
关档时没有隔离，就必须直说「无隔离」，不能再借"本机受限执行"四个字暗示有边界。
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

# 走命令执行的两个工具。其余工具的文件写入由 tools/paths.py 的工作区约束负责。
COMMAND_TOOLS = frozenset({"run_shell", "run_tests"})

OUTCOME_ALLOW = "allow"
OUTCOME_ASK = "ask"
OUTCOME_DENY = "deny"

# Python 侧「计划模式」只读取证，命令会由 permissions 统一拒绝；这里不要抢先拦，
# 否则用户在计划模式下会看到两条互相矛盾的拒绝理由。
_FALLTHROUGH_MODES = frozenset({"plan"})


# --------------------------------------------------------------------------- #
# 内置禁止：宿主机级破坏，任何配置都不放行
# --------------------------------------------------------------------------- #
# 这一组不是「风险高所以要问」，而是「问了也没意义」：装完系统的人不会希望
# 有个 Agent 能格式化磁盘、改引导项或删系统账户。真要做这些，人自己开终端。
BUILTIN_DENY: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(mkfs|mke2fs)\b"), "格式化文件系统"),
    (re.compile(r"(?i)\bformat\s+[a-z]:"), "格式化磁盘分区"),
    (re.compile(r"(?i)\b(diskpart|bcdedit|vssadmin|bootrec)\b"), "改动磁盘或引导配置"),
    (re.compile(r"(?i)\breg\s+(add|delete)\s+hklm\b"), "写入系统级注册表"),
    (re.compile(r"(?i)\b(shutdown|reboot|halt|poweroff)\b"), "关机或重启本机"),
    (re.compile(r"(?i)\bnet\s+user\s+\S+\s+(/delete|/add)\b"), "增删系统账户"),
    (re.compile(r"(?i)\b(rm|del|erase)\s+(-\S+\s+)*[/\\]?\*"), "通配删除当前目录内容"),
    (re.compile(r"(?i)\brm\s+(-\S+\s+)*(/|~|\$HOME)\s*$"), "删除根目录或家目录"),
    (re.compile(r"(?i)\bcipher\s+/w\b"), "擦除磁盘空闲区"),
    (re.compile(r"(?i)\btakeown\b|\bicacls\b.*\bgrant\b"), "接管文件所有权或权限"),
)

# 联网行为：命令会出网，或会从远端拉取内容。
NETWORK_COMMAND = re.compile(
    r"""(?ix)
    \b(curl|wget|aria2c|nc|ncat|telnet|ssh|scp|sftp|ftp|rsync|bitsadmin|certutil\s+-urlcache)\b
    | \b(invoke-webrequest|invoke-restmethod|iwr|irm|start-bitstransfer)\b
    | \bgit\s+(clone|fetch|pull|push|remote|submodule|ls-remote)\b
    | \b(npm|pnpm|yarn|bun)\s+(install|i|add|update|upgrade|ci|dlx|exec)\b
    | \b(pip|pip3|uv|poetry|pipenv)\s+(install|add|update|upgrade|download|sync)\b
    | \b(cargo|go|gem|composer|dotnet)\s+(install|add|get|fetch|update|restore)\b
    | \bdocker\s+(pull|push|login|run)\b
    | \bhttps?://
    """
)

# 绝对路径：盘符、UNC、POSIX 根。相对路径交给 resolve 之后判断。
_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\[^\\/]|/[^/\s])")
# 可能会把路径写在 `--out=C:\x` 或 `> C:\x` 里，先按这些符号粗切一刀。
_PATH_SPLIT = re.compile(r"[=,]")
_TRAILING_PUNCT = "\"'`)]},;"

# 路径闸门对「不存在的目标」怎么处理。真语义就写在这里：`_outside_paths` 只判**存在**的越界
# 绝对路径，不存在的新目标直接跳过——否则 `echo "C:\\nope"` 这种字面量会被误杀。
# 治理快照把这两个字母抄进 `permission_profile.missing_path_behavior`，两边不许各写各的。
MISSING_PATH_BEHAVIOR = "skip"


@dataclass(frozen=True)
class Verdict:
    """一次策略判定。deny 是硬边界，ask 是节奏，两者文案必须能指向下一步动作。"""

    outcome: str = OUTCOME_ALLOW
    rule: str = "allow"
    reason: str = ""
    prefix: str = ""

    @property
    def allowed(self) -> bool:
        return self.outcome == OUTCOME_ALLOW

    @property
    def denied(self) -> bool:
        return self.outcome == OUTCOME_DENY

    @property
    def asks(self) -> bool:
        return self.outcome == OUTCOME_ASK

    def as_dict(self) -> dict:
        return {
            "outcome": self.outcome,
            "rule": self.rule,
            "reason": self.reason,
            "command_prefix": self.prefix,
        }


def allow(rule: str = "allow") -> Verdict:
    return Verdict(OUTCOME_ALLOW, rule)


def ask(rule: str, reason: str = "", prefix: str = "") -> Verdict:
    return Verdict(OUTCOME_ASK, rule, reason, prefix)


def deny(rule: str, reason: str, prefix: str = "") -> Verdict:
    return Verdict(OUTCOME_DENY, rule, reason, prefix)


# --------------------------------------------------------------------------- #
# 策略
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SafetyPolicy:
    """一台机器上的能力边界。默认值全部取最保守的一档。"""

    # 默认不准执行命令：装好就能跑命令的产品，第一次误删就没人敢用了。
    shell_enabled: bool = False
    network_enabled: bool = False
    # 默认每条命令都要人点一次。改这一项是最省事的「放宽」，所以放在最前面。
    confirm_all_commands: bool = True
    restrict_to_workspace: bool = True
    timeout_seconds: float = 120.0
    allowed_roots: tuple[str, ...] = ()
    denied_commands: tuple[str, ...] = ()
    source: str = "default"

    # -- 构造 --------------------------------------------------------------
    @classmethod
    def from_settings(cls, settings: dict | None) -> "SafetyPolicy":
        section = (settings or {}).get("safety")
        if not isinstance(section, dict):
            return cls()
        return cls(
            shell_enabled=bool(section.get("shell_enabled", False)),
            network_enabled=bool(section.get("network_enabled", False)),
            confirm_all_commands=bool(section.get("confirm_all_commands", True)),
            restrict_to_workspace=bool(section.get("restrict_to_workspace", True)),
            timeout_seconds=_clamp_timeout(section.get("timeout_seconds")),
            allowed_roots=tuple(_clean_list(section.get("allowed_roots"))),
            denied_commands=tuple(_clean_list(section.get("denied_commands"))),
            source="settings",
        )

    @classmethod
    def load(cls) -> "SafetyPolicy":
        """运行时入口：跟着 settings.json 走，环境变量只做兜底覆盖。

        桌面壳会把 BLANK_SANDBOX_ENABLED 设成 0，那一条继续表示「不启用容器」，
        与「允许执行命令」是两件事，不能互相顶替。
        """
        from blank_api.services.settings import SettingsStore

        try:
            policy = cls.from_settings(SettingsStore().read())
        except Exception:  # noqa: BLE001 - 读设置失败不能变成"权限放开"
            policy = cls()
        override = str(os.environ.get("BLANK_SHELL_ENABLED") or "").strip().lower()
        if override in ("1", "true", "yes", "on"):
            policy = _replace(policy, shell_enabled=True)
        elif override in ("0", "false", "no", "off"):
            policy = _replace(policy, shell_enabled=False)
        return policy

    # -- 文本入口 ----------------------------------------------------------
    def command_text(self, tool_name: str, arguments: dict) -> str:
        """取出这条调用真正要执行的命令文本；自动识别的测试命令返回空串。"""
        if tool_name not in COMMAND_TOOLS:
            return ""
        return str((arguments or {}).get("command") or "").strip()

    def roots_for(self, workspace: str | Path) -> tuple[Path, ...]:
        """允许触达的根目录：工作区 + 用户显式加的白名单。"""
        roots: list[Path] = []
        try:
            roots.append(Path(workspace).resolve())
        except OSError:
            pass
        for item in self.allowed_roots:
            try:
                roots.append(Path(os.path.expandvars(os.path.expanduser(item))).resolve())
            except OSError:
                continue
        deduped: list[Path] = []
        for root in roots:
            if root not in deduped:
                deduped.append(root)
        return tuple(deduped)

    # -- 判定 --------------------------------------------------------------
    def gate(
        self,
        tool_name: str,
        arguments: dict,
        workspace: str | Path,
        *,
        mode: str = "default",
    ) -> Verdict:
        """先判「能不能做」，再判「要不要问」。"""
        if tool_name not in COMMAND_TOOLS:
            return allow("not_a_command")

        from blank_api.services.permissions import command_prefix

        command = self.command_text(tool_name, arguments)
        prefix = command_prefix(command) if command else ""

        if not self.shell_enabled:
            return deny(
                "capability:shell_disabled",
                "当前安全设置不允许 Agent 执行命令。到「设置 → 安全与权限」打开「允许执行命令」后重试。",
                prefix,
            )

        if command:
            for pattern, label in BUILTIN_DENY:
                if pattern.search(command):
                    return deny(
                        "builtin:destructive",
                        f"该命令会{label}，属于内置禁止项，任何设置都不会放行。",
                        prefix,
                    )

            for rule in self.denied_commands:
                if _prefix_hit(command, rule):
                    return deny(
                        "user:denied_prefix",
                        f"命令命中你设置的禁止规则「{rule}」。如需允许，到设置页调整禁止列表。",
                        prefix,
                    )

            if not self.network_enabled and NETWORK_COMMAND.search(command):
                return deny(
                    "capability:network_disabled",
                    "该命令会访问网络（下载、拉取或推送），当前安全设置禁止联网。到「设置 → 安全与权限」打开联网后重试。",
                    prefix,
                )

            if self.restrict_to_workspace:
                outside = self._outside_paths(command, workspace)
                if outside:
                    return deny(
                        "capability:path_escape",
                        f"命令访问了工作区以外的路径：{outside}。把该目录加入允许列表，或关闭「仅限工作区」后再试。",
                        prefix,
                    )

        # 计划模式下这个工具本来就会被统一下拒绝，不要抢先给一个"要不要允许"的问句。
        if mode in _FALLTHROUGH_MODES:
            return allow("mode:plan")

        if self.confirm_all_commands and mode != "bypass":
            return ask(
                "policy:confirm_all",
                "安全设置要求每条命令都先确认。",
                prefix,
            )
        return allow("policy:auto")

    # -- 路径 --------------------------------------------------------------
    def _outside_paths(self, command: str, workspace: str | Path) -> str:
        """返回第一条越界路径；没有则返回空串。

        只把「真实存在」的绝对路径算作越界，避免把 ``echo "C:\\nope"`` 这种字面量
        误判成越界 —— 误杀会让人关掉整条策略，比漏判更糟。这就是
        `MISSING_PATH_BEHAVIOR = "skip"` 的出处：不存在的目标不参与判定。
        """
        roots = self.roots_for(workspace)
        try:
            base = Path(workspace).resolve()
        except OSError:
            base = Path(workspace)
        for token in _candidate_paths(command):
            target = self._normalize(token, base)
            if target is None:
                continue
            if not _exists(target):
                continue
            if not _within(target, roots):
                return str(target)
        return ""

    @staticmethod
    def _normalize(token: str, base: Path) -> Path | None:
        cleaned = token.strip().strip(_TRAILING_PUNCT)
        if not cleaned or len(cleaned) < 2:
            return None
        expanded = os.path.expandvars(os.path.expanduser(cleaned))
        try:
            candidate = Path(expanded)
            if not candidate.is_absolute():
                if ".." not in candidate.parts:
                    return None
                candidate = base / candidate
            return candidate.resolve()
        except (OSError, ValueError, RuntimeError):
            return None


def _candidate_paths(command: str) -> list[str]:
    """从命令里挑出可能是路径的片段：绝对路径、带 ``..`` 的相对路径。

    每段命令的第一个 token 是程序本身（``C:\\Python312\\python.exe``、``npm``），
    它不是被读写的文件：解释器通常装在项目虚拟环境或系统目录里，把它当成越界
    会导致「跑自己的测试」都跑不了。真正的越界对象在后面的参数里。
    """
    from blank_api.services.permissions import split_segments, tokenize

    found: list[str] = []
    for segment in split_segments(command):
        for token in tokenize(segment)[1:]:
            for piece in _PATH_SPLIT.split(token):
                piece = piece.strip().strip(_TRAILING_PUNCT)
                if not piece:
                    continue
                if _ABSOLUTE.match(piece) or piece.startswith(".."):
                    found.append(piece)
    return found


def _exists(path: Path) -> bool:
    try:
        return path.exists()
    except (OSError, ValueError):
        return False


def _within(path: Path, roots: tuple[Path, ...]) -> bool:
    target = os.path.normcase(str(path))
    for root in roots:
        base = os.path.normcase(str(root))
        if target == base or target.startswith(base + os.sep) or target.startswith(base + "/"):
            return True
    return False


def _prefix_hit(command: str, rule: str) -> bool:
    """用户规则按 token 前缀匹配，与审批时的「命令前缀」口径一致。"""
    from blank_api.services.permissions import _starts_with, split_segments, prefix_rule, tokenize

    tokens = prefix_rule(rule)
    if not tokens:
        return False
    for segment in split_segments(command):
        if _starts_with(tokenize(segment), tokens):
            return True
    return False


def _clean_list(raw) -> list[str]:
    if isinstance(raw, str):
        items = raw.replace("\n", ",").split(",")
    elif isinstance(raw, (list, tuple)):
        items = [str(item) for item in raw]
    else:
        return []
    cleaned: list[str] = []
    for item in items:
        text = item.strip()
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned[:50]


def _clamp_timeout(raw) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 120.0
    if value != value:  # NaN
        return 120.0
    return max(10.0, min(600.0, value))


def _replace(policy: SafetyPolicy, **changes) -> SafetyPolicy:
    from dataclasses import replace as _dataclass_replace

    return _dataclass_replace(policy, **changes)


# --------------------------------------------------------------------------- #
# 执行后端诊断
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class BackendInfo:
    """界面要能回答「现在到底是怎么隔离的」，而不是笼统写个"已开启沙箱"。"""

    backend: str  # docker / local / none
    isolation: str  # container / none / 实测强度（job_only、os_enforced…）
    label: str
    detail: str
    platform: str = field(default_factory=lambda: os.name)
    docker_available: bool = False
    # 「装了」与「能用」是两件事：读数由 docker_probe 现场探测给出，界面不许自己再推导。
    docker_state: str = "pending"
    docker_label: str = ""
    docker_detail: str = ""
    docker_age_seconds: float | None = None
    # 档位事实与后端名同源（resolve_tier），界面不必自己再推导一遍
    tier: str = ""
    strength: str = ""
    available: bool = True
    reason: str = ""
    notice: str = ""

    def as_dict(self) -> dict:
        return {
            "backend": self.backend,
            "isolation": self.isolation,
            "label": self.label,
            "detail": self.detail,
            "platform": self.platform,
            "docker_available": self.docker_available,
            "docker_state": self.docker_state,
            "docker_label": self.docker_label,
            "docker_detail": self.docker_detail,
            "docker_age_seconds": self.docker_age_seconds,
            "tier": self.tier,
            "strength": self.strength,
            "available": self.available,
            "reason": self.reason,
            "notice": self.notice,
        }


def backend_info() -> BackendInfo:
    """默认档 + 现场实测的诚实账。`off` 下必须写「无隔离」，不许伪装成受限执行。"""
    from blank_api.services.sandbox import docker_probe
    from blank_api.services.sandbox.tiers import resolve_tier

    # 「PATH 里有 docker」不等于「容器可用」：装了但守护进程没应答是常态，
    # 那一行读数由现场探测给出，这里只负责把它如实端给界面。
    probe = docker_probe.probe_for_decision()
    resolution = resolve_tier("")
    facts = {
        "docker_available": probe.usable,
        "docker_state": probe.state,
        "docker_detail": probe.detail,
        "docker_label": probe.label,
        "docker_age_seconds": round(time.time() - probe.measured_at, 1) if probe.measured_at else None,
        "tier": resolution.tier,
        "strength": resolution.strength,
        "available": resolution.available,
        "reason": resolution.reason,
        "notice": resolution.notice,
    }
    if not resolution.global_enabled:
        return BackendInfo(
            backend="none",
            isolation="none",
            label="无隔离（宿主完整权限）",
            detail=(
                resolution.notice
                + "。策略开关仍然生效（禁止命令、禁网、路径闸门），但它是审批与拦截，不是隔离边界。"
            ),
            **facts,
        )
    if resolution.backend == "docker":
        return BackendInfo(
            backend="docker",
            isolation="container",
            label="容器隔离",
            detail="命令在一次性容器里执行：工作区是唯一可写挂载点，默认无网络，CPU/内存/进程数/时长均有上限。",
            **facts,
        )
    if not resolution.available:
        # 「该做什么」按探测读数分开说：装了但守护进程没起来的人，你让他去装 Docker 是耍人。
        remedy = (
            "启动 Docker 守护进程（或装 Docker）"
            if probe.state == docker_probe.STATE_DAEMON_UNREACHABLE
            else "装 Docker"
        )
        return BackendInfo(
            backend="none",
            isolation="none",
            label="沙箱后端不可用：命令会被拒绝",
            detail=(
                resolution.notice
                + f"。{remedy}；或把 BLANK_SANDBOX_ALLOW_LOCAL 设成 1（删掉该变量），"
                "改回本机受限额执行。"
            ),
            **facts,
        )
    return BackendInfo(
        backend="local",
        isolation=resolution.strength,
        label="本机执行：" + resolution.label,
        detail=(
            resolution.notice
            + "。工作目录钉在工作区、环境变量走白名单、超时后回收整棵进程树——这些是策略级约束。"
            "装了 Docker 并启动守护进程后会自动改用容器隔离，不用改任何配置。"
        ),
        **facts,
    )


__all__ = [
    "BUILTIN_DENY",
    "BackendInfo",
    "COMMAND_TOOLS",
    "NETWORK_COMMAND",
    "OUTCOME_ALLOW",
    "OUTCOME_ASK",
    "OUTCOME_DENY",
    "SafetyPolicy",
    "Verdict",
    "allow",
    "ask",
    "backend_info",
    "deny",
]
