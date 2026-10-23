"""步级治理快照：对标 Codex rollout 里的 `turn_context`（实物见 `~/.codex/sessions/**/rollout-*.jsonl`）。

为什么需要它：任务级快照只在 `task.started` 写一次。中途把模式切成 plan、路由换了模型、
特性开关被环境变量压住——事件流里全都没有痕迹，出问题时回答不了「这一步当时到底被什么
策略管着」。本轮真实故障有两类正卡在这点上：设置改了没生效（要靠 source 才知道是谁压的）、
审批卡读写口径不一致（要靠当时策略才知道是哪条规则放的行）。

口径只有一处：本模块。事件、证据包、界面都读这里的同一个函数。再写第二份比较，就会
重演「三处各写各的」那一类 bug。
"""

from __future__ import annotations

import hashlib
import json
import platform
from datetime import datetime
from pathlib import Path
from typing import Any

from blank_api.services.features import FEATURES, resolve
from blank_api.services.instructions import content_sha
from blank_api.services.sandbox import boundary
from blank_api.services.sandbox.policy import SandboxPolicy
from blank_api.services.sandbox.runner import SandboxUnavailable, get_sandbox
from blank_api.services.workspace import mask_path


def sandbox_boundary(writable_root: str = "") -> dict[str, Any]:
    """命令此刻实际会被什么边界执行。

    每次现取，不缓存：策略可能被改，缓存等于把「上次读到的值」冒充「现在的值」。
    后端一律走 `get_sandbox`，不在这里重新推导一遍选择逻辑——推导一份就多一个会漂移的地方。
    """
    policy = SandboxPolicy()
    try:
        backend = get_sandbox(policy).name
        denied = ""
    except SandboxUnavailable as exc:
        # 起不来也是事实，要原样写出来：命令类工具此刻会被直接拒掉，
        # 这不是「跑得很慢」，是「根本没跑」。
        backend, denied = "none", str(exc)
    # 配置值 ≠ 生效值。下面这些数字是 policy 里写着的目标，`enforced` 才回答"谁真的在执行它"：
    # 非管理员的 Windows 上（M0 实测档位 job_only）内存/进程数/进程树由 Job Object 强制，
    # CPU、磁盘、文件与网络边界谁都没强制——继续把 cpus=1.0 当成生效值展示就是撒谎。
    enforced = boundary.enforced_fields(backend, limits_enabled=policy.enabled)
    cap = boundary.measure()
    return {
        "backend": backend,
        "enabled": policy.enabled,
        # 隔离强度只有两种真实说法：容器，或者宿主直连（宿主直连不承诺隔离）。
        "isolation": "container" if backend == "docker" else "host",
        # 现场探测出来的档位与来源，与工具事件里的 sandbox_enforcement 同一口径。
        "boundary_strength": cap.strength,
        "boundary_strength_label": cap.label,
        "boundary_measured_seconds_ago": cap.as_dict().get("age_seconds"),
        "boundary_apparatus_ok": cap.apparatus_ok,
        "enforcement_source": boundary.enforcement_source(backend, limits_enabled=policy.enabled),
        "enforced": enforced,
        "allow_local": policy.allow_local,
        "cpus": policy.cpus,
        "memory": policy.memory,
        "pids": policy.pids,
        "tmpfs": policy.tmpfs_size,
        # 语义按 policy 的说法回报：writable_rootfs 为假时容器根目录只读、仅工作区可写。
        "read_only_root": not policy.writable_rootfs,
        "network": policy.allow_network,
        "timeout_seconds": policy.timeout_seconds,
        # 唯一可写位置就是工作区；传进来的是已掩码的名字，主机路径不外泄。
        "writable_root": writable_root,
        "denied": denied,
    }


