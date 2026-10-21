"""apply_patch: deterministic unified-diff style edits with reversible backups."""

from __future__ import annotations

import difflib
import re
import uuid
from pathlib import Path

from blank_api.models.task import FileChange
from blank_api.services.log_safety import contains_secret, redact
from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome, ok
from blank_api.services.tools.paths import BACKUP_DIRNAME, resolve_for_write

HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
CONTEXT_TOLERANCE = 40
# 补丁头里的路径：``--- a/foo.py`` / ``+++ b/foo.py``。git 风格带 a/ b/ 前缀，去掉。
FILE_HEADER = re.compile(r"^(?:---|\+\+\+)\s+(?:\w/)?(\S+)")


class PatchError(ValueError):
    """补丁无法应用。

    ``near`` 是失配位置附近的真实内容（带行号）。没有它，模型只能盲猜着重打一遍，
    实测一次失败的补丁平均要重试两次以上，这既是延迟也是 token。
    """

    def __init__(self, message: str, near: str = "") -> None:
        super().__init__(message)
        self.near = near


def infer_path(patch: str) -> str:
    """从补丁头里取文件路径，取不到返回空串。

    模型写补丁时几乎总带着 ``--- a/x`` / ``+++ b/x``，却常常忘了另填一个
    ``path`` 参数。以前这种调用直接被参数校验挡掉（"缺少必填参数：path"），
    白白烧掉一步。路径明明就在补丁里，就该自己读出来。
    """
    preferred = ""
    for line in (patch or "").replace("\r\n", "\n").split("\n"):
        match = FILE_HEADER.match(line.strip())
        if not match:
            continue
        candidate = match.group(1).strip()
        if candidate in ("/dev/null", ""):
            continue
        if line.strip().startswith("+++"):
            return candidate
        preferred = preferred or candidate
    return preferred


def _split_lines(text: str) -> list[str]:
    if text == "":
        return []
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def parse_hunks(patch: str) -> list[dict]:
    hunks: list[dict] = []
    current: dict | None = None
    body = patch.replace("\r\n", "\n")
    raw_lines = body.split("\n")
    if body.endswith("\n") and raw_lines and raw_lines[-1] == "":
        raw_lines.pop()
    for raw in raw_lines:
        header = HUNK_HEADER.match(raw.strip())
        if header:
            if current is not None:
                hunks.append(current)
            current = {
                "old_start": int(header.group(1)),
                "body": [],
            }
            continue
        if raw.startswith(("--- ", "+++ ", "diff ", "index ", "\\")):
            continue
        if current is None:
            if raw.strip():
                raise PatchError("补丁缺少 @@ 段落头，无法定位修改位置")
            continue
        if raw[:1] in {" ", "-", "+"}:
            current["body"].append((raw[0], raw[1:]))
        elif not raw.strip():
            current["body"].append((" ", ""))
    if current is not None:
        hunks.append(current)
    if not hunks:
        raise PatchError("补丁中没有可应用的段落")
    return hunks


def _matches(lines: list[str], index: int, body: list[tuple[str, str]]) -> bool:
    old_side = [text for op, text in body if op in (" ", "-")]
    if index < 0 or index + len(old_side) > len(lines):
        return False
    return lines[index : index + len(old_side)] == old_side


def _nearby(lines: list[str], index: int, radius: int = 4) -> str:
    """失配位置附近的真实内容，带行号。

    只给"第 N 行附近不匹配"时，模型唯一的办法是重读整个文件再重打一遍补丁；
    把附近几行直接回给它，多数情况下一次就能改对。
    """
    start = max(0, index - radius)
    end = min(len(lines), index + radius + 1)
    return "\n".join(f"{number:>5}| {lines[number - 1]}" for number in range(start + 1, end + 1))


def _locate(lines: list[str], hunk: dict, cursor: int) -> int:
    expected = max(hunk["old_start"] - 1, 0)
    candidates = [expected]
    candidates += [i for i in range(max(cursor, expected - CONTEXT_TOLERANCE), min(len(lines), expected + CONTEXT_TOLERANCE + 1)) if i != expected]
    for index in candidates:
        if _matches(lines, index, hunk["body"]):
            return index
    old_side = [text for op, text in hunk["body"] if op in (" ", "-")]
    wanted = "\n".join(f"{number:>5}? {text}" for number, text in enumerate(old_side[:6], start=hunk["old_start"]))
    raise PatchError(
        f"补丁无法应用：第 {hunk['old_start']} 行附近上下文不匹配。"
        f"补丁期望的内容（前 6 行）：\n{wanted}",
        near=_nearby(lines, expected),
    )


def apply_patch_text(original: str, patch: str) -> str:
    """Apply a unified-diff style patch deterministically."""
    ends_with_newline = original.endswith("\n")
    lines = _split_lines(original)
    out: list[str] = []
    cursor = 0
    for hunk in parse_hunks(patch):
        index = _locate(lines, hunk, cursor)
        out.extend(lines[cursor:index])
        position = index
        for op, text in hunk["body"]:
            if op in (" ", "-"):
                if lines[position] != text:
                    raise PatchError(
                        f"补丁无法应用：第 {position + 1} 行内容与补丁不一致"
                        f"（补丁写的是 {text!r}）",
                        near=_nearby(lines, position),
                    )
                position += 1
            if op in (" ", "+"):
                out.append(text)
        cursor = position
    out.extend(lines[cursor:])
    result = "\n".join(out)
    if result and ends_with_newline:
        result += "\n"
    return result


