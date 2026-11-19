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

另有一条：**被裁掉的中段不该消失**。超预算的结果整份写到工作区
（`.blank/tool-results/`），回执里给路径——省 token 的方式从"删"变成"挪"，
模型需要细节时仍然读得到（`git diff` 稍大就只剩前后两段，那种损失是查不出来的）。

必须在结果产生时做、而不是事后改写历史：历史一旦改动，前缀缓存全失效，
省下的上下文会被未命中缓存的差价吃掉。
"""

from __future__ import annotations

import hashlib
import json
import re

# 每个工具在上下文里允许占的字符数：(头部, 尾部)。尾部为 0 表示只要头部。
# 数值不是拍脑袋：读文件平均 2.7k 字符、shell 平均 0.7k，预算设在均值之上、
# 灾难值之下——正常输出完整进入，异常巨大的输出才被切。
OverflowWriter = "Callable[[str, str], str | None]"

TOOL_BUDGETS: dict[str, tuple[int, int]] = {
    # shell 与测试的输出是"编程现场"：失败的断言在中段、结论在末尾、diff 在整段。
    # 原来 1000+2500 只够 85 行——改三个文件的 diff 就看不全，模型只能再跑一次
    # （同样的命令、同样的输出、同样的钱）。放宽到 3000+6000 之后，当前这一步看得全；
    # 长跑的代价由微压缩兜住：超过 8 轮的结果一律回落到 2000 字符（context.py），
    # 也就是说放的是"眼前这一步"，不是"整个任务"。
    "run_shell": (6_000, 8_000),
    "run_tests": (3_000, 7_000),
    "read_file": (6_000, 0),
    "list_files": (2_500, 0),
    "glob": (2_500, 0),
    "search_code": (2_500, 0),
    "read_notes": (1_500, 0),
    # git 的 diff 是模型自检的主要材料，2000 字符只够看一个文件的开头。
    "git": (8_000, 2_000),
}
DEFAULT_BUDGET = (8_000, 0)
# read_file 的头部预算按"这次读到的文件有多大"伸缩，而不是一个常数。
# 固定 6,000 的代价是可算的：800 行源码约 24k 字符，模型要连读四次；而每次续读
# 都要把整段上下文重发一遍——省下的 18k 上下文，换来三倍输入 token。
# 上限取 12,000（窗口 48k 字符的四分之一）：一条结果就吃掉半个窗口的话，
# 下一步必定触发压缩，而压缩一次的开销远大于多给这 6k 字符。
READ_SMALL_FILE = 12_000   # 整份装得下的文件：一次给全
READ_MEDIUM_FILE = 120_000  # 到这个体量前都给上限，再大就只给引子
READ_HEAD_CAP = 12_000
# 日志噪音档：行形态高度重复的输出（`pytest: 12 passed` 跑两千行）只给一点点。
# 一刀切的预算是错的：diff 要看全（每个文件的改动都不同），日志要看少（两千行说同一件事）。
# 按"去数字后的行形态重复度"分流，比按工具名分流更贴近事实。
NOISY_BUDGET = (600, 1_200)
NOISE_SHAPE_RATIO = 0.5
NOISE_SAMPLE_LINES = 400
# 整个 payload 的硬上限（JSON 里还有 data 字段，不只是 output）
MAX_PAYLOAD_CHARS = 20_000
# 结果里允许内联携带 diff 的工具：补丁的 diff 是冗余的，直接丢
DROP_DIFF_TOOLS = frozenset({"apply_patch", "edit_file", "write_file", "notebook_edit", "create_file"})


def _head(text: str, head: int) -> str:
    """取头部，并退回上一个换行处：切在半行中间会让模型读到残句，比不切更难用。"""
    head_part = text[:head]
    cut = head_part.rfind("\n")
    if cut > head * 0.6:
        head_part = head_part[: cut + 1]
    return head_part


def _clip(text: str, head: int, tail: int) -> tuple[str, int]:
    """保留头尾，中间折掉。返回 (新文本, 省下的字符数)。"""
    if head <= 0 and tail <= 0:
        return text, 0
    limit = head + tail
    if len(text) <= limit:
        return text, 0
    head_part = _head(text, head)
    if tail <= 0:
        kept = (
            f"{head_part}\n…（此处省略 {len(text) - len(head_part)} 字符："
            f"需要后面的内容请缩小范围重跑，或把输出写到工作区内的文件再分批读）"
        )
    else:
        tail_part = text[-tail:]
        cut = tail_part.find("\n")
        if 0 <= cut < len(tail_part) * 0.4:
            tail_part = tail_part[cut + 1 :]
        kept = (
            f"{head_part}\n…（中段省略 {len(text) - len(head_part) - len(tail_part)} 字符，"
            f"以下是结尾）…\n{tail_part}"
        )
    return kept, len(text) - len(kept)


def _read_file_note(kept: str, data: dict) -> str:
    """读文件被裁剪时，把「共几行 / 现在给到第几行 / 下一步 offset」写全。

    只写一句「请带 offset 重读」是空头支票：模型照着写一遍，工具压根没有这个参数，
    它就被送回 search_code 摸行号——实测 29 步里 13 步是这么烧掉的。
    """
    total = data.get("total_lines")
    start = data.get("start_line") or 1
    if not isinstance(total, int) or total <= 0 or not isinstance(start, int):
        return ""
    # 行数用 splitlines 数，不要数换行符：正文末尾带不带换行取决于调用方有没有
    # rstrip，数换行就会在这两种情况下差一行。差的那一行正是这条提示要消灭的东西——
    # 少说一行，模型会把最后一行再读一遍；多说一行，它会跳过一行真没看的内容。
    lines = kept.splitlines()
    if not lines:
        return ""
    end = start + len(lines) - 1
    if end >= total:
        return ""
    return f"…（本文件共 {total} 行，此处显示第 {start}–{end} 行，继续请用 offset={end + 1} 重读）"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


_NUMBER_RUN = re.compile(r"\d+")


def _shape_ratio(text: str) -> float:
    """去数字后的行形态有多分散。1.0=每行都不同，接近 0=两千行在说同一件事。

    只看前 400 行：判断"这是日志还是 diff"用不着读全文，而全文可能有几万行。
    """
    lines = text.splitlines()[:NOISE_SAMPLE_LINES]
    if len(lines) < 20:
        return 1.0
    shapes = {_NUMBER_RUN.sub("#", line.strip()) for line in lines}
    return len(shapes) / len(lines)


def _looks_like_patch(text: str) -> bool:
    """是不是补丁/差异。

    必须先判这个再判噪音：一个改了 150 处的 diff，去数字之后每一条 hunk 长得一模一样，
    光看形态会被当成"重复日志"砍掉——而那恰恰是模型最需要逐行看全的东西。
    """
    lines = [line for line in text.splitlines()[:NOISE_SAMPLE_LINES] if line.strip()]
    if len(lines) < 10:
        return False
    hits = sum(1 for line in lines if line.startswith(("@@", "+++", "---", "diff ", "Index: ", "+", "-")))
    return hits / len(lines) >= 0.3


def _read_head(data, output_len: int, base: int) -> int:
    """给 read_file 选头部预算：小文件给全、中等文件给上限、巨物只给引子。

    第三档要单独说清：一个 2MB 的日志，多给 6k 字符只是多 6k 行前奏，
    真正该做的是用 search_code 定位或用 offset 跳读——给多了既没用，又白占窗口。
    """
    size = data.get("size") if isinstance(data, dict) else None
    if not isinstance(size, int) or size <= 0:
        # 没有 size（老的回执、外部调用方）：只按"这次到底读进来多少"判断，
        # 读进来就不超上限的，不必裁。
        return max(base, output_len) if output_len <= READ_SMALL_FILE else base
    if size <= READ_SMALL_FILE:
        return READ_SMALL_FILE
    if size <= READ_MEDIUM_FILE:
        return READ_HEAD_CAP
    return base


def _budget_for(tool_name: str, output: str) -> tuple[int, int]:
    head, tail = TOOL_BUDGETS.get(tool_name, DEFAULT_BUDGET)
    # 只对命令类结果做噪音判定：read_file 有行号、search_code 有路径，形态本来就该分散，
    # 真被误判成噪音也只是少看几行，而 read_file 的 note 会把 offset 给出来。
    if tail <= 0 or _looks_like_patch(output):
        return head, tail
    if _shape_ratio(output) < NOISE_SHAPE_RATIO:
        return NOISY_BUDGET
    return head, tail


def render(
    tool_name: str,
    payload: dict,
    seen: dict[str, int] | None = None,
    step: int = 0,
    overflow: "OverflowWriter | None" = None,
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
        head, tail = _budget_for(tool_name, output)
        data_for_note = body.get("data")
        if tool_name == "read_file" and tail <= 0:
            # 预算按文件大小伸缩：这一步多读 6k，后面就少跑两三轮。
            head = _read_head(data_for_note, len(output), head)
            stats["read_head"] = head
        # 读文件被裁剪时，泛泛的「用 search_code 或带 offset 重读」要换成有数的一句话：
        # 共几行、现在给到第几行、下一步 offset 是几。三样缺一样，模型就只能再摸一遍。
        note = ""
        if tool_name == "read_file" and tail <= 0 and len(output) > head and isinstance(data_for_note, dict):
            note = _read_file_note(_head(output, head), data_for_note)
        if note:
            body["output"] = f"{_head(output, head).rstrip()}\n{note}"
        else:
            body["output"], _ = _clip(output, head, tail)
        # 裁掉了就存一份完整的：回执里给路径，模型需要细节时读得到。
        # 只在**真的裁了**的时候写盘——没裁的结果本来就在上下文里，再存一份是垃圾。
        if overflow is not None and len(body["output"]) < len(output):
            saved_to = overflow(tool_name, output)
            if saved_to:
                body["output"] = (
                    f"{body['output']}\n（完整输出已存盘：{saved_to}，共 {len(output)} 字符；"
                    f"需要中段就用 read_file 读那个文件）"
                )
                stats["overflow_path"] = saved_to

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
