"""固定四行交付：完成 / 改动 / 验证 / 需要你。

任务书 §5 要求默认交付固定为这四行，没有的项隐藏。做成纯函数的好处是判分器、
证据页和聊天正文可以读同一份口径，不必各拼一套——"一个人说改了 3 个文件、另一个
说改了 2 个"这种事，根源往往就是两处各拼一次。

约定：
- ``完成`` 只留一句话。空泛表扬（"很高兴为您服务"）与自我复述不进来。
- ``改动`` 用数字，不贴整段 Diff——Diff 在证据页看，正文里贴几百行没人读。
- ``验证`` 必须带命令与退出码。没有真跑就没有这一行，不许写"已验证"。
- 有 ``改动`` 却没有 ``验证`` 时，不许整行消失：另起一行 ``未跑校验：<原因>``
  如实说明。静默隐藏会被读成"这次不需要验证"，和谎报是一回事。
- ``需要你`` 没有就整行隐藏；有就只写一个动作。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 空泛表扬 / 自我复述。命中的句子整句丢掉，只保留第一句实质结果。
_NOISE = re.compile(
    r"^(很高兴|希望(这|以上)|如有(任何)?(问题|需要)|请随时|感谢(您|你的)|"
    r"好的[，,]?|当然[，,]?|没问题[，,]?|总之[，,]?|综上所述)"
)
# 一句话结果最多这么长：再长就不是"一句话"，而是又一段汇报。
_MAX_COMPLETED = 120
# 改动行里最多列这么多文件名。再多就只是堆名字，计数已经说清规模了。
_MAX_LISTED_FILES = 5
_ENTRY_NAMES = ("index.html", "index.htm")
# 「改动有、校验没有」时写在交付里的原因。固定措辞，便于前端与判分器按同一口径匹配。
# 措辞刻意不含「验证：」子串——那一行只留给真跑过、带退出码的记录。
UNVERIFIED_REASON = "改动后没有任何校验记录，结果尚未自检"


def entrypoint(files: tuple[str, ...]) -> str:
    """从改动文件里挑出入口页；挑不出来就返回空串，不硬造。"""
    html = [name for name in files if str(name).lower().endswith((".html", ".htm"))]
    for name in html:
        if name.rsplit("/", 1)[-1].lower() in _ENTRY_NAMES:
            return name
    return html[0] if len(html) == 1 else ""


@dataclass(frozen=True)
class DeliveryFacts:
    """交付要用的全部事实。都是结构化数据，不从正文里再猜一遍。"""

    completed: str = ""
    changed_files: int = 0
    diff_count: int = 0
    verify_command: str = ""
    verify_exit: int | None = None
    needs_user: str = ""
    # 没跑校验时的具体原因（例如跳过原因）。为空则用固定的 UNVERIFIED_REASON。
    verify_note: str = ""
    # 跑过校验，但校验之后又改了代码——记录过期。与「根本没跑」要能分开看。
    verify_stale: bool = False
    # 这次校验覆盖到的改动文件。序号答「过没过期」，文件集答「验的是什么」。
    verify_files: tuple[str, ...] = ()
    # 文件名按第一次出现的顺序；只放计数不留名字，用户就没法按「文件结构」验收。
    files: tuple[str, ...] = ()


def _one_line(text: str, *, limit: int = _MAX_COMPLETED) -> str:
    """压成一句话：去换行、去空泛开场、超长截断。"""
    line = " ".join(str(text or "").split())
    if not line:
        return ""
    # 按句号切，跳过空泛开场，取第一句有实质内容的。
    for piece in re.split(r"(?<=[。！？!?])", line):
        candidate = piece.strip(" 。！？!?，,")
        if not candidate or _NOISE.match(candidate):
            continue
        return candidate[:limit]
    return ""


def verification_ran(facts: DeliveryFacts) -> bool:
    """真的跑过校验：有命令，且拿到过退出码。

    只有命令没有退出码不算——那正是"说跑了其实没跑"的形态。
    """
    return bool(str(facts.verify_command or "").strip()) and facts.verify_exit is not None


def render(facts: DeliveryFacts) -> str:
    """四行交付。没有的项整行隐藏，不留"（无）"这类占位。

    唯一的例外是「改动」有、校验没有：另起一行如实写 ``未跑校验：<原因>``。
    """
    lines: list[str] = []
    completed = _one_line(facts.completed)
    if completed:
        lines.append(f"完成：{completed}")
    if int(facts.changed_files or 0) > 0:
        detail = f"{int(facts.changed_files)} 个文件"
        names = tuple(str(name).strip() for name in facts.files if str(name).strip())
        if names:
            listed = "、".join(names[:_MAX_LISTED_FILES])
            if len(names) > _MAX_LISTED_FILES:
                listed += "…"
            detail += f"（{listed}）"
        if int(facts.diff_count or 0) > 0:
            detail += f"、{int(facts.diff_count)} 处 Diff"
        entry = entrypoint(names)
        if entry:
            detail += f"，入口 {entry}"
        lines.append(f"改动：{detail}")
    if verification_ran(facts):
        lines.append(f"验证：{str(facts.verify_command).strip()}（退出码 {int(facts.verify_exit)}）")
    elif int(facts.changed_files or 0) > 0:
        # 改了东西却没有任何校验记录：必须显式说出来，但不能占用「验证：」前缀——
        # 那一行只给真跑过、带退出码的记录（见 test_v163_super_agent 的不变量）。
        # 静默隐藏等于把「没验证」谎报成「无需验证」：真机 46 个改了代码的 episode
        # 就是这么过去的，校验跑了不少，却没有一次落在最后一次改动之后。
        lines.append(f"未跑校验：{str(facts.verify_note or '').strip() or UNVERIFIED_REASON}")
    needs = _one_line(facts.needs_user, limit=80)
    if needs:
        lines.append(f"需要你：{needs}")
    return "\n".join(lines)


def as_dict(facts: DeliveryFacts) -> dict:
    """结构化形态：前端可以按字段画，不必回去解析四行文本。"""
    return {
        "completed": _one_line(facts.completed),
        "changed_files": int(facts.changed_files or 0),
        "diff_count": int(facts.diff_count or 0),
        "files": [str(name) for name in facts.files],
        "entry": entrypoint(tuple(str(name) for name in facts.files)),
        "verify": {
            "command": str(facts.verify_command or "").strip(),
            "exit": facts.verify_exit,
            # 改了东西但没跑校验：结构化字段里也要能直接判，别让前端回读文本。
            "missing": int(facts.changed_files or 0) > 0 and not verification_ran(facts),
            # 跑过但过期（校验之后又改了代码）：与「根本没跑」分开，下游能分别处置。
            "stale": bool(facts.verify_stale),
            # 这条校验覆盖的改动文件：只给真跑过且没过期的记录，否则是空。
            "covers": list(facts.verify_files) if verification_ran(facts) else [],
        },
        "needs_user": _one_line(facts.needs_user, limit=80),
        "text": render(facts),
    }


__all__ = ["DeliveryFacts", "as_dict", "entrypoint", "render"]
