"""意图识别与交付口径。

用户的每一句话先在这里被判一次，分三类：

- ``answer`` 不需要动手（寒暄、概念、解释、闲聊）——直接出结果，不进工具循环，
  不拆步骤、不审查、不验收。一次模型调用结束。
- ``advise`` 在问判断（「你觉得哪里能优化」）——先只读看几眼再答。**只读**是关键：
  它需要依据，但用户要的是意见，不是一次改造。看了几个文件、给出结论就结束，
  不写文件、不跑命令、不进修复回路。
- ``act`` 需要动工作区（读文件、改代码、跑命令）——先拆步骤，再执行，最后交付。

``advise`` 是补出来的：早先这类请求被判成 ``answer``，一次模型调用直接答，
听起来很有道理但一行代码都没看，于是只能说些放之四海皆准的废话；判成 ``act``
又会真的动手改，用户只是问问，回来发现文件被动过了。

交付文本一律按「结论 / 下一步」两段写。其余内容（自我介绍、过程复述、
方法论说明）都算噪音，在 ``normalize_answer`` 里被清掉。
"""

from __future__ import annotations

import json
import re

INTENT_ANSWER = "answer"
INTENT_ACT = "act"
INTENT_ADVISE = "advise"


# 明显不需要动工作区的开场：打招呼、道谢、确认在不在。
_CHITCHAT = re.compile(
    r"^\s*(?:"
    r"你好|您好|hi|hello|hey|在吗|在不在|哈喽|嗨|"
    r"谢谢|多谢|感谢|thanks|thank you|ok|okay|好的|收到|明白|懂了|嗯+|"
    r"再见|拜拜|bye"
    r")\s*[!！。.~～,，]*\s*$",
    re.IGNORECASE,
)

# 明确的动手信号：出现这些词基本都要读文件或跑命令。
_ACTION = re.compile(
    r"(改|修改|修复|修一下|重写|重构|实现|新增|添加|加一个|加个|加上|删除|移除|替换|接入|集成|"
    r"升级|降级|安装|部署|发布|打包|构建|编译|运行|执行|跑一下|跑一遍|测试|验证|"
    r"排查|调试|定位|优化|加速|清理|整理|拆分|迁移|提交|回滚|生成|创建|新建|"
    r"写一个|写个|写入|做一个|做个|帮我做|搭一个|搭个|"
    r"读一下|看一下代码|看看代码|搜一下|找一下|列出|统计|对比|导出|转换|"
    r"refactor|fix|implement|build|deploy|migrate|test|debug|run|create|generate)",
    re.IGNORECASE,
)

# 提问信号：没有动手词、又带这些，基本是问概念。
_QUESTION = re.compile(
    r"(是什么|什么是|啥是|什么意思|为什么|怎么回事|区别|哪个好|能不能|可不可以|是否|"
    r"怎么理解|如何理解|介绍一下|解释一下|讲讲|说说|科普|原理|概念)",
)

# 在问「你怎么看」，不是在派活。这类句子常常夹着「优化 / 改」这种动作词
# （「你觉得界面有什么可以优化的」），只看动作词会把它判成改代码任务，
# 于是它真的去改了一通，最后审查说意图不对齐，几十步全部作废。
#
# 判成 advise 而不是 answer：answer 是一次模型调用直接答，一个字都不看，
# 只能给出「界面要简洁、要留白」这种放之四海皆准的话，等于没答。
#
# 这里刻意不放过「要不要」：「先出方案再决定要不要改」是在派活（先产出方案），
# 按意见类判会让它一句"我觉得应该改"就结束，什么也没产出。宁可多问一次模型。
_OPINION = re.compile(
    r"(你觉得|你认为|你看|您看|有什么(?:可以|需要|能)?(?:优化|改|改进|问题|提升)|"
    r"还有(?:哪些|什么)(?:可以|需要)?(?:改|优化|做|补)|需不需要|值不值得|"
    r"是不是该|该不该)"
)

# 但一旦句子里还有明确的指派（帮我 / 然后 / 顺便…），就是真派活了，别降级。
_IMPERATIVE = re.compile(
    r"(帮我|给我|替我|直接|然后|并且|顺便|同时|马上|立刻|现在就去|按.{0,6}改)"
)

# 短句阈值：超过这个长度即使没有动词，也交给模型判，不在这里拍板。
_SHORT_GOAL = 30

# 界面会在用户原话前面拼一段固定前缀：「执行规则：…」「你处于…场景」「场景上下文：…」。
# 前缀里满是「执行 / 修改 / 测试 / 读取」这类动作词，拿它去判意图，连
# 「你现在的工作目录是哪里」都会被判成改代码任务——被判成 act 之后就进工具循环，
# 一步一次全量上下文，几十步下来什么都没交付。判定前必须先把用户原话剥出来。
_USER_MARKER = "用户任务："
_WRAPPER_LINE = re.compile(
    r"^\s*(?:执行规则\s*[:：]|你处于|场景上下文\s*[:：]|日常记忆模式\s*[:：])"
)


