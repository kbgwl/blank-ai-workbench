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

from blank_api.services.effort import effort_for_prompt
from blank_api.services.prompt_args import render_prompt
from blank_api.services.skills_defs import match_skill_paths
from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome, fail, ok

# 清单是每轮都在付的固定成本：一条 Skill 只占一行。
SUMMARY_CHARS = 120

# 哪些工具算"碰到了工作区里的这个文件"。只认真正落到文件上的那几个；glob /
# search_code 的 pattern 是通配符不是路径，越权拿它去匹配等于凭猜想激活。
TOUCHED_PATH_TOOLS = frozenset(
    {"read_file", "edit_file", "write_file", "apply_patch", "notebook_edit"}
)

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


def render_skill(
    instructions: str,
    args: str,
    *,
    names=(),
    project_dir: str = "",
    session_id: str = "",
    skill_dir: str = "",
    effort: str = "",
) -> str:
    """把调用参数交给 Skill：与 Claude 的 ``qW`` 同一套占位符规则。"""
    return render_prompt(
        instructions,
        args,
        names=names,
        project_dir=project_dir,
        session_id=session_id,
        skill_dir=skill_dir,
        effort=effort,
    )


def skill_loading_header(name: str) -> str:
    """Claude 的 ``formatSkillLoadingMetadata``：预加载消息的标记头。

    续聊时靠这个头认出「这一份正文上一轮已经给过」，对应上游 ``kDs`` 用
    ``isSkillLoadingMetadata`` 扫历史消息的做法。
    """
    return (
        f"<command-message>{name}</command-message>\n"
        f"<command-name>{name}</command-name>\n"
        "<skill-format>true</skill-format>"
    )


def preload_skills(
    catalog,
    names,
    *,
    project_dir: str = "",
    session_id: str = "",
    carried=(),
    effort: str = "",
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """把 agent frontmatter 里声明的 Skill 正文预加载成消息正文。

    返回 ``([(name, text)], [(name, error)])``。缺名只告警、不阻断派发（上游
    同样只是 log 一行 warning）；``carried`` 里的名字上一轮已经进过对话，跳过
    以免续聊把同一份正文越叠越长。预加载成功后名字记进 ``catalog.loaded``，
    子代理再调 ``skill`` 工具会得到「已加载」，不会把正文贴第二遍。

    ``effort`` 是**环境档位**：Skill 自己声明了就以它为准，没声明时用调用方
    给的这一份（子代理定义里的档位），两者都没有才回落默认档。
    """
    loaded: list[tuple[str, str]] = []
    problems: list[tuple[str, str]] = []
    for raw in names or ():
        name = str(raw or "").strip()
        if not name or name in carried:
            continue
        # 子代理定义里点名的 Skill 是作者显式声明的，不受条件激活闸门影响：
        # 否则 agent 写了 skills: [x] 却因为 x 有 paths 而加载不到。
        entry = (
            catalog.get(name, include_conditional=True)
            if isinstance(catalog, SkillCatalog)
            else None
        )
        if entry is None:
            problems.append((name, f"Skill '{name}' specified in frontmatter was not found"))
            continue
        catalog.loaded.add(name)
        body = render_skill(
            entry["instructions"],
            "",
            names=entry["argument_names"],
            project_dir=project_dir,
            session_id=session_id,
            skill_dir=entry["base_dir"],
            effort=effort_for_prompt(entry.get("effort") or effort),
        )
        loaded.append((name, skill_loading_header(name) + "\n\n" + body))
    return loaded, problems


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
            paths = tuple(str(item) for item in (entry.get("paths") or ()) if str(item).strip())
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
                # paths 非空 = 条件技能：清单里先不出现，等文件工具碰到匹配路径再激活
                # （对齐上游 conditionalSkills / activatedConditionalSkillNames）。
                "paths": paths,
                "model_invocable": bool(entry.get("model_invocable", True)),
                "user_invocable": bool(entry.get("user_invocable", True)),
                "source": str(entry.get("source") or ""),
                "path": str(entry.get("path") or ""),
                "ignored": tuple(entry.get("ignored") or ()),
                # 磁盘命令的调用参数声明与基础目录：args 里的 `$name` 按
                # `argument_names` 的次序取位置参数，`${CLAUDE_SKILL_DIR}` 用
                # `base_dir`。库里的 Skill 两者皆空。
                "argument_hint": str(entry.get("argument_hint") or ""),
                "argument_names": tuple(entry.get("argument_names") or ()),
                "kind": str(entry.get("kind") or "skill"),
                "base_dir": str(entry.get("base_dir") or ""),
                # 命令/Skill 声明的思考强度：加载正文时替换 ${CLAUDE_EFFORT}。
                "effort": str(entry.get("effort") or ""),
                # 命令/Skill 声明的模型：skill 工具调用后覆盖本轮后续步骤的主模型。
                # 对齐上游 SkillTool 返回的 {kind:"model", mainLoopModel}。
                "model": str(entry.get("model") or ""),
                # Skill 声明的钩子：事件名 → HookSpec 列表。注册时直接传给
                # HookRunner.register_skill_hooks，不经过序列化往返。
                "hooks": entry.get("hooks") or {},
                # upstream unloadableGuard：hooks 无效时为 True，此时不注册钩子。
                "unloadable_guard": bool(entry.get("unloadable_guard", False)),
            }
        self.loaded: set[str] = set()
        self.activated: set[str] = set()

    def entries(self) -> list[dict]:
        return [dict(item) for item in self._entries.values()]

    def names(self) -> list[str]:
        return list(self._entries)

    def _visible(self, name: str, item: dict) -> bool:
        """条件技能在激活前不进清单，也不能被模型按名字猜中。"""
        return not item["paths"] or name in self.activated

    def _visible_entries(self) -> list[dict]:
        return [
            item
            for name, item in self._entries.items()
            if self._visible(name, item)
        ]

    def model_names(self) -> list[str]:
        """模型可调用的名字：``disable-model-invocation`` 与未激活的条件技能不出现。"""
        return [
            name
            for name, item in self._entries.items()
            if item["model_invocable"] and self._visible(name, item)
        ]

    def model_available(self) -> bool:
        """条件技能也算可用：它们只是不在清单里，工具不该因此被整个拿掉。"""
        return any(item["model_invocable"] for item in self._entries.values())

    def listing(self) -> list[dict]:
        """系统提示里那一栏的形状：只有名字与一句话说明，不放正文。"""
        return [
            {"name": item["name"], "description": item["description"]}
            for item in self._visible_entries()
            if item["model_invocable"]
        ]

    def get(self, name: str, *, include_conditional: bool = False) -> dict | None:
        item = self._entries.get(name)
        if item is None:
            return None
        if not include_conditional and not self._visible(name, item):
            return None
        return item

    def touch(self, paths) -> list[dict]:
        """文件被碰到时尝试激活条件技能；返回这次**新**激活的那些。

        幂等：同一技能只激活一次，调用方因此可以把完整 tool_results 每步重放
        一遍——审批恢复重建目录后也能靠检查点里的结果把状态找回来。
        """
        activated: list[dict] = []
        for name, item in self._entries.items():
            if not item["paths"] or name in self.activated:
                continue
            for path in paths or ():
                if match_skill_paths(item["paths"], path):
                    self.activated.add(name)
                    activated.append(
                        {
                            "name": name,
                            "description": item["description"],
                            "path": str(path),
                        }
                    )
                    break
        return activated

    def __len__(self) -> int:
        return len(self._entries)

    def __bool__(self) -> bool:
        return bool(self._entries)


