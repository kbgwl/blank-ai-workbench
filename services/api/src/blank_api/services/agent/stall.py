"""空转断路器：同一件事试到第三遍之前就停下来。

真实台账（安装版 `%APPDATA%/blank-ai-workbench-desktop/blank.db`，9 个任务）里，
一次 101 步 / 140 次模型调用 / 7.13M token 的失败任务占了全部花销的 64.5%。
把它的工具调用逐条摊开，浪费只有四类，而且**没有一类是"压缩不够狠"造成的**：

============================  ====  ==========================================
浪费型态                       次数  当时系统怎么反应
============================  ====  ==========================================
`apply_patch` 参数是 `{}`       4     只回一句"缺少必填参数：path"，模型继续猜
`run_tests` 挂起审批后重试      3     每次都再生成一张待审批请求，结果完全相同
`'node' 不是内部或外部命令`      3     每条命令各失败一次，没人说"这个环境缺 node"
同一条命令原样跑两遍             6     照跑不误，每次都再付一遍 3 万 token 的上下文
============================  ====  ==========================================

所以这里做的不是"把上下文压得更小"，而是**在调用发生前判定它不会带来新信息**：

1. 同一签名（工具 + 语义参数）已经失败两次 → 第三次直接拦，不再花一次执行与一轮上下文；
2. 同一工具同类失败三次（参数错、审批拒、环境缺命令各算一类）→ 拦该工具，让模型换路；
3. 环境级缺陷（沙箱起不来、命令不存在、目录越界、断网）→ 一次就记死，因为**不改环境
   不会好**，再试只是换一条命令重演同一个失败；
4. 挂起审批同样一次记死：用户没批之前，重复调用不可能得到不同答案。

判定只做"减法"：没有明确证据（同签名两次失败、同类三次失败、环境已坏）时一律放行。
宁可漏拦一次，也不要把"重读一个刚被改过的文件"这类合法重试拦掉——那是把省 token
做成了减功能。

本模块不碰 I/O、不碰工具实现，只吃 payload 里的 status / message，所以能逐条断言。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

# -- 失败分类 ---------------------------------------------------------------
# 分类只从工具回执的**文字**来——那是网关唯一一条所有工具共用的通道。
# 加一条规则必须有真实回执样本，否则就变成"猜模型会怎么错"。
KIND_MISSING_ARG = "missing_arg"
KIND_BAD_ARG = "bad_arg"
KIND_DENIED = "denied"
KIND_APPROVAL = "approval"
KIND_ENV_MISSING = "env_missing"
KIND_SANDBOX = "sandbox"
KIND_NETWORK = "network"
KIND_PATH = "path_outside"
KIND_TIMEOUT = "timeout"
KIND_OTHER = "other"

# 工具级的环境缺陷：整个工具都跑不了，重试与换参数都不会改变结果。
# 这里刻意**不含** KIND_ENV_MISSING —— 缺 `node` 只影响用到 node 的命令，`dir` 照样能跑。
# 把它算成工具级，就会在一条命令失败之后拦掉整个 run_shell，那是减功能。
TOOL_BROKEN_KINDS = frozenset({KIND_SANDBOX, KIND_NETWORK})

# 阈值。数字来自真实账本：空参数 apply_patch 连发 4 次、run_tests 挂审批后连试 3 次。
# 设在"已经明确错了"之后一格，既能截住重复，又留一次给模型自我纠正。
SIGNATURE_FAIL_LIMIT = 2  # 同一签名失败满 2 次 → 第 3 次拦
SAME_KIND_LIMIT = 3  # 同一工具同类失败满 3 次 → 回执里加"连错几次"的强提示（不拦）
# 成功重复的阈值比失败宽：`read_file` 读 → 改 → 再读确认是合法动作，真实账本里
# 同参数读过 3 次，其中就有改完复读。而重复成功本身不贵——相同输出在 payload 层
# 已经折叠成一句指针，真正省下的是那一轮上下文。第 4 次才拦。
OK_REPEAT_LIMIT = 3

# 签名里不算"意图"的键：换超时、换步数还是同一个动作。
# 但 limit / offset / max_matches 是算的——那决定了模型看到的内容，换它就是换一件事。
IGNORED_ARG_KEYS = ("timeout_seconds", "timeout", "step", "call_id")

_CN = re.compile(r"[\u4e00-\u9fff]")
_MISSING_ARG = re.compile(r"缺少必填参数|必填参数|missing required|Field required", re.I)
_BAD_ARG = re.compile(r"缺少 @@|无法定位修改位置|补丁未产生任何变更|参数 .* 类型应为|不是合法", re.I)
_APPROVAL = re.compile(r"待审批|等待审批|未获得审批|awaiting approval", re.I)
_DENIED = re.compile(r"驳回|拒绝执行|未获批准|denied", re.I)
# 命令不存在。中文 Windows 的回执是 GBK，经 utf-8 落库后后半句变成 U+FFFD 乱码，
# 所以判据只能是「单引号里的命令名 + 紧跟乱码/中文」这个结构，不能匹配中文词。
# 真实样本（安装版 blank.db 第 101 步那个任务）：
#   `'node' ���������ڲ����ⲿ���Ҳ���ǿ����еĳ���`
#   `'ls' �����ڲ����ⲿ���Ҳ���ǿ����еĳ���`
_ENV_MISSING = re.compile(
    r"'([A-Za-z0-9_.\\/-]{2,40})'\s*(?:\ufffd|[非不]|is not recognized)"
    r"|command not found:?\s*([A-Za-z0-9_.\\/-]{2,40})"
    r"|找不到命令",
    re.I,
)
_SANDBOX = re.compile(r"SandboxUnavailable|沙箱.*(不可用|起不来|没起)|sandbox unavailable", re.I)
_NETWORK = re.compile(r"禁止联网|无法访问网络|network is unreachable|getaddrinfo failed|当前安全设置禁止联网", re.I)
_PATH = re.compile(r"工作区以外|越界|outside the workspace", re.I)
_TIMEOUT = re.compile(r"超时|timed out|timeout", re.I)


def classify(payload: dict) -> str:
    """把一次失败回执归到一类。成功一律回空串。

    只看 `ok` 与两段文字；不看工具名——同一句话在 `run_shell` 和 `run_tests`
    里含义相同（都是"这个环境跑不了这条命令"），按工具分反而会漏。
    """
    if payload.get("ok"):
        return ""
    status = str(payload.get("status") or "")
    if status == "awaiting_approval":
        return KIND_APPROVAL
    text = f"{payload.get('message') or ''}\n{payload.get('output') or ''}"
    for kind, pattern in (
        (KIND_MISSING_ARG, _MISSING_ARG),
        (KIND_SANDBOX, _SANDBOX),
        (KIND_ENV_MISSING, _ENV_MISSING),
        (KIND_APPROVAL, _APPROVAL),
        (KIND_NETWORK, _NETWORK),
        (KIND_PATH, _PATH),
        (KIND_BAD_ARG, _BAD_ARG),
        (KIND_TIMEOUT, _TIMEOUT),
        (KIND_DENIED, _DENIED),
    ):
        if pattern.search(text):
            return kind
    if status in ("denied", "rejected"):
        return KIND_DENIED
    return KIND_OTHER


def missing_command(payload: dict) -> str:
    """从"命令不存在"的回执里取出那个命令名（`node` / `ls` / `pnpm`）。

    取不出来的就回空串：后面的判定退回按签名计数，不拿一个猜出来的名字去拦别的东西。
    """
    text = f"{payload.get('message') or ''}\n{payload.get('output') or ''}"
    match = _ENV_MISSING.search(text)
    if not match:
        return ""
    return str(match.group(1) or match.group(2) or "").strip()


_COMMAND_TOOLS = frozenset({"run_shell", "run_tests"})
_SEPARATORS = re.compile(r"[\s|&;()<>]+")


def _uses_command(name: str, arguments: dict, missing: str) -> bool:
    """这次调用会不会用到那条已知不存在的命令。

    只对命令类工具判，且只看 `command` 字段——别的工具参数里出现 "node" 两个字
    说明不了任何事（读一个名为 node 的文件完全合法）。切词后比 basename，
    所以 `E:\\env\\node\\node.exe` 也认成 node：缺的是这个名字，不是那个路径。
    """
    if name not in _COMMAND_TOOLS or not missing:
        return False
    command = str((arguments or {}).get("command") or "")
    if not command:
        return False
    for token in _SEPARATORS.split(command):
        if not token:
            continue
        base = token.replace("\\", "/").rsplit("/", 1)[-1].lower()
        if base.endswith((".exe", ".cmd", ".bat")):
            base = base.rsplit(".", 1)[0]
        if base == missing.lower():
            return True
    return False


def signature(name: str, arguments: dict) -> str:
    """一次调用的语义指纹：工具名 + 排序后的参数（去掉不影响意图的键）。

    排序是必须的：模型每次给的键顺序不固定，不排序就会把"同一件事"算成两次，
    于是既不拦、也数不清重复了几次。
    """
    clean = {key: value for key, value in (arguments or {}).items() if key not in IGNORED_ARG_KEYS}
    try:
        body = json.dumps(clean, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        body = str(clean)
    return f"{name}:{hashlib.sha256(body.encode('utf-8')).hexdigest()[:16]}"


def _schema_hint(name: str, schema: dict | None) -> str:
    """缺参数时把"这个工具要什么"直接写进回执。

    只回一句"缺少必填参数：path"，模型对 schema 的想象就是唯一依据——真实账本里它
    连着四次发 `{}`，因为每次拿到的信息量与上一次完全相同。把必填项、类型和一条
    形状示例摆出来，它才有第二次尝试的依据。
    """
    if not isinstance(schema, dict):
        return ""
    properties = schema.get("properties") or {}
    required = [str(key) for key in (schema.get("required") or [])]
    if not required and not properties:
        return ""
    described: list[str] = []
    for key in required or list(properties)[:4]:
        rule = properties.get(key) or {}
        kind = str(rule.get("type") or "").strip()
        enum = [str(item) for item in (rule.get("enum") or [])]
        label = f"{key}({kind})" if kind else key
        if enum:
            label += f"，可选 {'/'.join(enum[:4])}"
        described.append(label)
    example = {key: "…" for key in (required or list(properties)[:2])}
    return (
        f"{name} 的必填参数是：{'、'.join(described)}。"
        f"形状示例：{json.dumps(example, ensure_ascii=False)}"
    )


def hint_for(kind: str, name: str, schema: dict | None = None, arguments: dict | None = None) -> str:
    """这一类失败"下一步该做什么"。空串表示没有比模型自己知道的更多的信息。

    刻意不写"请重试"：重试正是被拦下的那个动作。每条都要么指出改什么、要么指出别再做。
    """
    if kind == KIND_MISSING_ARG:
        return _schema_hint(name, schema) or f"{name} 缺必填参数，先确认它的参数名再调用。"
    if kind == KIND_BAD_ARG:
        if name == "apply_patch":
            return (
                "补丁必须带 `--- a/路径` `+++ b/路径` 与 `@@` 段头；新建文件用 `--- /dev/null`，"
                "整文件重写优先用 write_file。"
            )
        if name == "write_file":
            return "改成纯文本 diff 或先 read_file 拿到原文，再按原文重写。"
        return f"{name} 的参数格式不对，按工具说明重排参数，不要把同一份参数再发一次。"
    if kind == KIND_APPROVAL:
        return (
            f"{name} 已经在等用户审批，重复调用不会得到不同结果。先做不需要审批的部分，"
            "或者在交付里说明卡在哪一步。"
        )
    if kind == KIND_DENIED:
        return f"{name} 已被拒绝，本次会话不再执行同类操作；换一条不需要它的路子，或直接问用户。"
    if kind == KIND_ENV_MISSING:
        return (
            "这个环境里没有这条命令。先确认它在 PATH 里（`where 命令名`），或用绝对路径；"
            "换个写法再试一遍结果不会变。"
        )
    if kind == KIND_SANDBOX:
        return "沙箱后端起不来，命令类工具本轮都跑不了；改用读写文件的工具，或让用户检查沙箱设置。"
    if kind == KIND_NETWORK:
        return "当前禁止联网。要走网络得先在「设置 → 安全与权限」里打开，否则改用本地已有内容。"
    if kind == KIND_PATH:
        return "这个路径在工作区之外。要么改成工作区内的路径，要么让用户把它加进允许列表。"
    if kind == KIND_TIMEOUT:
        return "这次是超时，说明工作量比超时预算大：缩小范围（少读几个文件、加过滤条件）再跑。"
    return ""


@dataclass(frozen=True)
class Verdict:
    """一次"要不要放行"的结论。`block` 为真时调用不会被真正执行。"""

    block: bool = False
    kind: str = ""
    reason: str = ""
    hint: str = ""

    def note(self) -> str:
        """拼进回执的一行说明。block 时用 reason + hint，放行时只提示一句。"""
        parts = [item for item in (self.reason, self.hint) if item]
        return " ".join(parts)


@dataclass
class StallBreaker:
    """本回合的调用账本。内存即可：它判的是"这一次执行里已经做过什么"。

    崩溃续跑不会重建它——续跑的那一轮本来就要重新面对同一组工具，这时少拦一次
    比把上一轮的判断硬套到新的一轮更安全。真需要跨轮的话，账在 `tool_calls` 表里，
    由台账页去看。
    """

    # 签名 → {"fail": 失败次数, "ok": 成功次数, "kind": 最近一次失败分类, "tool": 工具名}
    signatures: dict[str, dict] = field(default_factory=dict)
    # (工具, 分类) → 次数
    kind_counts: dict[tuple[str, str], int] = field(default_factory=dict)
    # 已确认坏掉的工具：(工具, 分类)。进了这里该工具的这类调用不再放行。
    broken: set[tuple[str, str]] = field(default_factory=set)
    # 已确认不存在的命令名。缺 node 只影响用到 node 的命令，因此单独记，
    # 不升级成"整个 run_shell 不可用"。
    missing_commands: set[str] = field(default_factory=set)
    # 签名 → 是否已经等过审批。用户没批之前，同一调用不可能有别的结果。
    awaiting: set[str] = field(default_factory=set)
    order: int = 0
    blocked: int = 0

    def record(self, name: str, arguments: dict, payload: dict) -> None:
        """一次调用收尾后记账。成功会清掉该工具的同类失败计数。

        清计数是有意的：工具刚刚成功过，说明环境和参数都没问题，前面那几次失败
        多半是参数写错，这时继续用"同类失败三次"拦它就会拦住正确用法。
        """
        kind = classify(payload)
        key = signature(name, arguments)
        entry = self.signatures.setdefault(key, {"fail": 0, "ok": 0, "kind": "", "tool": name})
        if not kind:
            entry["ok"] += 1
            entry["kind"] = ""
            for existing in [item for item in self.kind_counts if item[0] == name]:
                self.kind_counts.pop(existing, None)
            self.broken = {(tool, why) for tool, why in self.broken if tool != name}
            self.awaiting.discard(key)
            # 这次成功用到了某条"已知不存在"的命令 → 说明它现在能跑了（装了、改了 PATH），
            # 把这条记忆撤掉。不清的话，一次历史失败会永久拦掉一条正确的路。
            command = str((arguments or {}).get("command") or "")
            for missing in [item for item in self.missing_commands if item.lower() in command.lower()]:
                self.missing_commands.discard(missing)
            return

        entry["fail"] += 1
        entry["kind"] = kind
        self.kind_counts[(name, kind)] = self.kind_counts.get((name, kind), 0) + 1
        if kind in TOOL_BROKEN_KINDS:
            self.broken.add((name, kind))
        if kind == KIND_APPROVAL:
            self.awaiting.add(key)
        if kind == KIND_ENV_MISSING:
            missing = missing_command(payload)
            if missing:
                self.missing_commands.add(missing)

    def verdict(self, name: str, arguments: dict, schema: dict | None = None) -> Verdict:
        """调用前的判定。顺序即优先级：越确定的证据越先判，理由才说得清。"""
        key = signature(name, arguments)
        entry = self.signatures.get(key)

        # 1) 同一签名已经失败到阈值：这次一定不会更好。
        if entry and entry["fail"] >= SIGNATURE_FAIL_LIMIT:
            kind = str(entry.get("kind") or KIND_OTHER)
            return Verdict(
                block=True,
                kind=kind,
                reason=f"这一步和前面失败过 {entry['fail']} 次的调用完全相同，再执行只会得到同样的结果。",
                hint=hint_for(kind, name, schema, arguments),
            )
        # 2) 挂过审批：用户没批之前重复调用没有意义，等它比催它省事。
        if key in self.awaiting:
            return Verdict(
                block=True,
                kind=KIND_APPROVAL,
                reason=f"{name} 还在等用户审批。",
                hint=hint_for(KIND_APPROVAL, name, schema, arguments),
            )
        # 3) 工具级环境缺陷（沙箱起不来 / 网络被禁）：换参数也还是同一堵墙。
        for kind in TOOL_BROKEN_KINDS:
            if (name, kind) in self.broken:
                return Verdict(
                    block=True,
                    kind=kind,
                    reason=f"{name} 已经因为「{_label(kind)}」失败过，这个问题不改变环境就不会消失。",
                    hint=hint_for(kind, name, schema, arguments),
                )
        # 4) 缺的那条命令又来了：换一条用到同一个命令的命令也是同一个失败。
        for missing in self.missing_commands:
            if _uses_command(name, arguments, missing):
                return Verdict(
                    block=True,
                    kind=KIND_ENV_MISSING,
                    reason=f"这个环境里没有 `{missing}`，上一条用到它的命令已经因此失败。",
                    hint=hint_for(KIND_ENV_MISSING, name, schema, arguments),
                )
        # 5) 同一签名成功太多次：结果已经在上下文里，别再买一遍。
        if entry and entry["ok"] >= OK_REPEAT_LIMIT:
            return Verdict(
                block=True,
                kind="repeat_ok",
                reason=f"这一步与前面成功过 {entry['ok']} 次的调用完全相同，结果已经在上下文里。",
                hint="直接使用上次的结果；如果文件在这之后被改过，先 read_file 确认再决定要不要重跑。",
            )
        # 6) 同一工具同类失败到阈值：**不拦**，但把"已经连错几次"说清楚。
        #    这一步刻意不 block。真实账本里 apply_patch 的 4 次失败全是一个毛病，
        #    按签名拦就够；如果扩成"这个工具错了 3 次就整个封掉"，那么第 4 次它改成
        #    正确参数也进不来——模型没有任何办法证明自己改对了，任务就死在这里。
        #    拦的目的始终是"别重复"，不是"别尝试"。
        for (tool, kind), count in self.kind_counts.items():
            if tool == name and count >= SAME_KIND_LIMIT:
                hint = hint_for(kind, name, schema, arguments)
                return Verdict(
                    block=False,
                    kind=kind,
                    reason=f"{name} 已经因为「{_label(kind)}」连错 {count} 次，这是第 {count + 1} 次尝试。",
                    hint=hint,
                )
        # 7) 只失败过一次：不拦，但把原因和下一步说清，省掉第二次试错。
        if entry and entry["fail"] == 1:
            kind = str(entry.get("kind") or "")
            hint = hint_for(kind, name, schema, arguments)
            if hint:
                return Verdict(block=False, kind=kind, reason="这一步刚刚失败过一次。", hint=hint)
        return Verdict()

    def stats(self) -> dict:
        return {
            "blocked": self.blocked,
            "signatures": len(self.signatures),
            "broken": sorted(f"{tool}:{kind}" for tool, kind in self.broken),
            "missing_commands": sorted(self.missing_commands),
        }


_LABELS = {
    KIND_MISSING_ARG: "参数缺失",
    KIND_BAD_ARG: "参数格式不对",
    KIND_DENIED: "被拒绝",
    KIND_APPROVAL: "等待审批",
    KIND_ENV_MISSING: "环境缺命令",
    KIND_SANDBOX: "沙箱不可用",
    KIND_NETWORK: "网络被禁",
    KIND_PATH: "路径越界",
    KIND_TIMEOUT: "超时",
    KIND_OTHER: "同类错误",
}


def _label(kind: str) -> str:
    return _LABELS.get(kind, "同类错误")


def has_cjk(text: str) -> bool:
    return bool(_CN.search(text or ""))