def unwrap_goal(goal: str) -> str:
    """取出用户这次真正写的那句话；没有包装时原样返回。

    只在**开头确实是包装**时才剥。这条限制不是保守，是必需的：用户原话里
    完全可能自己写「用户任务：」四个字（比如引用上一次的说法），没有包装时
    去切它就是把用户的话切掉一半。
    """
    text = str(goal or "")
    first_line = text.lstrip().splitlines()[0] if text.strip() else ""
    if not _WRAPPER_LINE.match(first_line):
        return text.strip()
    # 界面拼的前缀在用户原话之前，所以第一个分隔符就是界面那个；
    # 用户原话里自己写的「用户任务：」只会出现在它后面。
    marker = text.find(_USER_MARKER)
    if marker >= 0:
        text = text[marker + len(_USER_MARKER) :]
    kept = [line for line in text.splitlines() if not _WRAPPER_LINE.match(line)]
    cleaned = "\n".join(kept).strip()
    # 剥完是空的，说明这句话本身就是包装（或拼的时候就没有用户内容），
    # 这时退回原文，不要让调用方拿到空串去判意图。
    return cleaned or str(goal or "").strip()


def guess_intent(goal: str) -> str | None:
    """规则快判，返回 ``answer`` / ``advise`` / ``act``，判不准返回 None 交给模型。

    规则只处理边界清晰的几头：纯寒暄、征求意见、以及带明确动作词的请求。
    中间地带（"你看这个怎么办"）一律交给模型，宁可多一次调用也不要误判——
    把改代码当成闲聊、或者把打招呼当成开发任务，这两种都很难看。
    """
    text = (goal or "").strip()
    if not text:
        return INTENT_ANSWER
    if _CHITCHAT.match(text):
        return INTENT_ANSWER
    # 先看是不是在问判断。这一步必须排在动作词之前：「有什么可以优化的」
    # 里的「优化」会把一句征求意见判成改代码任务。
    if _OPINION.search(text) and not _IMPERATIVE.search(text):
        return INTENT_ADVISE
    if _ACTION.search(text):
        return INTENT_ACT
    if len(text) <= _SHORT_GOAL and _QUESTION.search(text):
        return INTENT_ANSWER
    return None


INTENT_INSTRUCTION = (
    "先判断这句话要不要动工作区，不要寒暄、不要解释你的判断。只输出一个 JSON 对象：\n"
    '{"intent": "answer / advise / act", "note": "用一句不超过 20 字的话复述用户想要什么",'
    ' "steps": ["需要看东西或动手时列 2-6 步，不需要则为空"], "answer": "intent 为 answer 时写完整答案，否则留空"}\n'
    "判定规则：\n"
    "- 寒暄、致谢、闲聊、问概念、问你能做什么、解释已有代码 → answer，直接把答案写进 answer 字段。\n"
    "- 用户问你怎么看、哪里有问题、有什么可以优化、值不值得、该不该 → advise。\n"
    "  这类照样要给判断，但判断必须有依据：steps 写清要先看哪几处（例如「读取 panel.tsx」），"
    "不允许在没看过任何东西时给结论。\n"
    "- 要读文件、改代码、跑命令、查数据、生成产物 → act，steps 写清先做什么后做什么。\n"
    "- steps 每条只写「做什么」，一条一句、不超过 18 字，例如「读取 panel.tsx」「修改配色配置」。\n"
    "  不要写怎么做、不要写理由、不要写「步骤一」「首先」这类前缀，不要重复用户原话。\n"
    "- answer 时不要给步骤、不要调用任何工具；advise 时只做只读调研，绝不修改文件；"
    "act 时不要在 answer 里写答案。\n"
    "长度纪律（很重要）：\n"
    "- answer 默认不超过 5 行、120 字。只有用户要清单、或明确说「详细说说」时才展开。\n"
    "- 展开也只用短句逐条列，不写小标题、不举例子、不复述用户的问题。\n"
    "- 寒暄只回一句，不要顺势介绍自己的能力。\n"
    "这一次只做判定与拆分，不要调用工具。"
)

