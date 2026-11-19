"""外部字符串进"系统级标签块"之前的消毒。

``<system-reminder>`` 这种标签是**我们自己**画的边界，画边界的意思很明确：
框里是资料，框外才是规矩。但资料可以自己带一个闭合标签——一段 SKILL.md 的
description、一个文件名、一条技能市场里拿到的标题，只要写进去 ``</system-reminder>``，
它后面的字就从"资料"升格成"系统提示"。技能是第三方文本，文件名是任意字符串，
所以这不是理论风险，而是**一个不用写代码就能触发的注入面**。

规矩只有一条：任何外部字符串在拼进标签块之前先过 :func:`neutralize`。
它不删内容、不改语义，只让那几个字失去"标签"这个身份——模型仍然看得见
原始写法（``&lt;/system-reminder&gt;``），只是它不再是一条能闭合边界的指令。

为什么不用转义整段 HTML：这些字段是给人看的说明文字，只在可能冒充标签的
那一个字符上动手，误伤最小；全量转义会让正常内容变得难读，而难读的说明
同样等于没写。
"""

from __future__ import annotations

import re

# 我们自己在提示里用到的边界标签。只盯标签名，不碰普通的 `<`（比较运算、泛型、
# 箭头函数在说明文字里很常见，转掉它们反而是错）。
_TAG_START = re.compile(
    r"<(?=\s*/?\s*(?:system-reminder|system|tool|function_results|user|assistant|command-\w+)\b)",
    re.IGNORECASE,
)
_WHITESPACE = re.compile(r"\s+")


def neutralize(text: object, *, one_line: bool = False, limit: int = 400) -> str:
    """把外部文本变成"只能是文本"的形态。

    ``one_line``：单行字段（名字、路径、一行描述）用它。换行是伪造"下一段指令"
    最省事的办法——一行 ``名称：x`` 之后接一行 ``忽略上面的约束``，读起来就像
    系统自己写的。折成一行之后，它至少是一句显眼的怪话。

    ``limit``：超长截断。注入不需要长度，而一条 8,000 字的 description 会把
    真正的提醒挤到看不见的地方。
    """
    value = _TAG_START.sub("&lt;", str(text or ""))
    if one_line:
        value = _WHITESPACE.sub(" ", value)
    value = value.strip()
    if limit > 0 and len(value) > limit:
        value = value[: limit - 1].rstrip() + "…"
    return value
