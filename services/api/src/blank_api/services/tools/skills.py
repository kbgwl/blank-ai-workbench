"""Skill：把用户/项目预置的说明按需加载进这一轮（对齐 Claude Code 的 Skill 工具）。

Claude 的形状是「先给清单、按名加载」：系统提示里只放名字与一句话说明，模型判断
任务属于某个 Skill 覆盖的范围时再调用工具把正文取进来照着做。旧实现把全部启用
Skill 的正文每轮都塞进系统提示——模型不需要判断，token 也每轮都付；启用的 Skill
一多，真正相关的那一份反而被淹掉。

目录是任务级的只读快照（运行时从库里取一次），加载状态按会话记：同一个 Skill
在一趟任务里只展开一次，重复调用返回一行「已加载」而不是把正文再贴一遍。
"""

from __future__ import annotations

import re

from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome, fail, ok

# 清单是每轮都在付的固定成本：一条 Skill 只占一行。
SUMMARY_CHARS = 120
# 正文里允许模型用 `$ARGUMENTS` 引用本次调用参数（与 Claude 的 Skill 一致）。
ARGUMENT_PLACEHOLDER = "$ARGUMENTS"

_LEADING_MARKS = re.compile(r"^[#>*\-\s]+")


def skill_summary(instructions: str) -> str:
    """把 Skill 压成一行说明：frontmatter 里的 description 优先，其次正文第一段。

    不凭空生成概括——库里已有的 Skill 只有正文，正文第一行本来就是作者写的
    "这是干什么的"。提案写进来的 Skill 带 frontmatter，其中的 description 才是
    作者专门写的一句话说明，比正文首行更准确。
    """
    text = str(instructions or "")
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for index, line in enumerate(lines[1:], start=1):
            if line.strip() in ("---", "..."):
                for meta_line in lines[1:index]:
                    key, _, value = meta_line.partition(":")
                    if key.strip() == "description" and value.strip():
                        summary = value.strip().strip('"').strip("'")
                        return summary if len(summary) <= SUMMARY_CHARS else summary[: SUMMARY_CHARS - 1] + "…"
                text = "\n".join(lines[index + 1 :])
                break
    for raw in text.splitlines():
        line = _LEADING_MARKS.sub("", raw).strip()
        if not line:
            continue
        return line if len(line) <= SUMMARY_CHARS else line[: SUMMARY_CHARS - 1] + "…"
    return ""


def render_skill(instructions: str, args: str) -> str:
    """把调用参数交给 Skill：优先填 `$ARGUMENTS`，否则附在正文末尾。"""
    text = str(instructions or "")
    args = str(args or "").strip()
    if not args:
        return text
    if ARGUMENT_PLACEHOLDER in text:
        return text.replace(ARGUMENT_PLACEHOLDER, args)
    return f"{text}\n\n调用参数：{args}"


class SkillCatalog:
    """一次任务可用的 Skill 快照 + 本任务已经加载过哪些。

    同名以**后出现的为准**：库里的查询按 `updated_at` 升序，后出现的那行是较新的
    修订；清单里仍然只列一次，不让同一个名字占两行。
    """

    def __init__(self, entries=None) -> None:
        self._entries: dict[str, dict] = {}
        for entry in entries or ():
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            if not name:
                continue
            instructions = str(entry.get("instructions") or "").strip()
            # 提案采用后的 Skill 自带一句专门写的 description（skills.metadata），
            # 它比从正文首行压出来的更准确；没有时才回落到正文。
            self._entries[name] = {
                "name": name,
                "description": str(entry.get("description") or "").strip()
                or skill_summary(instructions),
                "instructions": instructions,
                # 磁盘定义（.blank/skills）带过来的审计/可用性字段。库里那条没有，
                # 默认就是"模型可调用、用户可调用"。
                "display_name": str(entry.get("display_name") or "").strip(),
                "allowed_tools": tuple(entry.get("allowed_tools") or ()),
                "disallowed_tools": tuple(entry.get("disallowed_tools") or ()),
                "model_invocable": bool(entry.get("model_invocable", True)),
                "user_invocable": bool(entry.get("user_invocable", True)),
                "source": str(entry.get("source") or ""),
                "path": str(entry.get("path") or ""),
                "ignored": tuple(entry.get("ignored") or ()),
            }
        self.loaded: set[str] = set()

    def entries(self) -> list[dict]:
        return [dict(item) for item in self._entries.values()]

    def names(self) -> list[str]:
        return list(self._entries)

    def model_names(self) -> list[str]:
        """模型可调用的名字：``disable-model-invocation`` 的那些不能出现。"""
        return [name for name, item in self._entries.items() if item["model_invocable"]]

    def model_available(self) -> bool:
        return any(item["model_invocable"] for item in self._entries.values())

    def listing(self) -> list[dict]:
        """系统提示里那一栏的形状：只有名字与一句话说明，不放正文。"""
        return [
            {"name": item["name"], "description": item["description"]}
            for item in self._entries.values()
            if item["model_invocable"]
        ]

    def get(self, name: str) -> dict | None:
        return self._entries.get(name)

    def __len__(self) -> int:
        return len(self._entries)

    def __bool__(self) -> bool:
        return bool(self._entries)


