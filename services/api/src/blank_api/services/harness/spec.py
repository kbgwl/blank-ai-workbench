"""可版本化的项目 harness：c1-c7 组件面、校验、渲染与 diff。

Harness² 把 agent 周围可优化的东西拆成八个组件。这里只保留七个：我们的运行时
没有插件挂载点（c8），收一个 `.ts` 插件文件只会让人以为它会被执行。其余组件
都落在 ``<workspace>/.blank/harness/``，由 ``instructions.py`` 合成到项目层。

两个硬约束：

* 这里只改提示词和按需文件，不改 ``safety.py`` / ``permissions.py`` /
  ``sandbox/policy.py``。harness 不能成为放宽安全边界的通道。
* always-on 文本有总预算。模型每一步都要为它付 token，所以它必须短；技能、
  子代理、脚本按需列出，正文留在磁盘上等 agent 真需要时再读。
"""

from __future__ import annotations

import ast
import difflib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

HARNESS_DIR = ".blank/harness"
TOTAL_CHAR_LIMIT = 6_000
FILE_CHAR_LIMIT = 4_000
SCRIPT_CHAR_LIMIT = 40_000

_NAME = r"[a-z0-9][a-z0-9_-]*"

# 这些标记只会出现在评测材料里。harness 读到它们，等于把答案往模型眼前送；
# 宁可拒绝一次合法编辑，也不要让一次评测变成开卷考试。
LEAK_MARKERS: tuple[str, ...] = (
    "task-samples.json",
    "evals/results",
    "evals/workspaces",
    "expect_files",
    '"n_passed"',
    '"n_criteria"',
    '"pass_rate"',
    '"all_pass"',
    '"verify_ok"',
    "deterministic_eval",
)
_TASK_ID = re.compile(r"\bT\d{2}\b")


class HarnessError(ValueError):
    """一次校验里所有问题一起返回，proposer 才能一轮改完。"""


@dataclass(frozen=True)
class Component:
    key: str
    label: str
    pattern: re.Pattern[str]
    limit: int
    always_on: bool = False
    on_demand: bool = False


SURFACE: tuple[Component, ...] = (
    Component(
        "c1",
        "工作方式",
        re.compile(r"systemprompt\.md"),
        FILE_CHAR_LIMIT,
        always_on=True,
    ),
    Component(
        "c2",
        "硬性约束",
        re.compile(r"guardrails\.md"),
        FILE_CHAR_LIMIT // 2,
        always_on=True,
    ),
    Component(
        "c3",
        "长期记忆",
        re.compile(r"LongTermMEMORY\.md"),
        FILE_CHAR_LIMIT // 2,
        always_on=True,
    ),
    Component(
        "c4",
        "工具使用",
        re.compile(r"tool_descriptions/tool_guidance\.md"),
        FILE_CHAR_LIMIT,
        always_on=True,
    ),
    Component(
        "c5",
        "子代理",
        re.compile(rf"sub_agents/({_NAME})/AGENT\.md"),
        3_000,
        on_demand=True,
    ),
    Component(
        "c6",
        "技能",
        re.compile(rf"skills/({_NAME})/SKILL\.md"),
        6_000,
        on_demand=True,
    ),
    Component(
        "c6",
        "技能脚本",
        re.compile(rf"skills/({_NAME})/scripts/[A-Za-z0-9_.\-]+\.py"),
        20_000,
        on_demand=True,
    ),
    Component(
        "c7",
        "脚本",
        re.compile(rf"scripts/{_NAME}\.py"),
        SCRIPT_CHAR_LIMIT,
        on_demand=True,
    ),
)

ALWAYS_ON = tuple(component for component in SURFACE if component.always_on)


def normalize_rel(rel: str) -> str:
    """把相对路径规整成 POSIX 形式，任何逃逸尝试直接拒绝。"""
    raw = str(rel or "").strip().replace("\\", "/")
    if not raw:
        raise HarnessError("路径为空")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise HarnessError(f"harness 只接受相对路径：{rel!r}")
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise HarnessError(f"路径不能越出 harness 目录：{rel!r}")
    if not parts:
        raise HarnessError(f"路径为空：{rel!r}")
    return "/".join(parts)