REPORT_INSTRUCTION = (
    "任务执行完了。写交付说明。"
    "过程已经单独记录并展示给用户了——改了哪些文件、跑了哪些命令、输出是什么、花了多久，"
    "这些下面都会逐条列出来，你在这里再写一遍，用户就要读两遍同样的东西。\n"
    "只写这两段，能写多少写多少，没有就不写：\n"
    "结论\n"
    "- 最多 2 条，每条不超过 40 字。写**结果**，不写动作。\n"
    "  好：登录令牌改成 30 天有效\n"
    "  不好：修改 auth.py、更新 config.py、运行测试\n"
    "下一步\n"
    "- 只有用户确实需要接着做点什么才写，最多 2 条；不需要就整段不要写。\n"
    "如果还有没验证的、或者你知道哪儿可能有风险，就并进结论里一句话说完，不要单开一段。\n"
    "硬要求：总共不超过 6 行；不出现「我」「作为」「本次任务」这类自述；不列文件清单、不复述命令；"
    "没做完的事不许写成做完了。"
)

ANSWER_INSTRUCTION = (
    "直接回答用户。要求：不超过 5 行；不要开场白，不要「好的」「当然」「让我」；"
    "不要复述用户的问题；不要介绍你自己能做什么，除非用户问的就是这个。"
)

# 只读调研之后的判断。和 answer 的区别是它手里有刚读到的东西，所以可以具体；
# 和 report 的区别是用户问的是"你怎么看"，不是"你干了啥"，所以不许写成工作汇报。
ADVISE_INSTRUCTION = (
    "用户问的是你的判断，不是让你动手。上面这些工具结果是你刚读到的事实。\n"
    "只写这两部分，能写多少写多少：\n"
    "1. 先说结论：一句话给出你的判断，具体到文件或位置，不要写「整体不错，还有提升空间」。\n"
    "2. 再列最多 3 条依据，每条一行，写你看到的事实，不写你打算怎么做。\n"
    "硬要求：总共不超过 8 行；不写「我读了」「我看了」这类过程句；"
    "没看到的不要猜，不确定就直说没看到。不要输出「结论」「下一步」这类小标题。"
)

# 模型习惯性加的开场白，逐条剥掉。
_NOISE_PREFIX = re.compile(
    r"^\s*(?:"
    r"好的[，,。！!\s]*|好[，,。！!\s]+|当然[，,。！!\s]*|没问题[，,。！!\s]*|"
    r"收到[，,。！!\s]*|明白了[，,。！!\s]*|了解[，,。！!\s]*|"
    r"让我(?:来|先)?[^。\n]{0,20}[。，,\n]|我来(?:帮你)?[^。\n]{0,20}[。，,\n]|"
    r"首先[，,][^。\n]{0,20}[。，,\n]|"
    r"作为(?:一个)?(?:AI|人工智能|语言模型)[^。\n]{0,30}[。，,\n]"
    r")",
    re.IGNORECASE,
)

# 结尾客套，用户读完答案不会因为少了这句话而困惑。
_NOISE_SUFFIX = re.compile(
    r"(?:如果(?:你)?(?:还)?有(?:其他|别的)?(?:问题|需要|疑问)[^。\n]{0,20}[。！!]?"
    r"|如需[^。\n]{0,20}[。！!]?"
    r"|希望(?:对你|这)(?:有|能)[^。\n]{0,20}[。！!]?"
    r"|随时(?:告诉|找)我[^。\n]{0,10}[。！!]?)\s*$"
)

# 交付两段的标题，用于把自由文本拆成分区。旧标题（已完成 / 未完成）保留识别：
# 模型偶尔仍按旧格式写，认不出来就会把整段塞进结论，白丢一次结构。
_SECTION = re.compile(r"^\s*(?:#{1,4}\s*)?\*{0,2}(结论|已完成|未完成|已做|待做|下一步|建议)\*{0,2}\s*[:：]?\s*$")


def _clean_lines(text: str) -> list[str]:
    return [line.rstrip() for line in (text or "").replace("\r\n", "\n").split("\n")]


def normalize_answer(text: str) -> str:
    """把模型回答里的废话削掉，保留内容本身。

    只做四件事：剥开场白、剥结尾客套、压空行、去首尾空白。
    不改变正文措辞——改写内容会引入新的错误，代价远大于收益。
    """
    body = (text or "").strip()
    if not body:
        return ""
    # 开场白可能叠好几层（「好的，当然可以」），最多剥 3 层
    for _ in range(3):
        stripped = _NOISE_PREFIX.sub("", body, count=1).lstrip()
        if stripped == body:
            break
        body = stripped
    body = _NOISE_SUFFIX.sub("", body).rstrip()
    lines = _clean_lines(body)
    # 连续空行压成一个
    squeezed: list[str] = []
    for line in lines:
        if not line.strip() and squeezed and not squeezed[-1].strip():
            continue
        squeezed.append(line)
    return "\n".join(squeezed).strip()