def unified_diff(original: str, updated: str, path: str) -> str:
    diff = difflib.unified_diff(
        _split_lines(original),
        _split_lines(updated),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        lineterm="",
    )
    return "\n".join(diff)


def backup_relative(task_id: str, relative: str) -> str:
    flattened = relative.replace("/", "__").replace("\\", "__")
    return f"{BACKUP_DIRNAME}/{task_id}/{uuid.uuid4().hex[:8]}/{flattened}"


def write_backup(root: Path, task_id: str, relative: str, content: bytes) -> str:
    backup_rel = backup_relative(task_id, relative)
    target = resolve_for_write(root, backup_rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return backup_rel


def restore_backup(root: Path, backup_rel: str) -> None:
    """Copy a stored pre-change backup over the live file (undo)."""
    from blank_api.services.tools.paths import resolve_existing

    source = resolve_existing(root, backup_rel)
    parts = Path(backup_rel).parts
    relative = Path(*parts[3:]).as_posix().replace("__", "/") if len(parts) > 3 else ""
    if not relative:
        raise PatchError("备份路径无法解析原始文件")
    destination = resolve_for_write(root, relative)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source.read_bytes())


def risky_patch_path(relative: str) -> bool:
    parts = {p.lower() for p in Path(relative).parts}
    return ".git" in parts or BACKUP_DIRNAME in parts or "env" in parts or ".env" in parts


async def _apply_patch(context: ToolContext, arguments: dict) -> ToolOutcome:
    patch_text = str(arguments.get("patch") or "")
    # 路径可以省略：补丁头里通常已经写了，读出来比让模型再补一次便宜。
    relative = str(arguments.get("path") or "").strip() or infer_path(patch_text)
    if not relative:
        return ToolOutcome(
            ok=False,
            status="error",
            message=(
                "没有文件路径：请在 path 参数里给工作区相对路径，"
                "或在补丁开头加一行 `+++ b/相对路径`。"
            ),
        )
    target = resolve_for_write(context.root, relative)
    existed = target.exists()
    original = target.read_text(encoding="utf-8") if existed else ""
    try:
        patched = apply_patch_text(original, patch_text)
    except PatchError as error:
        # 失败里带上真实上下文，模型下一次就能改对。只给"不匹配"三个字，
        # 它只能重读整个文件再盲猜一遍——那一步既是延迟也是成本。
        return ToolOutcome(
            ok=False,
            status="error",
            message=str(error),
            output=str(error),
            data={"path": relative, "near": error.near},
        )
    if existed and patched == original:
        return ToolOutcome(ok=False, status="error", message="补丁未产生任何变更")
    backup_rel = ""
    if existed:
        backup_rel = write_backup(context.root, context.task_id, relative, original.encode("utf-8"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(patched, encoding="utf-8")
    diff = unified_diff(original, patched, relative)
    if contains_secret(diff):
        # 明文密钥只留在用户批准的文件里，入库的 diff 与事件负载必须脱敏。
        diff = redact(diff)
    change_id = str(uuid.uuid4())
    async with context.session_factory() as session:
        session.add(
            FileChange(
                id=change_id,
                task_id=context.task_id,
                path=relative,
                change_type="modified" if existed else "created",
                diff=diff,
                backup_path=backup_rel,
            )
        )
        await session.commit()
    changed_lines = len([line for line in diff.split("\n") if line[:1] in {"+", "-"} and line[1:2] not in {"+", "-"}])
    return ok(
        f"已修改文件：{relative}（{changed_lines} 行变更）",
        path=relative,
        change_type="modified" if existed else "created",
        change_id=change_id,
        backup_path=backup_rel,
        reversible=bool(backup_rel) or not existed,
        diff=diff,
    )


def patch_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="apply_patch",
            description=(
                "对工作区内的文件应用 unified diff 风格补丁（@@ -起始,行数 +起始,行数 @@ 段落，"
                "行首空格为上下文、- 为删除、+ 为新增）。文件不存在时补丁只含新增行即可创建文件。"
                "修改前会自动保存可回滚备份。"
            ),
            input_schema={
                "type": "object",
                "required": ["patch"],
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "工作区相对文件路径；可省略，省略时从补丁的 +++/--- 头推断",
                    },
                    "patch": {"type": "string", "description": "补丁内容"},
                },
            },
            risk_level="medium",
            risk_for=lambda arguments: "high"
            if risky_patch_path(str(arguments.get("path") or infer_path(str(arguments.get("patch") or ""))))
            or contains_secret(str(arguments.get("patch", "")))
            else "medium",
            handler=_apply_patch,
            timeout_seconds=20.0,
        )
    ]