def component_for(rel: str) -> Component | None:
    try:
        rel = normalize_rel(rel)
    except HarnessError:
        return None
    return next((item for item in SURFACE if item.pattern.fullmatch(rel)), None)


def read_tree(root: str | Path, *, strict: bool = False) -> dict[str, str]:
    """读取 harness 目录下的已知组件。

    ``strict=False`` 时忽略 README、.gitkeep 这类不在可编辑面上的文件；优化器
    用 ``strict=True``，让“写了一个没人会读的文件”在采纳前就暴露。
    """
    directory = Path(root)
    if not directory.is_dir():
        return {}
    tree: dict[str, str] = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise HarnessError(f"harness 不接受符号链接：{path}")
        if not path.is_file():
            continue
        rel = normalize_rel(path.relative_to(directory).as_posix())
        if component_for(rel) is None:
            if strict and rel not in ("README.md", ".gitkeep"):
                raise HarnessError(f"{rel!r} 不在可编辑面上")
            continue
        try:
            tree[rel] = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise HarnessError(f"{rel!r} 不是 UTF-8 文本：{exc}") from None
    return tree


def _frontmatter_description(body: str) -> str:
    match = re.search(r"^description:\s*(.+)$", body[:800], re.MULTILINE)
    return match.group(1).strip().rstrip(".") if match else ""


def _name_of(component: Component, rel: str) -> str:
    match = component.pattern.fullmatch(rel)
    return match.group(1) if match and match.groups() else Path(rel).stem


def validate(tree: Mapping[str, str], *, require_content: bool = True) -> None:
    """校验整棵树；所有问题一起抛出，避免 proposer 改一轮才发现下一处。"""
    problems: list[str] = []
    always_on_chars = 0
    has_always_on = False

    for raw_rel, body in sorted(tree.items()):
        try:
            rel = normalize_rel(raw_rel)
        except HarnessError as exc:
            problems.append(f"{raw_rel}: {exc}")
            continue

        component = component_for(rel)
        if component is None:
            problems.append(f"{rel}: 不在 c1-c7 可编辑面上")
            continue

        text = str(body or "")
        if not text.strip():
            problems.append(f"{rel}: 内容为空")
            continue
        if len(text) > component.limit:
            problems.append(
                f"{rel}: {len(text)} 字符，超过 {component.key} 上限 {component.limit}"
            )
        if component.always_on:
            always_on_chars += len(text)
            has_always_on = True

        if rel.endswith(".py"):
            try:
                ast.parse(text)
            except SyntaxError as exc:
                problems.append(f"{rel}: Python 语法错误：{exc.msg}（第 {exc.lineno} 行）")

        for marker in LEAK_MARKERS:
            if marker in text:
                problems.append(f"{rel}: 含有评测材料标记 {marker!r}，不能进入 harness")
        task_id = _TASK_ID.search(text)
        if task_id:
            problems.append(f"{rel}: 含有评测任务编号 {task_id.group(0)!r}，不能进入 harness")

    if always_on_chars > TOTAL_CHAR_LIMIT:
        problems.append(
            f"always-on 文本 {always_on_chars} 字符，超过总预算 {TOTAL_CHAR_LIMIT}"
        )
    if require_content and not has_always_on:
        problems.append("至少需要一个 always-on 组件（systemprompt/guardrails/记忆/工具说明）")

    if problems:
        raise HarnessError("\n".join(problems))


