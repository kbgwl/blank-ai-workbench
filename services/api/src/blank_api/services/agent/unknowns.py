"""写之前先确认：这一处事实，是你给的，还是模型编的。

模型"不问就编"不是一个态度问题，是一个可判定的问题：**写进去的事实值，在它
能看见的任何地方都找不到出处**。用户没说、工作区里也没有，那它只能是编的。
这一层把这个判断做成确定性的，放在写工具落盘**之前**：

1. 从这一次写调用的参数里取出事实值（电话、邮箱、地址）；
2. 拿去和「用户说过的 + 工作区现有的 + 本轮已经确认过的」比对；
3. 找不到出处的，**不许落盘**：这条写入被拒，服务端自己向用户提问。

为什么不在提示词里说？因为提示词只改变概率。2026-10-29 的实测已经证明过：
同一条指令，换个上下文就不问了，然后编一个像样的电话写进页面里。用户的抱怨
「它不懂也不知道问」说的就是这件事——那不是文案，是行为没有约束。

刻意收窄：只查**具体到不可能凭空猜中**的事实值（完整电话、完整邮箱、结构化
地址）。公司名、行业、文案风格这些查不了——「星河科技」和真实公司名长得一样，
正则分不出来，硬查只会把正常写作判成造假。这一格的诚实说法写在验收记录里。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# 只查"往文件里写字"的工具。run_shell 跑的是命令：命令行里的端口号、时间戳、
# 路径都会命中电话/地址的正则，把「跑一次测试」判成「编了手机号」。
CONTENT_TOOLS = frozenset({"write_file", "create_file", "apply_patch"})

# 只查"拿给人看"的文件。代码文件里的电话邮箱多数是**测试夹具**（表单校验的样例、
# 单测里的假地址），把它们也拦下来，写测试就变成一轮一轮地答问，这个功能就没人受得了。
# 真正捅到用户眼前的编造，都发生在页面和文档里。
CONTENT_SUFFIXES = frozenset(
    {".html", ".htm", ".md", ".mdx", ".txt", ".vue", ".svelte", ".astro", ".tsx", ".jsx"}
)

MAX_FINDINGS = 4
# 工作区佐证扫描的上限：这一层只是为了"少问一句废话"，不值得为一个任务读几十兆。
CORPUS_FILES = 120
CORPUS_BYTES = 1_200_000


@dataclass(frozen=True)
class Finding:
    """一处没有出处的事实值。``key`` 是归一化后的比对键。"""

    label: str
    token: str
    key: str
    hint: str


@dataclass(frozen=True)
class _Rule:
    label: str
    pattern: re.Pattern[str]
    hint: str


# 每条都要求"完整成串"，宁可漏报也不误报：半个号码不构成"编了一个电话"。
_RULES: tuple[_Rule, ...] = (
    _Rule(
        "电话",
        re.compile(
            r"(?<![\d+])(?:\+?86[\s-]?)?"
            r"(?:1[3-9]\d(?:[\s-]?\d{4}){2}|0\d{2,3}[\s-]?\d{7,8}|400[\s-]?\d{3}[\s-]?\d{4})"
            r"(?!\d)"
        ),
        "这个号码要用你的真实号码吗？",
    ),
    _Rule(
        "邮箱",
        re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
        "这个邮箱是你的吗？",
    ),
    _Rule(
        "地址",
        re.compile(
            r"[\u4e00-\u9fa5]{2,10}市[\u4e00-\u9fa5]{2,10}(?:区|县|镇)"
            r"[\u4e00-\u9fa5\d]{2,25}(?:路|街|大道|巷|号)"
        ),
        "这个地址是真实的办公地址吗？",
    ),
)

# 归一化：只留数字、字母、汉字。这样「138-1234-5678」与「138 1234 5678」
# 和用户原话里的「13812345678」能对上，不会被空格和横线骗过去。
_DROP = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")


def _norm(text: str) -> str:
    return _DROP.sub("", text.lower())


def _canonical(label: str, key: str) -> str:
    """「+86 138…」和用户说的「138…」是同一件事，比对不能因为区号就判成编造。"""
    if label == "电话" and key.startswith("86") and len(key) == 13:
        return key[2:]
    return key


def facts(text: str) -> list[Finding]:
    """从一段文本里取出"具体到不可能猜中"的事实值。"""
    if not text:
        return []
    found: list[Finding] = []
    seen: set[str] = set()
    for rule in _RULES:
        for match in rule.pattern.finditer(text):
            token = match.group(0).strip()
            key = _canonical(rule.label, _norm(token))
            # 太短的值（例如只有 4 位）比对时会被别的内容顺带命中，不当作事实。
            if len(key) < 7 or key in seen:
                continue
            seen.add(key)
            found.append(Finding(rule.label, token, key, rule.hint))
            if len(found) >= MAX_FINDINGS:
                return found
    return found


def target_path(arguments: dict) -> str:
    """这次写落在哪个文件上。apply_patch 允许把路径写在补丁头里，也认一下。"""
    for key in ("path", "file_path", "file"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    patch = arguments.get("patch")
    if isinstance(patch, str):
        match = re.search(r"^\+\+\+\s+b?/?(\S+)", patch, re.M)
        if match:
            return match.group(1)
    return ""


def is_content_target(arguments: dict) -> bool:
    path = target_path(arguments).lower()
    if not path:
        # 路径都没有的写入本来就跑不通，交给工具自己报错，不在这里抢戏。
        return False
    return any(path.endswith(suffix) for suffix in CONTENT_SUFFIXES)


def facts_of_arguments(name: str, arguments: dict) -> list[Finding]:
    """只看写类工具里"内容"那几个字段，不看路径。"""
    if name not in CONTENT_TOOLS or not isinstance(arguments, dict):
        return []
    if not is_content_target(arguments):
        return []
    found: list[Finding] = []
    seen: set[str] = set()
    for value in arguments.values():
        if not isinstance(value, str):
            continue
        for item in facts(value):
            if item.key in seen:
                continue
            seen.add(item.key)
            found.append(item)
            if len(found) >= MAX_FINDINGS:
                return found
    return found


def corpus(
    root: Path | str | None, messages: object, *, extra: object = ()
) -> str:
    """模型"本来就能知道"的东西：用户说过的、工具读回来的、工作区里已有的。

    刻意**不含** assistant 消息：它自己上一轮编出来的名字，不能反过来当出处。

    ``extra`` 是"本任务里用户刚刚确认过的值"（``AgentSession.confirmed_text``）。
    服务端自己发的问题不像 ``request_user_input`` 那样会在消息里留一条工具结果，
    不把这些值补进来，用户刚给的电话下一步又会被判成"没有出处"，然后被问第二遍。
    """
    parts: list[str] = []
    for text in extra or ():
        if isinstance(text, str) and text.strip():
            parts.append(text)
    for item in messages or ():
        if not isinstance(item, dict):
            continue
        if str(item.get("role") or "") not in ("user", "tool"):
            continue
        content = item.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.extend(part for part in content if isinstance(part, str))
    blob = _norm(" ".join(parts))
    return blob + "\n" + _workspace_text(root)


def _workspace_text(root: Path | str | None) -> str:
    """工作区现有文本的前若干 KB。读不到就算了——这一层只做加分项。"""
    if not root:
        return ""
    path = Path(root)
    if not path.is_dir():
        return ""
    from blank_api.services.tools.paths import iter_text_files

    chunks: list[str] = []
    budget = CORPUS_BYTES
    for index, (_, file_path) in enumerate(iter_text_files(path)):
        if index >= CORPUS_FILES or budget <= 0:
            break
        try:
            data = file_path.read_bytes()[:budget]
        except OSError:
            continue
        budget -= len(data)
        chunks.append(data.decode("utf-8", "ignore"))
    return _norm(" ".join(chunks))


def unverified(
    findings: list[Finding],
    known: str,
    *,
    allowed: frozenset[str] | set[str] = frozenset(),
) -> list[Finding]:
    """去掉"能找到出处"的那些。找不到出处的，就是模型自己编的。"""
    return [
        item
        for item in findings
        if item.key not in allowed and item.key not in known
    ]


def questions_for(findings: list[Finding], *, start: int = 1) -> list[dict]:
    """把"没有出处的事实"变成用户看得懂的问题。

    一个问题一处值：用户回一句「号码是我的」不该顺带把邮箱也认了。
    """
    questions: list[dict] = []
    for offset, item in enumerate(findings):
        questions.append(
            {
                "id": f"fact_{start + offset}",
                "header": f"{item.label}出处",
                "question": (
                    f"我准备写进去的{item.label}是「{item.token}」，但你没给过、工作区里也没有——"
                    f"{item.hint}不确认我就先不写这一处。"
                ),
                "options": [
                    {"label": "就用这个", "description": "这是真实信息，可以写进去"},
                    {"label": "我另给一个", "description": "直接在下面填真实的，我按你填的写"},
                    {"label": "先留空", "description": "这处先不填，等你以后补"},
                ],
                "allow_free_text": True,
            }
        )
    return questions


# 用户答案是这三种之一，剩下的都要当成"又是一句话没给值"来处理。
CONFIRM_MARKS = ("就用这个", "就用它", "是的", "对的", "没错", "可以")
BLANK_MARKS = ("留空", "不填", "不写", "去掉", "删掉", "先不", "待补", "不用", "不要")
# 问题里的第二个选项：用户选了它但没在下面写字时，等于"还没给值"。
PENDING_MARK = "我另给一个"
# "你看着办"不是值。用户把决定权交回来时，这一处仍然没有出处——不能把这句话
# 当成"用户给了电话"，否则模型会照着这四个字写进页面里（真机探针实测到过：
# 用户答"你看着办"，如果按值处理，拒绝理由就变成"用户给的是：你看着办"）。
DELEGATE_MARKS = ("你看着办", "看着办", "你定", "你决定", "随便", "都行", "都可以", "无所谓")


def decide(item: Finding, answer: str) -> str:
    """用户对"这一处值"的回答算哪种：``allow`` / ``blank`` / ``value`` / ``delegate`` / ``none``。

    只有 ``allow`` 能把这一处放行。``value`` 是"用户给了真的"——模型必须按这个写，
    所以它自己那一个仍然拦着，理由里带着用户给的值。``delegate``（"你看着办"）
    不是值：决定权交回来了，但事实仍然只有用户知道，所以留空。
    """
    text = (answer or "").strip()
    if not text:
        return "none"
    if any(mark in text for mark in BLANK_MARKS):
        return "blank"
    if any(mark in text for mark in DELEGATE_MARKS):
        return "delegate"
    if text == PENDING_MARK:
        return "none"
    if any(mark in text for mark in CONFIRM_MARKS):
        return "allow"
    if _norm(text) == item.key or item.key in _norm(text):
        return "allow"
    return "value"


def explain(findings: list[Finding], answers: dict[str, str], kinds: dict[str, str]) -> str:
    """给模型看的拒绝理由。必须带上用户给的答案，否则它下一步只会再编一遍。"""
    lines = [
        "已拦下这次写入：下面这些值没有出处——用户没说过、工作区里也没有，只能是编的。",
        "· " + "；".join(f"{item.token}（{item.label}）" for item in findings),
    ]
    given = [
        f"{item.token} → 用户给的是：{answers[item.key]}"
        for item in findings
        if kinds.get(item.key) == "value" and answers.get(item.key)
    ]
    blank = [
        f"{item.token} → 用户要求留空"
        for item in findings
        if kinds.get(item.key) == "blank"
    ]
    delegate = [
        f"{item.token} → 用户让你自己定"
        for item in findings
        if kinds.get(item.key) == "delegate"
    ]
    missing = [
        item.token
        for item in findings
        if kinds.get(item.key) in (None, "none") and item.key not in answers
    ]
    if given:
        lines.append("用户的回答：")
        lines.extend("· " + line for line in given)
        lines.append("这一处按用户给的值重写。不要再用你原来那个。")
    if blank:
        lines.append("用户的回答：")
        lines.extend("· " + line for line in blank)
        lines.append("这一处不要填：整段去掉，或明确写成「待补充」。")
    if delegate:
        lines.append("用户的回答：")
        lines.extend("· " + line for line in delegate)
        lines.append(
            "这两处是只有用户知道的事实，不要替他编：留空并写成「待补充」，"
            "在总结里说一句等他给。编一个像样的值比留空糟得多。"
        )
    if missing:
        lines.append(
            "这些值用户没有回答：" + "、".join(missing) + "。这一处不要填——"
            "整段去掉，或明确写成「待补充」，并在总结里说明缺什么。"
            "编一个像样的值比留空糟得多。"
        )
    if not (given or blank or delegate or missing):
        lines.append("用户没有给值。这一处不要填，也不要在总结里把它写成已经确认过。")
    return "\n".join(lines)
