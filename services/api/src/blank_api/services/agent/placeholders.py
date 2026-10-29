"""交付前的占位内容审计：把"该问而没问"变成可读的事实。

模型被要求在信息不足时先提问（``graph.SYSTEM_PROMPT`` 的"先问再干"），但提示词
是概率性的——它会漏。这一层只做一件确定性的事：扫一遍本轮**写进工具参数**的内容，
找出几乎不可能是有意为之的占位值（``example.com``、``138-0000-0000``、
``lorem ipsum``、「示例公司」「待填写」……），交给交付环节如实说明。

刻意收得很窄。``TODO``、``placeholder``、``xxx`` 这类在正常代码里到处都是，
放进来自会把「写了个表单」判成「用了占位」，那这种审计就没人信了。

两个使用点：``review_result_node`` 记进 ``review.completed``（依据条可见），
``summarize_result_node`` 在交付正文末尾追加一句确定性的话（模型改不掉）。
两次都只在**本任务从未问过用户**时才报——问过之后就说明信息是用户给的，
再说"这是占位"就成了冤枉。
"""

from __future__ import annotations

import json
import re

MAX_HITS = 12
# 单次扫描上限：run_shell 的参数里可能嵌着整个文件，几十 KB 的正则扫描不值得。
SCAN_CHARS = 200_000

# 写类工具：只有它们的参数会落到工作区里。读类工具的参数是路径和查询词，
# 扫进来只会把「搜索 example.com 这个域名」判成占位。
WRITE_TOOLS = frozenset(
    {"write_file", "create_file", "apply_patch", "run_shell", "spawn_agent", "spawn_agents"}
)

# (标签, 正则)。每条都要求"整词/整串"，宁可漏报也不误报。
_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("示例域名", re.compile(r"\bexample\.(?:com|org|net)\b", re.I)),
    ("占位邮箱", re.compile(r"\b(?:name|your|youremail|someone)@[\w.-]+\.[a-z]{2,}\b", re.I)),
    ("占位手机号", re.compile(r"\b1[3-9]\d[- ]?0{4}[- ]?0{4}\b")),
    ("占位手机号", re.compile(r"\b1[3-9]0{9}\b")),
    ("占位固话", re.compile(r"\b0\d{2,3}-0{3,4}-0{4}\b")),
    ("占位客服号", re.compile(r"\b400-0{3}-0{4}\b")),
    ("占位文案", re.compile(r"lorem\s+ipsum", re.I)),
    ("占位标记", re.compile(r"(待填写|待补充|请替换|示例公司|某某公司|占位内容|示例文案|示例数据)")),
    ("假公司名", re.compile(r"(?<![A-Za-z])(?:XX|xx|某某)\s*(?:公司|科技|集团|工作室)")),
)


def scan(text: str, *, label: str = "") -> list[dict]:
    """在一段文本里找占位值。返回 ``[{label, token, line, sample}]``。

    同一行同一标签只记一次：一段 HTML 里连着十个 ``example.com`` 不等于十个问题。
    """
    hits: list[dict] = []
    seen: set[tuple[str, str]] = set()
    window = text[:SCAN_CHARS]
    lines = window.splitlines() or [window]
    for index, line in enumerate(lines, start=1):
        for tag, pattern in _RULES:
            match = pattern.search(line)
            if not match:
                continue
            token = match.group(0)
            key = (tag, token.lower())
            if key in seen:
                continue
            seen.add(key)
            hits.append(
                {
                    "label": tag,
                    "token": token[:60],
                    "line": index,
                    "sample": line.strip()[:120],
                }
            )
            if len(hits) >= MAX_HITS:
                return hits
    return hits


def _as_text(value: object) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def scan_messages(messages: object) -> list[dict]:
    """扫本轮所有写类工具的调用参数。

    只看 ``assistant`` 的 ``tool_calls``：那里的内容才是模型自己写出去的。
    工具结果（``role=tool``）是被写坏之后的回显，扫它们只会重复计数。
    """
    hits: list[dict] = []
    for item in messages or ():
        if not isinstance(item, dict) or item.get("role") != "assistant":
            continue
        for call in item.get("tool_calls") or ():
            if not isinstance(call, dict):
                continue
            function = call.get("function") or {}
            name = str(function.get("name") or "")
            if name not in WRITE_TOOLS:
                continue
            for hit in scan(_as_text(function.get("arguments"))):
                hits.append({"tool": name, **hit})
                if len(hits) >= MAX_HITS:
                    return hits
    return hits


def describe(hits: list[dict]) -> str:
    """把命中压成一句话，给交付正文用。不指控，只陈述缺口。"""
    tokens: list[str] = []
    for hit in hits:
        token = str(hit.get("token") or "").strip()
        if token and token not in tokens:
            tokens.append(token)
    if not tokens:
        return ""
    shown = "、".join(tokens[:4])
    more = f" 等 {len(tokens)} 处" if len(tokens) > 4 else ""
    return (
        f"这一轮有内容用的是占位值（{shown}{more}）——它们是我编的占位，"
        "换成你的真实信息才算完成；也可以直接告诉我真实值，我改。"
    )