def parse_intent(text: str) -> dict:
    """从模型输出里解析判定结果；解析不出来时返回空 dict 由调用方兜底。"""
    body = (text or "").strip()
    if not body:
        return {}
    candidates: list[str] = []
    if body.startswith("{"):
        candidates.append(body)
    depth, start = 0, -1
    for index, char in enumerate(body):
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                candidates.append(body[start : index + 1])
                start = -1
    for chunk in candidates:
        try:
            parsed = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        intent = str(parsed.get("intent") or "").strip().lower()
        if intent not in (INTENT_ANSWER, INTENT_ACT, INTENT_ADVISE):
            # 兼容模型写成 "no_action"/"chat"/"direct" 的说法
            if intent in ("chat", "direct", "none", "no_action", "reply", "answer_direct"):
                intent = INTENT_ANSWER
            elif intent in ("task", "action", "do", "execute", "work"):
                intent = INTENT_ACT
            elif intent in ("opinion", "advice", "suggest", "review", "investigate"):
                intent = INTENT_ADVISE
            else:
                continue
        steps = parsed.get("steps") or []
        if isinstance(steps, str):
            steps = [steps]
        clean_steps = []
        for item in steps:
            if isinstance(item, dict):
                item = item.get("text") or item.get("title") or item.get("step") or ""
            line = str(item).strip()
            # 模型爱写「步骤一：先读取…」这种带编号和解释的长句。计划卡是给人看的清单，
            # 只留动作本身：去掉编号/前缀、去掉句末标点、限长。
            line = re.sub(r"^\s*(?:步骤\s*[一二三四五六七八九十\d]+\s*[、.：:]?|\d+\s*[.、)]|[-*•])\s*", "", line)
            line = line.strip(" 。；;，,")
            # 两个字的步骤也常见（"跑测试"是三字，"看日志"三字），门槛只用来滤掉空串
            if len(line) >= 2:
                clean_steps.append(line[:60])
        return {
            "intent": intent,
            "note": str(parsed.get("note") or "").strip()[:120],
            "steps": clean_steps[:8],
            "answer": str(parsed.get("answer") or "").strip(),
        }
    return {}


def split_sections(text: str) -> dict[str, list[str]]:
    """把交付文本按「结论 / 未完成 / 下一步」拆成几组条目。

    「已完成」映射成「结论」：动作清单和结果结论要分开，前者界面上已经有了一份
    （逐条事件流），后者才是这段文字存在的理由。旧标题照样认，不让模型的一次
    格式偏离把整段结构丢掉。

    没写这些段（模型没遵守格式）时返回空 dict，调用方自己兜底。
    """
    sections: dict[str, list[str]] = {}
    current = ""
    aliases = {"已完成": "结论", "已做": "结论", "待做": "未完成", "建议": "下一步"}
    for line in _clean_lines(normalize_answer(text)):
        match = _SECTION.match(line)
        if match:
            title = aliases.get(match.group(1), match.group(1))
            current = title
            sections.setdefault(current, [])
            continue
        if not current:
            continue
        cleaned = re.sub(r"^\s*(?:[-*•]|\d+[.、)])\s*", "", line).strip()
        if cleaned:
            sections[current].append(cleaned)
    return {key: value for key, value in sections.items() if value}


def format_report(done: str = "", undone: str = "", next_step: str = "") -> str:
    """按固定顺序拼装交付文本。

    **空段直接省略，不写「无」。** 原来的做法是空段填「无」，理由是不让人误以为
    漏写了；但代价是每份交付都白白多出「未完成 / - 无 / 下一步 / - 无」四行，
    读者还得先确认"这是空的还是漏的"，才回到正题。而"都做完了"这件事，结论段
    自己会说，界面上「N 次失败」也在说。

    结论段仍然例外：它没有任何内容时不该产生交付文本，返回空串由调用方兜底。
    """

    def block(title: str, body: str) -> str:
        lines = [line.strip() for line in (body or "").split("\n") if line.strip()]
        if not lines:
            return ""
        return title + "\n" + "\n".join(
            line if line.startswith(("-", "*")) else f"- {line}" for line in lines
        )

    return "\n".join(
        part for part in (block("结论", done), block("未完成", undone), block("下一步", next_step)) if part
    )


def as_report(text: str) -> str:
    """把任意交付文本规整成可读的一小段。

    - 带小标题：按标题重排、清掉空段。
    - 只有正文（一句话结论）：**原样返回，不套小标题**。套成「结论 / - 那句话 /
      下一步 / - 无」会把一行字变成四行，且没有一处新信息。
    """
    body = normalize_answer(text)
    if not body:
        return ""
    sections = split_sections(body)
    if sections:
        return format_report(
            "\n".join(sections.get("结论", [])),
            "\n".join(sections.get("未完成", [])),
            "\n".join(sections.get("下一步", [])),
        )
    return body
