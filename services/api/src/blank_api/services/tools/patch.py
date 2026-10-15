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


class PatchError(ValueError):
    pass


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


def _locate(lines: list[str], hunk: dict, cursor: int) -> int:
    expected = max(hunk["old_start"] - 1, 0)
    candidates = [expected]
    candidates += [i for i in range(max(cursor, expected - CONTEXT_TOLERANCE), min(len(lines), expected + CONTEXT_TOLERANCE + 1)) if i != expected]
    for index in candidates:
        if _matches(lines, index, hunk["body"]):
            return index
    raise PatchError(f"补丁无法应用：第 {hunk['old_start']} 行附近上下文不匹配")


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
                    raise PatchError(f"补丁无法应用：第 {position + 1} 行内容已被改动")
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
    relative = arguments["path"]
    target = resolve_for_write(context.root, relative)
    existed = target.exists()
    original = target.read_text(encoding="utf-8") if existed else ""
    patched = apply_patch_text(original, arguments["patch"])
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
                "required": ["path", "patch"],
                "properties": {
                    "path": {"type": "string", "description": "工作区相对文件路径"},
                    "patch": {"type": "string", "description": "补丁内容"},
                },
            },
            risk_level="medium",
            risk_for=lambda arguments: "high"
            if risky_patch_path(str(arguments.get("path", "")))
            or contains_secret(str(arguments.get("patch", "")))
            else "medium",
            handler=_apply_patch,
            timeout_seconds=20.0,
        )
    ]