def feature_snapshot(settings: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """生效值 + 来源。只报 on/off 不够：用户说「我明明开了」，得能指出是被 env 压住还是写在没人认的旧字段里。"""
    out: dict[str, dict[str, Any]] = {}
    for name in FEATURES:
        enabled, source = resolve(name, settings)
        out[name] = {"on": enabled, "source": source}
    return out


def layer_ref(layer: Any) -> dict[str, Any]:
    """指令层的引用：turn.context 与 world.state 共用这一个口径。

    只指认「哪一层、哪个文件（掩码后）、多少字、内容是哪一版」，正文一个字都不抄。
    比对内容变没变读 `sha`，不需要原文——这正是 Codex 用哈希代表指令的原因。
    收到字符串（老调用方、任务级附加指令）时也能工作：就地算指纹，绝不回抄原文。
    """
    as_dict = getattr(layer, "as_dict", None)
    if callable(as_dict):
        return dict(as_dict())
    text = str(layer or "")
    # 裸字符串是「随任务附带的内联指令」，它没有文件可指认。
    # 第一版把它原样塞进 path 字段，等于换了个地方继续抄正文——这条被测试当场抓住。
    return {
        "source": "task",
        "path": "task",
        "chars": len(text),
        "sha": content_sha(text),
        "truncated": False,
    }


def instructions_digest(layers: list[Any] | tuple[Any, ...] | None) -> str:
    """整套指令的版本号（Codex 的 `collaboration_mode.instructions` 就是一个 sha）。

    逐层指纹只能说明「这一层是哪版」，拼起来的指纹才能回答「这次注入的整套约定是哪一版」——
    顺序也算在内：同样两层换个顺序，模型看到的提示词就不同。
    """
    parts: list[str] = []
    for item in layers or []:
        if isinstance(item, str):
            source, content = "task", item
        else:
            source = str(getattr(item, "source", "") or "")
            content = str(getattr(item, "content", "") or "")
        parts.append(f"{source}\n{content}")
    return content_sha("\n\n".join(parts))


def turn_context(
    *,
    node: str,
    step: int,
    role: str,
    model: str,
    provider: str,
    reasoning_effort: str = "",
    verbosity: str = "",
    tier: str = "",
    permission_mode: str,
    permissions: dict[str, Any],
    guardian_mode: str = "",
    readonly: bool = False,
    workspace_root: str = "",
    max_steps: int = 0,
    token_budget: int = 0,
    depth: int = 0,
    agent_path: str = "root",
    instruction_layers: list[Any] | None = None,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """一次模型调用当时的完整治理状态。字段形状对齐 Codex 的 `turn_context`，
    但只报我们自己真有依据的东西——没有的（比如 approvals_reviewer 的 guardian 线程 id）不编。

    主机路径在这里就掩掉：事件会进时间线、证据包和全文索引，漏一次等于把用户目录结构
    广播给所有能看到这个任务的人。调用方传原始根路径，不该再各自记得脱敏。
    """
    masked_root = mask_path(workspace_root) if workspace_root else ""
    # 一次取时刻：连取三次可能跨过午夜，那就会写出一个「今天既是 21 号也是 22 号」的快照。
    now = datetime.now().astimezone()
    return {
        "step": step,
        "node": node,
        "role": role,
        "model": model,
        "provider": provider,
        "reasoning_effort": reasoning_effort or "",
        "verbosity": verbosity or "",
        "tier": tier or "",
        "permission_mode": permission_mode,
        "permissions": permissions,
        "guardian_mode": guardian_mode or "",
        "readonly": bool(readonly),
        "workspace": masked_root,
        "max_steps": max_steps,
        "token_budget": token_budget,
        "depth": depth,
        "agent_path": agent_path,
        # 指令来源：出问题时能回答「这一步的提示词是哪几层拼出来的」。
        # 只给引用（层名/掩码路径/字数/内容指纹），绝不给正文——以前这里是
        # `str(item)`，而 InstructionLayer 的 repr 含 content，实测一条快照 4145 字节，
        # 整份 AGENTS.md 连主机绝对路径一起进了事件流、证据包和全文索引。
        "instructions": [layer_ref(item) for item in (instruction_layers or [])],
        # 对标 Codex 的 current_date / timezone。刻意只到「天」的粒度、且用本地时区名：
        # 指纹里一旦混进秒级时钟，digest 每一步都变，去重永不命中，等于每步重贴一遍同样的策略。
        "current_date": now.strftime("%Y-%m-%d"),
        "timezone": now.tzname() or now.strftime("%z"),
        "sandbox": sandbox_boundary(masked_root),
        "features": feature_snapshot(settings),
    }


def environment_snapshot(shell: Any = None, workspace_root: str = "") -> dict[str, Any]:
    """对标 Codex `world_state.environments.local{cwd,status,shell}`。

    只报名字与数量，不报主机路径：shell 快照里带着完整 PATH（几十条以操作者主目录打头的
    条目）和解释器绝对路径，原样进事件流、证据包与全文索引，等于把装机结构广播给所有能
    看到这个任务的人。要细看的人在本机把 `expose_workspace_paths` 打开即可（那时 cwd 也会还原）。
    """
    local: dict[str, Any] = {
        "cwd": mask_path(workspace_root) if workspace_root else "",
        "status": "unknown",
        "shell": "",
        "os": f"{platform.system()} {platform.release()}".strip(),
        "machine": platform.machine(),
        "python": "",
        "has_venv": False,
        "venv_pinned": False,
        "path_entries": 0,
        "env_names": [],
    }
    if shell is None:
        # 没有快照也是事实：这个运行时压根没装 shell 环境层，命令类工具当时无环境可依。
        return {"local": local}
    local["status"] = "captured"
    local["cwd"] = local["cwd"] or mask_path(getattr(shell, "cwd", "") or "")
    local["shell"] = Path(str(getattr(shell, "shell", "") or "")).name
    local["python"] = Path(str(getattr(shell, "python", "") or "")).name
    local["has_venv"] = bool(getattr(shell, "venv", ""))
    local["venv_pinned"] = bool(getattr(shell, "venv_pinned", False))
    local["path_entries"] = len(list(getattr(shell, "path_entries", ()) or ()))
    local["env_names"] = sorted(getattr(shell, "env", {}) or {})
    return {"local": local}


def world_state(
    *,
    permission_mode: str,
    model: str,
    provider: str,
    readonly: bool = False,
    guardian_mode: str = "",
    workspace_root: str = "",
    instruction_layers: list[Any] | None = None,
    shell: Any = None,
    context_window: int = 0,
    trigger_ratio: float = 0.0,
    reserve_tokens: int = 0,
    trigger_at: int = 0,
) -> dict[str, Any]:
    """对标 Codex 的 `world_state`：`{"full": true, "state": {...}}` 形态的环境快照。

    与 `turn_context` 的分工——那条记的是「这一步被什么策略管着」（审批、沙箱、开关、模型），
    这条记的是「这一步处在什么世界」（哪个目录、什么 shell/系统、哪一版指令、多大的窗口）。
    Codex 也是两条记录，且都靠「全量快照 + 内容指纹」而不是抄原文：几万字不进账本，
    但任何一个版本都能被指认、被比对出来。

    `full: true` 保留 Codex 的语义：这条就是当时的全量，不需要往前拼增量。
    """
    now = datetime.now().astimezone()
    return {
        "full": True,
        "state": {
            # 对标 collaboration_mode{mode, model, instructions:"<sha>"}：
            # instructions 是整套指令的版本号，逐层的引用在下面单独成项。
            "operating_mode": {
                "mode": permission_mode,
                "model": model,
                "provider": provider,
                "readonly": bool(readonly),
                "guardian_mode": guardian_mode or "",
                "instructions": instructions_digest(instruction_layers),
            },
            # 对标 agents_md / apps_instructions：用哈希代表内容。
            "instructions": [layer_ref(item) for item in (instruction_layers or [])],
            "environments": environment_snapshot(shell, workspace_root),
            "context_window_guidance": {
                "context_window": int(context_window or 0),
                "trigger_ratio": float(trigger_ratio or 0.0),
                "reserve_tokens": int(reserve_tokens or 0),
                "trigger_at": int(trigger_at or 0),
            },
            # 与 turn_context 同一口径：只到「天」。掺进秒级时钟就等于每步都宣称世界变了。
            "current_date": now.strftime("%Y-%m-%d"),
            "timezone": now.tzname() or now.strftime("%z"),
        },
    }


def world_state_digest(payload: dict[str, Any]) -> str:
    """环境快照指纹：判断「这一步和上一步是不是同一个世界」。

    秒级时钟一律不算进来（shell 快照的 `created_at` 每次不同，算进去就等于每步重发一条）；
    日期是有意义地算进来的：跨过午夜，任务依赖的「今天」确实换了。
    """
    state = payload.get("state") if isinstance(payload.get("state"), dict) else payload
    canonical = json.dumps(state, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


# 「距今多少秒」每次现算都会变，把它算进指纹就等于每步都宣称策略变了。
VOLATILE_SANDBOX_KEYS = ("boundary_measured_seconds_ago",)


def _without_volatile_sandbox(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key not in VOLATILE_SANDBOX_KEYS}


def digest(payload: dict[str, Any]) -> str:
    """快照指纹。用来判断「这一步和上一步是不是被同一套策略管的」。

    只算治理字段，不算 step/node/depth/agent_path —— 那些是"这一步是谁"，不是"策略变没变"。
    把它们算进去，每一步都不同，去重就永远不会命中，等于每步重贴一遍同样的策略。
    `sandbox` 里的"距今多少秒"同理必须剔掉：那是探测缓存的年龄，不是策略。M1b 接边界强度时
    把它带了进来，实测就是这条让去重每隔几步失效一次（同一套策略连着写三条 turn.context）。
    """
    governing = {
        key: _without_volatile_sandbox(value) if key == "sandbox" and isinstance(value, dict) else value
        for key, value in payload.items()
        if key not in {"step", "node", "role", "depth", "agent_path"}
    }
    canonical = json.dumps(governing, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
