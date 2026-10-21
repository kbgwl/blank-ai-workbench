"""工具结果进上下文之前的瘦身。

一次 100 步的任务里，上下文里绝大部分是工具输出：读一次文件 4,000 字符、
跑一次测试一次几千字符。这些内容对"紧接着的下一步"有用，对"第 30 步之后的
上下文"基本没用——但每一步都要把整段上下文重新发一遍，所以它们会按步数
重复计费，一百步就是一百次。

这里做三件事，全部发生在**结果刚产生时**，写完就不再改动：

1. **按工具给预算**。shell 与测试保留头尾（失败摘要在最前、结论在最后），
   读文件保留开头，补丁结果的 diff 整块丢掉——补丁是模型自己写的，回吐
   一遍等于让它读自己的作业。
2. **相同输出折叠**。同一任务里第二次拿到完全相同的结果，只留一句指回上次，
   不再重发正文。
3. **记账**。每次省下多少字符都进事件流，所以"省了多少"是量出来的，不是估的。

必须在结果产生时做、而不是事后改写历史：历史一旦改动，前缀缓存全失效，
省下的上下文会被未命中缓存的差价吃掉。
"""

from __future__ import annotations

import hashlib
import json

# 每个工具在上下文里允许占的字符数：(头部, 尾部)。尾部为 0 表示只要头部。
# 数值不是拍脑袋：读文件平均 2.7k 字符、shell 平均 0.7k，预算设在均值之上、
# 灾难值之下——正常输出完整进入，异常巨大的输出才被切。
TOOL_BUDGETS: dict[str, tuple[int, int]] = {
    "run_shell": (1_000, 2_500),
    "run_tests": (600, 2_000),
    "read_file": (6_000, 0),
    "list_files": (2_500, 0),
    "search_code": (2_500, 0),
    "read_notes": (1_500, 0),
    "git": (1_000, 1_000),
}
DEFAULT_BUDGET = (8_000, 0)
# 整个 payload 的硬上限（JSON 里还有 data 字段，不只是 output）
MAX_PAYLOAD_CHARS = 9_000
# 结果里允许内联携带 diff 的工具：补丁的 diff 是冗余的，直接丢
DROP_DIFF_TOOLS = frozenset({"apply_patch", "write_file", "create_file"})


def _clip(text: str, head: int, tail: int) -> tuple[str, int]:
    """保留头尾，中间折掉。返回 (新文本, 省下的字符数)。"""
    if head <= 0 and tail <= 0:
        return text, 0
    limit = head + tail
    if len(text) <= limit:
        return text, 0
    # 换行对齐：切在半行中间会让模型读到残句，比不切更难用。
    head_part = text[:head]
    cut = head_part.rfind("\n")
    if cut > head * 0.6:
        head_part = head_part[: cut + 1]
    if tail <= 0:
        kept = f"{head_part}\n…（此处省略 {len(text) - len(head_part)} 字符，需要细节请用 search_code 或带 offset 重读）"
    else:
        tail_part = text[-tail:]
        cut = tail_part.find("\n")
        if 0 <= cut < len(tail_part) * 0.4:
            tail_part = tail_part[cut + 1 :]
        kept = (
            f"{head_part}\n…（省略 {len(text) - len(head_part) - len(tail_part)} 字符）…\n{tail_part}"
        )
    return kept, len(text) - len(kept)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def render(
    tool_name: str, payload: dict, seen: dict[str, int] | None = None, step: int = 0
) -> tuple[str, dict]:
    """把一次工具结果渲染成要放进上下文的字符串。

    ``seen`` 是"本任务已出现过的输出指纹 → 第几步出现过"。命中就只回一句指针。
    返回 (内容, 记账)，记账里的 ``saved`` 是相对"原样塞进去"省下的字符数。
    """
    body = dict(payload or {})
    stats = {"original": 0, "kept": 0, "saved": 0, "dropped_diff": False, "duplicate": False}

    output = body.get("output")
    if isinstance(output, str) and output:
        stats["original"] += len(output)
        if tool_name in DROP_DIFF_TOOLS:
            pass
        head, tail = TOOL_BUDGETS.get(tool_name, DEFAULT_BUDGET)
        body["output"], _ = _clip(output, head, tail)

    data = body.get("data")
    if isinstance(data, dict) and tool_name in DROP_DIFF_TOOLS and data.get("diff"):
        data = {key: value for key, value in data.items() if key != "diff"}
        data["diff_omitted"] = "改动已记录在工作区变更里，不在此重复"
        body["data"] = data
        stats["dropped_diff"] = True

    text = json.dumps(body, ensure_ascii=False, default=str)
    if len(text) > MAX_PAYLOAD_CHARS:
        text, _ = _clip(text, MAX_PAYLOAD_CHARS - 1_200, 1_200)
    stats["kept"] = len(text)
    stats["saved"] = max(0, stats["original"] - stats["kept"]) if stats["original"] else 0

    if seen is not None:
        fingerprint = _digest(text)
        earlier = seen.get(fingerprint)
        if earlier is not None:
            # 指回上一次而不是重发正文。ok/status 必须保留：模型要知道它成功了，
            # 否则会把"已折叠"当成"没结果"再跑一遍——那反而更贵。
            stub = {
                "ok": body.get("ok"),
                "status": body.get("status"),
                "output": f"（与第 {earlier} 步的输出完全相同，正文已折叠；需要细节请重跑该工具）",
            }
            if body.get("message"):
                stub["message"] = body["message"]
            folded = json.dumps(stub, ensure_ascii=False, default=str)
            stats["duplicate"] = True
            # 折叠省的 = 本来要塞进去的整段
            stats["saved"] += max(0, len(text) - len(folded))
            stats["kept"] = len(folded)
            return folded, stats
        seen[fingerprint] = step
    return text, stats