def write_tree(root: str | Path, tree: Mapping[str, str]) -> Path:
    """把一棵已校验的树落到目录；已存在的同名组件直接覆盖。

    内容统一成 LF：Windows 的文本写入会把 ``\\n`` 变成 ``\\r\\n``，如果原内容
    已经带 ``\\r\\n`` 就会写成 ``\\r\\r\\n``，读回来与内存里的树不一致，dirty
    检测会把每一次安装都误判成“运行中被改过”。
    """
    validate(tree)
    directory = Path(root)
    directory.mkdir(parents=True, exist_ok=True)
    for raw_rel, body in sorted(tree.items()):
        rel = normalize_rel(raw_rel)
        target = directory / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        text = str(body).replace("\r\n", "\n").replace("\r", "\n")
        target.write_text(text, encoding="utf-8")
    return directory


def render(tree: Mapping[str, str]) -> str:
    """把树压成可注入系统提示的一段文字。

    c1-c4 全文进入（已经过总预算校验）；c5-c7 只列名字和描述，正文留在磁盘。
    """
    if not tree:
        return ""

    lines = [
        "# 项目 harness",
        "",
        "下面是本项目可版本化的工作约定。它们低于本次任务指令，且任何一条都不能"
        "放宽安全规则、审批规则或验收条件；冲突时按任务 > harness > 项目 > 用户取舍。",
        "",
    ]

    for component in ALWAYS_ON:
        rel = next((item for item in tree if component_for(item) is component), None)
        if rel is None:
            continue
        lines += [f"## {component.label}（{rel}）", str(tree[rel]).strip(), ""]

    on_demand: list[str] = []
    for rel, body in sorted(tree.items()):
        component = component_for(rel)
        if component is None or not component.on_demand:
            continue
        if component.key == "c6" and rel.endswith("SKILL.md"):
            name = _name_of(component, rel)
            desc = _frontmatter_description(str(body))
            on_demand.append(f"- 技能 `{name}`：{desc or '（无描述）'}（内容见 {rel}）")
        elif component.key == "c5":
            name = _name_of(component, rel)
            desc = _frontmatter_description(str(body))
            on_demand.append(f"- 子代理 `{name}`：{desc or '（无描述）'}（内容见 {rel}）")
        elif rel.startswith("scripts/") or "/scripts/" in rel:
            on_demand.append(f"- 脚本 `{rel}`")

    if on_demand:
        lines += [
            "## 按需资源",
            "下面的内容不占当前提示的预算；需要时先用 read_file 读原文，再按它的说明做。",
            *on_demand,
            "",
        ]

    text = "\n".join(lines).strip() + "\n"
    if len(text) > TOTAL_CHAR_LIMIT:
        text = text[:TOTAL_CHAR_LIMIT].rstrip() + "\n…（harness 超过总预算，已截断）\n"
    return text


def diff_tree(before: Mapping[str, str], after: Mapping[str, str]) -> dict:
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    modified = []
    for rel in sorted(set(before) & set(after)):
        if before[rel] == after[rel]:
            continue
        delta = difflib.unified_diff(
            str(before[rel]).splitlines(),
            str(after[rel]).splitlines(),
            lineterm="",
        )
        plus = sum(1 for line in delta if line.startswith("+") and not line.startswith("+++"))
        delta = difflib.unified_diff(
            str(before[rel]).splitlines(),
            str(after[rel]).splitlines(),
            lineterm="",
        )
        minus = sum(1 for line in delta if line.startswith("-") and not line.startswith("---"))
        modified.append({"path": rel, "added_lines": plus, "removed_lines": minus})
    return {"added": added, "removed": removed, "modified": modified}


def summarize(diff: Mapping[str, object]) -> str:
    added = diff.get("added") or []
    removed = diff.get("removed") or []
    modified = diff.get("modified") or []
    bits = []
    if added:
        bits.append(f"新增 {len(added)}")
    if removed:
        bits.append(f"删除 {len(removed)}")
    if modified:
        bits.append(f"修改 {len(modified)}")
    return "、".join(bits) or "无改动"


def load_workspace_tree(root: str | Path) -> dict[str, str]:
    """运行时入口：从 ``<workspace>/.blank/harness`` 读取；没有就是空树。"""
    workspace = Path(root)
    return read_tree(workspace / HARNESS_DIR, strict=False)