def touched_paths(tool_results) -> list[str]:
    """从工具台账里取"真被碰到的文件"（只认成功的文件工具）。"""
    paths: list[str] = []
    for item in tool_results or ():
        if not isinstance(item, dict) or not item.get("ok"):
            continue
        if str(item.get("tool") or "") not in TOUCHED_PATH_TOOLS:
            continue
        data = item.get("data")
        if not isinstance(data, dict):
            continue
        value = data.get("path")
        if isinstance(value, str) and value.strip():
            paths.append(value.strip())
    return paths


def activate_from_tool_results(catalog, tool_results) -> list[dict]:
    """按台账激活条件技能；返回这次新激活的项（调用方负责落事件与提示）。"""
    if not isinstance(catalog, SkillCatalog):
        return []
    return catalog.touch(touched_paths(tool_results))


def render_skill_activation(activated) -> str:
    """激活后的系统提醒：模型要知道"清单里多了一条、名字怎么拼"。"""
    lines = [
        "<system-reminder>",
        "刚才触碰的文件命中了以下条件 Skill 的 paths，它们现在可以调用：",
    ]
    for item in activated or ():
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        description = str(item.get("description") or "").strip()
        matched = str(item.get("path") or "").strip()
        suffix = f"（匹配 {matched}）" if matched else ""
        lines.append(
            f"- {name}：{description}{suffix}" if description else f"- {name}{suffix}"
        )
    lines.append("需要时先用 skill 工具加载正文，名字要照抄，不要猜。")
    lines.append("</system-reminder>")
    return "\n".join(lines)


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
    # Skill 声明的钩子：加载时注册进 HookRunner（对齐上游 SkillTool 加载即注册）。
    # unloadable_guard=True 时钩子整体无效，不注册；hooks 为空时跳过。
    skill_hooks = entry.get("hooks") or {}
    if not entry.get("unloadable_guard") and skill_hooks:
        runner = getattr(context, "hooks", None)
        if runner is not None and hasattr(runner, "register_skill_hooks"):
            added = runner.register_skill_hooks(name, skill_hooks)
            if added:
                await _record(
                    context,
                    "skill.hooks_registered",
                    {"skill": name, "added": added},
                )
    # Skill 声明的模型：本次调用之后，本轮后续步骤的主模型改用这一份。
    # 对齐上游 SkillTool 返回的 {kind:"model", mainLoopModel}——模型切换发生在
    # skill 工具调用之后，影响的是下一步及之后的模型调用，不是当前这一步。
    skill_model = str(entry.get("model") or "")
    if skill_model:
        turn_state = getattr(context, "turn_state", None)
        if isinstance(turn_state, dict):
            turn_state["skill_model"] = skill_model
    body = render_skill(
        entry["instructions"],
        args,
        names=entry["argument_names"],
        project_dir=str(getattr(context, "root", "") or ""),
        session_id=str(getattr(context, "task_id", "") or ""),
        skill_dir=entry["base_dir"],
        # Skill 自己声明了档位就以它为准；没声明时用本轮已经裁决好的那一份
        # （超纲关键词把本轮抬到 xhigh 时，正文里的 ${CLAUDE_EFFORT} 就该是 xhigh）。
        effort=effort_for_prompt(
            entry.get("effort") or getattr(context, "turn_state", {}).get("effort", "")
        ),
    )
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
                "本次任务里已经加载过的 Skill 不要再调用。args 按说明里的占位符展开："
                "$ARGUMENTS 是整串参数，$1、$2 是第几个参数（从 0 起），"
                "$名字 按 Skill 声明的参数名取对应位置。"
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
