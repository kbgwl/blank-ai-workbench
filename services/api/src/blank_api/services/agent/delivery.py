"""固定四行交付：完成 / 改动 / 验证 / 需要你。

任务书 §5 要求默认交付固定为这四行，没有的项隐藏。做成纯函数的好处是判分器、
证据页和聊天正文可以读同一份口径，不必各拼一套——"一个人说改了 3 个文件、另一个
说改了 2 个"这种事，根源往往就是两处各拼一次。

约定：
- ``完成`` 只留一句话。空泛表扬（"很高兴为您服务"）与自我复述不进来。
- ``改动`` 用数字，不贴整段 Diff——Diff 在证据页看，正文里贴几百行没人读。
- ``验证`` 必须带命令与退出码。没有真跑就没有这一行，不许写"已验证"。
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


@dataclass(frozen=True)
class DeliveryFacts:
    """交付要用的全部事实。都是结构化数据，不从正文里再猜一遍。"""

    completed: str = ""
    changed_files: int = 0
    diff_count: int = 0
    verify_command: str = ""
    verify_exit: int | None = None
    needs_user: str = ""


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


def render(facts: DeliveryFacts) -> str:
    """四行交付。没有的项整行隐藏，不留"（无）"这类占位。"""
    lines: list[str] = []
    completed = _one_line(facts.completed)
    if completed:
        lines.append(f"完成：{completed}")
    if int(facts.changed_files or 0) > 0:
        detail = f"{int(facts.changed_files)} 个文件"
        if int(facts.diff_count or 0) > 0:
            detail += f"、{int(facts.diff_count)} 处 Diff"
        lines.append(f"改动：{detail}")
    if str(facts.verify_command or "").strip() and facts.verify_exit is not None:
        lines.append(f"验证：{str(facts.verify_command).strip()}（退出码 {int(facts.verify_exit)}）")
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
        "verify": {
            "command": str(facts.verify_command or "").strip(),
            "exit": facts.verify_exit,
        },
        "needs_user": _one_line(facts.needs_user, limit=80),
        "text": render(facts),
    }


__all__ = ["DeliveryFacts", "as_dict", "render"]
