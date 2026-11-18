"""edit_file / write_file：Claude Code 的 Edit 与 Write 语义。

移植的是判据，不是文案：

* **Edit**：精确字符串替换。``old_string`` 必须与文件内容完全一致且唯一，否则失败；
  ``replace_all`` 才允许替换全部；``old_string == new_string`` 直接判无变更。
  失败时把失配位置附近的真实内容带回去，模型下一次能自己改对。
* **Write**：整文件写入。覆盖已有文件前必须先读过；读过之后文件被改过要先重读。
  新建文件不需要先读。

两者都过 ``file_state`` 的读后写闸门，写前自动备份，成功后落 FileChange。
磁盘侧函数（``_edit_on_disk`` / ``_write_on_disk``）由宿主与 fs-broker 共用——
与 apply_patch 一样，换执行边界不换语义。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping

from blank_api.models.task import FileChange
from blank_api.services.log_safety import contains_secret, redact
from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome, ok
from blank_api.services.tools.file_state import check_write_guard
from blank_api.services.tools.patch import (
    _nearby,
    risky_patch_path,
    unified_diff,
    write_backup,
)
from blank_api.services.tools.paths import resolve_for_write
from blank_api.services.workspace import open_verified


def _redacted_diff(original: str, updated: str, relative: str) -> str:
    diff = unified_diff(original, updated, relative)
    # 明文密钥只留在用户批准的文件里，入库的 diff 与事件负载必须脱敏。
    return redact(diff) if contains_secret(diff) else diff


def _changed_lines(diff: str) -> int:
    return len([line for line in diff.split("\n") if line[:1] in {"+", "-"} and line[1:2] not in {"+", "-"}])


def _near_missing(text: str, needle: str) -> str:
    """not-found 时给最可能是模型想改的那一段：取 needle 第一行做锚点。"""
    lines = text.split("\n")
    probe = next((line.strip() for line in needle.split("\n") if line.strip()), "")
    index = 0
    if probe:
        for number, line in enumerate(lines):
            if probe in line or (len(line.strip()) >= 4 and line.strip() in probe):
                index = number
                break
    return _nearby(lines, index)


def _occurrence_lines(text: str, needle: str, limit: int = 5) -> list[int]:
    lines: list[int] = []
    start = 0
    while len(lines) < limit:
        found = text.find(needle, start)
        if found < 0:
            break
        lines.append(text.count("\n", 0, found) + 1)
        start = found + max(len(needle), 1)
    return lines


def _read_existing(root, relative: str) -> bytes:
    with open_verified(root, relative, mode="rb") as handle:
        return handle.read()


def _edit_pairs(arguments: dict) -> list[tuple[str, str, bool]]:
    """把 ``edits[]`` 与 ``old_string/new_string`` 两种写法归一成替换表。

    Claude 的 Edit 允许一次调用带多段替换（``edits: [{old_string, new_string}]``），
    按给出顺序依次应用。我们原来只有单段写法，模型想改三处就得发三次调用——
    每次都要一次完整往返，实测同一处文件连发三次是最常见的浪费。两种写法
    统一收在这里，handler、磁盘实现、broker 三处都不用各自认一遍。
    """
    edits = arguments.get("edits")
    # 只要传了 edits 就以它为唯一来源：传了空数组是「什么都没给」，
    # 不该悄悄回落到 old_string——那会把一次写错的调用变成一次成功的单段替换。
    if isinstance(edits, list):
        pairs: list[tuple[str, str, bool]] = []
        for item in edits:
            if not isinstance(item, Mapping):
                continue
            pairs.append(
                (
                    str(item.get("old_string") or ""),
                    str(item.get("new_string") or ""),
                    bool(item.get("replace_all")),
                )
            )
        return pairs
    return [
        (
            str(arguments.get("old_string") or ""),
            str(arguments.get("new_string") or ""),
            bool(arguments.get("replace_all")),
        )
    ]


def _edit_on_disk(root, task_id: str, arguments: dict, guard: dict | None = None) -> dict:
    """Edit 的磁盘侧全部动作。返回结构化结果，供宿主与 broker 原样带回。"""
    relative = str(arguments.get("path") or "").strip()
    if not relative:
        return {"missing_path": True}
    pairs = _edit_pairs(arguments)
    if not pairs:
        return {"missing_edits": True, "path": relative}
    for index, (old_string, new_string, _) in enumerate(pairs):
        if not old_string:
            return {"empty_old_string": True, "path": relative, "index": index}
        if old_string == new_string:
            # Claude 原文：No changes to make: old_string and new_string are exactly the same.
            return {"no_op": True, "path": relative, "index": index}
    target = resolve_for_write(root, relative)
    if not target.exists():
        # Edit 只改已有文件。新建走 write_file，这条边界让两条工具的分工可判定。
        return {"file_missing": True, "path": relative}
    try:
        raw = _read_existing(root, relative)
    except FileNotFoundError:
        return {"file_missing": True, "path": relative}
    guard_error = check_write_guard(target, guard, raw)
    if guard_error is not None:
        return {"guard_error": guard_error, "path": relative}
    try:
        original = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {"not_text": True, "path": relative}
    # 模型给出的字符串通常用 \n；先把两边都归一，再按原文件的换行风格写回。
    crlf = "\r\n" in original
    updated = original.replace("\r\n", "\n")
    total = 0
    multi = len(pairs) > 1
    # 依次应用：后一段看得到前一段的结果，所以「先加一行、再改那行」这种写法成立。
    # 这是 Claude 的原语义，也是模型写多段替换时的默认预期。
    for index, (needle_raw, replacement_raw, replace_all) in enumerate(pairs):
        needle = needle_raw.replace("\r\n", "\n")
        replacement = replacement_raw.replace("\r\n", "\n")
        if not needle:
            return {"empty_old_string": True, "path": relative, "total_edits": len(pairs)}
        count = updated.count(needle)
        if count == 0:
            return {
                "not_found": True,
                "path": relative,
                "near": _near_missing(updated, needle),
                **({"index": index, "total_edits": len(pairs)} if multi else {}),
            }
        if count > 1 and not replace_all:
            return {
                "ambiguous": True,
                "path": relative,
                "occurrences": count,
                "lines": _occurrence_lines(updated, needle),
                **({"index": index, "total_edits": len(pairs)} if multi else {}),
            }
        updated = updated.replace(needle, replacement) if replace_all else updated.replace(needle, replacement, 1)
        total += count if replace_all else 1
    count = total
    if crlf:
        updated = updated.replace("\n", "\r\n")
    backup_rel = write_backup(root, task_id, relative, raw)
    payload = updated.encode("utf-8")
    # 读与写走同一个句柄：读到的对象就是被替换的对象，中间没有路径可换。
    with open_verified(root, relative, mode="r+b") as handle:
        handle.seek(0)
        handle.truncate()
        handle.write(payload)
    diff = _redacted_diff(original, updated, relative)
    return {
        "path": relative,
        "existed": True,
        "backup_path": backup_rel,
        "diff": diff,
        "changed_lines": _changed_lines(diff),
        "replacements": count,
    }


def _write_on_disk(root, task_id: str, arguments: dict, guard: dict | None = None) -> dict:
    """Write 的磁盘侧全部动作：整文件写入，已存在则先备份。"""
    relative = str(arguments.get("path") or "").strip()
    if not relative:
        return {"missing_path": True}
    content = arguments.get("content")
    if content is None:
        return {"missing_content": True, "path": relative}
    text = content if isinstance(content, str) else str(content)
    target = resolve_for_write(root, relative)
    existed = target.exists()
    original = ""
    binary = False
    backup_rel = ""
    if existed:
        try:
            raw = _read_existing(root, relative)
        except FileNotFoundError:
            existed = False
            raw = b""
        if existed:
            guard_error = check_write_guard(target, guard, raw)
            if guard_error is not None:
                return {"guard_error": guard_error, "path": relative}
            try:
                original = raw.decode("utf-8")
            except UnicodeDecodeError:
                binary = True
            if not binary and original == text:
                return {"unchanged": True, "path": relative, "existed": True}
            backup_rel = write_backup(root, task_id, relative, raw)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = text.encode("utf-8")
    if existed:
        with open_verified(root, relative, mode="r+b") as handle:
            handle.seek(0)
            handle.truncate()
            handle.write(payload)
    else:
        with open_verified(root, relative, mode="wb", create=True) as handle:
            handle.write(payload)
    diff = "" if binary else _redacted_diff(original, text, relative)
    return {
        "path": relative,
        "existed": existed,
        "backup_path": backup_rel,
        "diff": diff,
        "changed_lines": _changed_lines(diff),
        "binary": binary,
    }


def _routed(context: ToolContext) -> bool:
    from blank_api.services.tools import fs_broker

    return fs_broker.broker_enabled() or bool(getattr(context, "isolation_profile", ""))


async def _call(context: ToolContext, op: str, args: dict) -> dict:
    from blank_api.services.tools import fs_broker

    profile = str(getattr(context, "isolation_profile", "") or "")
    if profile:
        return await fs_broker.call_isolated(
            context.root, context.task_id, op, args, profile_name=profile
        )
    return await fs_broker.call_broker(context.root, context.task_id, op, args)


def _guard_for(context: ToolContext, relative: str) -> dict | None:
    cache = getattr(context, "file_state", None)
    return cache.guard(relative) if cache is not None else None


def _error_outcome(result: dict, marker: dict) -> ToolOutcome:
    path = str(result.get("path") or "")
    data = {"path": path, **marker}
    # 多段替换要说清是第几处没对上：只说"找不到"会让模型把整份 edits 重发一遍，
    # 而实际只需要修那一段。单段调用不显示这个后缀。
    where = ""
    if int(result.get("total_edits") or 0) > 1 and isinstance(result.get("index"), int):
        where = f"（edits 第 {result['index'] + 1} 处，共 {result['total_edits']} 处）"
    if result.get("missing_path"):
        return ToolOutcome(
            ok=False, status="error",
            message="没有文件路径：请传工作区相对路径。", data=data,
        )
    if result.get("missing_edits"):
        return ToolOutcome(
            ok=False, status="error",
            message="edits 是空的：请给出至少一段 old_string/new_string。", data=data,
        )
    if result.get("missing_content"):
        return ToolOutcome(
            ok=False, status="error", message="缺少 content：整文件写入要给出完整内容。", data=data,
        )
    if result.get("empty_old_string"):
        return ToolOutcome(
            ok=False, status="error", message=f"old_string 不能为空{where}：请给出要替换的确切内容。", data=data,
        )
    if result.get("no_op"):
        return ToolOutcome(
            ok=False, status="error",
            message=f"old_string 与 new_string 完全相同{where}，没有可做的修改。", data=data,
        )
    if result.get("file_missing"):
        return ToolOutcome(
            ok=False, status="error",
            message=f"文件不存在：{path}。新建文件请用 write_file。", data=data,
        )
    if result.get("not_text"):
        return ToolOutcome(
            ok=False, status="error",
            message=f"该文件不是 UTF-8 文本，无法精确替换：{path}", data=data,
        )
    if result.get("not_found"):
        near = str(result.get("near") or "")
        return ToolOutcome(
            ok=False, status="error",
            message=(
                f"old_string 在文件里找不到{where}。先把 read_file 的结果原样复制一段（包括缩进与空行），"
                "不要自己重排。"
                + (f"\n最接近的位置：\n{near}" if near else "")
            ),
            output=near,
            data={**data, "near": near},
        )
    if result.get("ambiguous"):
        lines = result.get("lines") or []
        positions = "、".join(str(item) for item in lines) if lines else "多处"
        return ToolOutcome(
            ok=False, status="error",
            message=(
                f"old_string 在文件里出现了 {result.get('occurrences')} 次（{positions}），不唯一{where}。"
                "补上前后几行让它唯一，或设 replace_all=true 全部替换。"
            ),
            data={**data, "occurrences": result.get("occurrences"), "lines": lines},
        )
    if "guard_error" in result:
        guard_error = result["guard_error"]
        return ToolOutcome(
            ok=False, status="error",
            message=str(guard_error.get("message") or "文件状态已变化，先重新读取再修改。"),
            data={"file_state_error": guard_error, **data},
        )
    return ToolOutcome(ok=False, status="error", message="文件操作失败", data=data)


async def _record_change(
    context: ToolContext, relative: str, change_type: str, diff: str, backup_rel: str
) -> str:
    change_id = str(uuid.uuid4())
    async with context.session_factory() as session:
        session.add(
            FileChange(
                id=change_id,
                task_id=context.task_id,
                path=relative,
                change_type=change_type,
                diff=diff,
                backup_path=backup_rel,
            )
        )
        await session.commit()
    return change_id


async def _edit_file(context: ToolContext, arguments: dict) -> ToolOutcome:
    from blank_api.services.tools import fs_broker as broker

    relative = str(arguments.get("path") or "").strip()
    marker: dict = {}
    guard = _guard_for(context, relative)
    if _routed(context):
        request = {**arguments, "task_id": context.task_id, "guard": guard}
        try:
            envelope = await _call(context, "edit_file", request)
        except broker.FsBrokerError as exc:
            return broker.broker_failure_outcome("edit_file", exc)
        result = envelope["result"]["result"]
        marker = broker.broker_success_marker("edit_file", envelope)
    else:
        result = _edit_on_disk(context.root, context.task_id, arguments, guard=guard)
    if any(key in result for key in (
        "missing_path", "missing_edits", "empty_old_string", "no_op", "file_missing", "not_text",
        "not_found", "ambiguous", "guard_error",
    )):
        return _error_outcome(result, marker)
    relative = str(result["path"])
    change_id = await _record_change(
        context, relative, "modified", str(result["diff"]), str(result["backup_path"])
    )
    if context.file_state is not None:
        context.file_state.record_write(context.root, relative)
    return ok(
        f"已修改文件：{relative}（{result['replacements']} 处替换，{result['changed_lines']} 行变更）",
        path=relative,
        change_type="modified",
        change_id=change_id,
        backup_path=str(result["backup_path"]),
        reversible=True,
        replacements=result["replacements"],
        diff=str(result["diff"]),
        **marker,
    )


async def _write_file(context: ToolContext, arguments: dict) -> ToolOutcome:
    from blank_api.services.tools import fs_broker as broker

    relative = str(arguments.get("path") or "").strip()
    marker: dict = {}
    guard = _guard_for(context, relative)
    if _routed(context):
        request = {**arguments, "task_id": context.task_id, "guard": guard}
        try:
            envelope = await _call(context, "write_file", request)
        except broker.FsBrokerError as exc:
            return broker.broker_failure_outcome("write_file", exc)
        result = envelope["result"]["result"]
        marker = broker.broker_success_marker("write_file", envelope)
    else:
        result = _write_on_disk(context.root, context.task_id, arguments, guard=guard)
    if any(key in result for key in ("missing_path", "missing_content", "guard_error")):
        return _error_outcome(result, marker)
    relative = str(result["path"])
    if result.get("unchanged"):
        # 内容一致时不落 FileChange、也不写备份：这次调用没有改变工作区。
        return ok(f"文件内容未变化：{relative}", path=relative, unchanged=True, **marker)
    existed = bool(result["existed"])
    change_type = "modified" if existed else "created"
    change_id = await _record_change(
        context, relative, change_type, str(result["diff"]), str(result["backup_path"])
    )
    if context.file_state is not None:
        context.file_state.record_write(context.root, relative)
    # 覆盖一份已有文件、实际只动了几行——这一次的生成成本已经花掉了，提醒是为下一次：
    # 整份重写要模型把整个文件重新想一遍，而 edit_file 只发改动的那段。
    hint = ""
    if existed and int(result["changed_lines"]) <= 30:
        hint = "\n（这次是整份覆盖；下次只改局部用 edit_file，不必把整份重新生成一遍。）"
    return ok(
        f"已写入文件：{relative}（{'覆盖' if existed else '新建'}，{result['changed_lines']} 行变更）{hint}",
        path=relative,
        change_type=change_type,
        change_id=change_id,
        backup_path=str(result["backup_path"]),
        reversible=existed,
        changed_lines=result["changed_lines"],
        diff=str(result["diff"]),
        **marker,
    )


def editing_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="edit_file",
            description=(
                "对工作区内的文件做精确字符串替换。修改前必须先用 read_file 读过该文件，否则调用会失败。"
                "old_string 必须与文件内容完全一致（包括缩进与空行），并且在文件中唯一，否则调用失败；"
                "确需替换所有出现时把 replace_all 设为 true。old_string 与 new_string 相同时不会产生任何修改，"
                "调用会失败。只改一处小内容时优先用它，不要整文件重写。"
                "同一个文件要改多段时，用 edits 数组一次发出（按顺序应用），不要拆成多次调用。"
                "写前会自动保存可回滚备份。"
            ),
            input_schema={
                "type": "object",
                "required": ["path"],
                "properties": {
                    "path": {"type": "string", "description": "工作区相对文件路径"},
                    "old_string": {"type": "string", "description": "要被替换的确切原文"},
                    "new_string": {"type": "string", "description": "替换后的内容"},
                    "replace_all": {
                        "type": "boolean",
                        "description": "为 true 时替换所有出现；默认只允许唯一匹配",
                    },
                    "edits": {
                        "type": "array",
                        "description": (
                            "一次改多处时用：每项是一段 old_string/new_string，按数组顺序依次应用。"
                            "传了 edits 就不要再传 old_string/new_string。"
                        ),
                        "items": {
                            "type": "object",
                            "required": ["old_string", "new_string"],
                            "properties": {
                                "old_string": {"type": "string", "description": "要被替换的确切原文"},
                                "new_string": {"type": "string", "description": "替换后的内容"},
                                "replace_all": {
                                    "type": "boolean",
                                    "description": "为 true 时替换所有出现；默认只允许唯一匹配",
                                },
                            },
                        },
                    },
                },
            },
            risk_level="medium",
            risk_for=lambda arguments: "high"
            if risky_patch_path(str(arguments.get("path") or ""))
            or contains_secret(str(arguments.get("new_string") or ""))
            else "medium",
            handler=_edit_file,
            timeout_seconds=20.0,
        ),
        ToolDefinition(
            name="write_file",
            description=(
                "把整个文件写成给定内容（新建或整体覆盖）。只在两种情况下用它：新建文件，"
                "或者整篇内容都要重写。改已有文件里的局部，用 edit_file——它只发改动的那一段，"
                "不必把整份文件重新生成一遍。覆盖已有文件前必须先用 read_file 读过它，"
                "文件在读过之后又被改过时会失败，需要重新读。新建文件不需要先读。"
                "写前会自动保存可回滚备份。"
            ),
            input_schema={
                "type": "object",
                "required": ["path", "content"],
                "properties": {
                    "path": {"type": "string", "description": "工作区相对文件路径"},
                    "content": {"type": "string", "description": "要写入的完整文件内容"},
                },
            },
            risk_level="medium",
            risk_for=lambda arguments: "high"
            if risky_patch_path(str(arguments.get("path") or ""))
            or contains_secret(str(arguments.get("content") or ""))
            else "medium",
            handler=_write_file,
            timeout_seconds=20.0,
        ),
    ]