async def _record(context: ToolContext, event: str, payload: dict) -> None:
    recorder = getattr(context, "recorder", None)
    if recorder is not None:
        await recorder.add(event, "skill", payload)


async def _skill(context: ToolContext, arguments: dict) -> ToolOutcome:
    catalog = getattr(context, "skills", None)
    name = str(arguments.get("skill") or "").strip()
    args = str(arguments.get("args") or "").strip()
    if name.startswith("/"):
        return fail(
            "error",
            f"skill 工具不接受前导斜杠：把「{name}」传成「{name.lstrip('/')}」再调用。",
        )
    if not isinstance(catalog, SkillCatalog) or not len(catalog):
        return fail("error", "当前任务没有可用的 Skill。")
    entry = catalog.get(name)
    if entry is None:
        available = "、".join(catalog.model_names())
        return fail(
            "error",
            f"没有名为「{name}」的 Skill。当前可用：{available}。"
            "只能用清单里的准确名字，不要猜；CLI 内置命令（/help、/clear 等）不是 Skill。",
        )
    if not entry["model_invocable"]:
        return fail(
            "error",
            f"Skill「{name}」标记了 disable-model-invocation，只能由用户在界面里显式触发，"
            "不能由你调用；也不要用别的方式复刻它的流程。",
        )
    if name in catalog.loaded:
        return ok(
            f"Skill「{name}」已在本任务中加载过，直接按已加载的说明执行，不要重复调用。",
            skill=name,
            already_loaded=True,
        )
    catalog.loaded.add(name)
    body = render_skill(entry["instructions"], args)
    await _record(context, "skill.invoked", {"skill": name, "has_args": bool(args), "chars": len(body)})
    return ok(
        f"已加载 Skill「{name}」，按以下说明执行：\n\n{body}",
        skill=name,
        already_loaded=False,
    )


def skill_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="skill",
            description=(
                "调用一个 Skill。Skill 是用户或项目为某类任务预先准备好的一组说明"
                "（部署步骤、评审清单、仓库特定的流程）。可用的 Skill 会列在系统提示里；"
                "当任务正好属于某个 Skill 覆盖的范围时，先调用这个工具把它的说明加载进来，"
                "再按说明执行，替换你的默认做法。skill 必须是清单里的准确名字，不要猜；"
                "用户点名要某个 Skill（例如「用 deploy」或 /deploy）也是调用它的请求。"
                "本次任务里已经加载过的 Skill 不要再调用。args 原样传给 Skill 的说明。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "skill": {
                        "type": "string",
                        "description": "可用 Skill 列表里的准确名字，不要带前导斜杠",
                    },
                    "args": {
                        "type": "string",
                        "description": "可选参数，原样传给 Skill 的说明",
                    },
                },
                "required": ["skill"],
            },
            risk_level="low",
            handler=_skill,
        )
    ]
