"""edit_file / write_file：Claude Code 的 Edit 与 Write 语义。

移植的是判据，不是文案：

* **Edit**：精确字符串替换。``old_string`` 必须与文件内容完全一致且唯一，否则失败；
  ``replace_all`` 才允许替换全部；``old_string == new_string`` 直接判无变更。
  失败时把失配位置附近的真实内容带回去，模型下一次能自己改对。
  精确匹配失败后还有两档"看起来一样"的容错：弯引号 ↔ 直引号、``\\uXXXX`` 转义
  字面量。命中的是**文件里的原文片段**，并把命中方式回传——不放宽到空白，
  也不假装成功：内容真的不同时仍然失败。
* **Write**：整文件写入。覆盖已有文件前必须先读过；读过之后文件被改过要先重读。
  新建文件不需要先读。写回时保留原文件的换行风格与 BOM：模型给的内容几乎总是
  LF、无 BOM，照原样写下去，改两行会报成整文件重写。

两者都过 ``file_state`` 的读后写闸门，写前自动备份，成功后落 FileChange。
磁盘侧函数（``_edit_on_disk`` / ``_write_on_disk``）由宿主与 fs-broker 共用——
与 apply_patch 一样，换执行边界不换语义。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping

from blank_api.models.task import FileChange
from blank_api.services.log_safety import contains_secret, redact
from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome, ok
from blank_api.services.tools.file_state import check_write_guard
from blank_api.services.tools.patch import (
    _nearby,
    conform_written as _conform_written,
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


def _diff_counts(diff: str) -> tuple[int, int, int]:
    """从统一 diff 数出 (新增行, 删除行, hunk 数)。

    工具回执不再带全文 diff，但交付/审查仍需要这三个事实；在写盘函数里
    顺手算完，事件与检查点就只留计数。
    """
    added = removed = hunks = 0
    for line in diff.splitlines():
        if line.startswith("@@"):
            hunks += 1
        elif line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed, hunks


def _changed_lines(diff: str) -> int:
    added, removed, _ = _diff_counts(diff)
    return added + removed


def _result_counts(result: dict) -> tuple[int, int, int]:
    """写盘结果里的 (added, removed, hunks)：新结果自带，老/broker 结果从 diff 兜底。"""
    diff = str(result.get("diff") or "")
    added, removed, hunks = _diff_counts(diff)
    return (
        int(result.get("added", added) or 0),
        int(result.get("removed", removed) or 0),
        int(result.get("hunks", hunks) or 0),
    )


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


# 弯引号与直引号的对照。模型从 read_file 里抄一段中文注释时，常常把原文的
# “ ” 写成 " "（或反过来）——两边视觉上几乎一样，字节却不同，精确匹配必失败。
# 折叠成直引号再找，是 Claude 的 Edit 做同一件事的方式（它的 `z_e`）。
_QUOTE_FOLD = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})
_UNICODE_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")


def _unescape_unicode(text: str) -> str:
    """把 needle 里的 ``\\u4e2d`` 字面量还原成字符。模型有时把转义当原文发过来。"""
    return _UNICODE_ESCAPE.sub(lambda match: chr(int(match.group(1), 16)), text)


def _match_once(text: str, needle: str) -> tuple[str, str] | None:
    """在 text 里找 needle 命中的**原文片段**，并说明是靠哪种形态命中的。

    返回原文片段而不只是布尔值：调用方要拿它去做替换，替换的必须是文件里真实
    存在的那几个字符，而不是模型发来的那一串——否则写回去的是"模型以为的原文"。
    """
    if needle in text:
        return needle, ""
    # 两边都折叠再找：错的方向可能是文件用了弯引号、模型发直引号，也可能反过来。
    # 只在 needle 自带引号时折叠是错的——最常见的那一版正是"文件弯、模型直"。
    folded_text = text.translate(_QUOTE_FOLD)
    folded = needle.translate(_QUOTE_FOLD)
    if folded_text != text or folded != needle:
        # 折叠是逐字符 1:1 的（BMP 单字符 → 单字符），索引可以直接回指原文，
        # 命中的片段长度与 needle 相同——替换时必须用它，而不是模型发来的那一串。
        at = folded_text.find(folded)
        if at >= 0:
            return text[at : at + len(folded)], "引号形态"
    if "\\u" in needle:
        decoded = _unescape_unicode(needle)
        if decoded != needle and decoded in text:
            return decoded, "转义还原"
    return None


def _resolve_needle(text: str, needle: str, replacement: str) -> tuple[str, str] | None:
    """这次替换该动哪一段原文。

    删除时优先连行尾换行一起删：``old_string`` 不带换行、文件里是 ``old_string\\n``，
    只删前半段就会留下一行空白——用户看到的是"删了一行，却多了一行空行"。
    Claude 的 Edit 同样先把 needle 补上那个换行再替换。

    但"整行"要真的整行：匹配落在行中间（``keep two`` 里的 ``two``）时不能吞换行，
    否则 ``keep two\\nthree`` 会被粘成 ``keep three``——那是删一个词，不是删一行。
    """
    if replacement == "" and not needle.endswith("\n"):
        found = _match_once(text, needle)
        if found is not None:
            at = text.find(found[0])
            starts_line = at == 0 or text[at - 1] == "\n"
            ends_line = text[at + len(found[0]) :].startswith("\n")
            if starts_line and ends_line:
                return found[0] + "\n", "整行删除"
        return found
    return _match_once(text, needle)


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
    # 哪些段是靠容错匹配上的：要回给模型，否则它永远不知道自己发的原文是错的，
    # 下一处还会照同一个错法再发一遍。
    matched: list[str] = []
    multi = len(pairs) > 1
    # 依次应用：后一段看得到前一段的结果，所以「先加一行、再改那行」这种写法成立。
    # 这是 Claude 的原语义，也是模型写多段替换时的默认预期。
    for index, (needle_raw, replacement_raw, replace_all) in enumerate(pairs):
        needle = needle_raw.replace("\r\n", "\n")
        replacement = replacement_raw.replace("\r\n", "\n")
        if not needle:
            return {"empty_old_string": True, "path": relative, "total_edits": len(pairs)}
        resolved = _resolve_needle(updated, needle, replacement)
        if resolved is None:
            return {
                "not_found": True,
                "path": relative,
                "near": _near_missing(updated, needle),
                **({"index": index, "total_edits": len(pairs)} if multi else {}),
            }
        hit, how = resolved
        count = updated.count(hit)
        if count > 1 and not replace_all:
            return {
                "ambiguous": True,
                "path": relative,
                "occurrences": count,
                "lines": _occurrence_lines(updated, hit),
                **({"index": index, "total_edits": len(pairs)} if multi else {}),
            }
        updated = updated.replace(hit, replacement) if replace_all else updated.replace(hit, replacement, 1)
        total += count if replace_all else 1
        if how:
            matched.append(how)
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
    added, removed, hunks = _diff_counts(diff)
    return {
        "path": relative,
        "existed": True,
        "backup_path": backup_rel,
        "diff": diff,
        "changed_lines": added + removed,
        "added": added,
        "removed": removed,
        "hunks": hunks,
        "replacements": count,
        "matched": matched,
    }


def _conform_append(text: str, original: str) -> str:
    """把要追加的片段调成原文件的形态（换行与 BOM），再原样接在后面。

    与 ``_conform_written`` 的区别：那是"整份新内容"，这是"只追加的一段"。
    BOM 只属于文件开头，片段里的 BOM 必须去掉；原文件末尾没有换行时补一个，
    否则两段会粘成一行。
    """
    fragment = text[1:] if text.startswith("\ufeff") else text
    crlf = original.count("\r\n")
    lf_only = original.count("\n") - crlf
    normalized = fragment.replace("\r\n", "\n")
    if crlf > lf_only:
        normalized = normalized.replace("\n", "\r\n")
    newline = "\r\n" if crlf > lf_only else "\n"
    if (
        original
        and normalized
        and not original.endswith(("\n", "\r"))
        and not normalized.startswith(("\n", "\r"))
    ):
        normalized = newline + normalized
    return normalized


def _write_on_disk(root, task_id: str, arguments: dict, guard: dict | None = None) -> dict:
    """Write 的磁盘侧全部动作：整文件写入或追加，已存在则先备份。"""
    relative = str(arguments.get("path") or "").strip()
    if not relative:
        return {"missing_path": True}
    content = arguments.get("content")
    if content is None:
        return {"missing_content": True, "path": relative}
    text = content if isinstance(content, str) else str(content)
    append = bool(arguments.get("append"))
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
            if binary and append:
                return {
                    "guard_error": "这是二进制文件，不能追加文本；请换一个文本文件或改用整文件写入。",
                    "path": relative,
                }
            if not binary:
                # 先按原文件形态归一再比：模型发的是 LF 内容、文件是 CRLF 时，
                # 不归一就会把「没改」判成「全改」，白写一次还留一份备份。
                if append:
                    text = original + _conform_append(text, original)
                else:
                    text = _conform_written(text, original)
                if original == text:
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
    added, removed, hunks = _diff_counts(diff)
    return {
        "path": relative,
        "existed": existed,
        "backup_path": backup_rel,
        "diff": diff,
        "changed_lines": added + removed,
        "added": added,
        "removed": removed,
        "hunks": hunks,
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
    matched = [item for item in (result.get("matched") or []) if item]
    added, removed, hunks = _result_counts(result)
    if context.file_state is not None:
        context.file_state.record_write(
            context.root, relative, added=added, removed=removed
        )
    # 容错匹配必须说出来：不说，模型就以为自己的 old_string 是对的，下一处照旧发错的。
    how = f"；{'、'.join(dict.fromkeys(matched))}匹配" if matched else ""
    return ok(
        f"已修改文件：{relative}（{result['replacements']} 处替换，{result['changed_lines']} 行变更{how}）",
        path=relative,
        change_type="modified",
        change_id=change_id,
        backup_path=str(result["backup_path"]),
        reversible=True,
        replacements=result["replacements"],
        added=added,
        removed=removed,
        hunks=hunks,
        matched=matched,
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
    # 覆盖一份已有文件、实际只动了几行——这一次的生成成本已经花掉了，提醒是为下一次：
    # 整份重写要模型把整个文件重新想一遍，而 edit_file 只发改动的那段。
    append = bool(arguments.get("append"))
    hint = ""
    if existed and not append and int(result["changed_lines"]) <= 30:
        hint = "\n（这次是整份覆盖；下次只改局部用 edit_file，不必把整份重新生成一遍。）"
    action = "追加" if append and existed else ("覆盖" if existed else "新建")
    added, removed, hunks = _result_counts(result)
    if context.file_state is not None:
        context.file_state.record_write(
            context.root, relative, added=added, removed=removed
        )
    return ok(
        f"已写入文件：{relative}（{action}，{result['changed_lines']} 行变更）{hint}",
        path=relative,
        change_type=change_type,
        change_id=change_id,
        backup_path=str(result["backup_path"]),
        reversible=existed,
        changed_lines=result["changed_lines"],
        added=added,
        removed=removed,
        hunks=hunks,
        **marker,
    )


def editing_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="edit_file",
            description=(
                "对工作区文件做精确字符串替换。修改前必须先用 read_file 读过，否则失败。"
                "old_string 必须与原文完全一致（含缩进/空行）且唯一；要替换所有出现设 replace_all=true。"
                "old_string 与 new_string 相同会失败。只改小段用它，别整文件重写；"
                "同一文件多段用 edits 数组一次发。写前自动备份。"
            ),
            input_schema={
                "type": "object",
                "required": ["path"],
                "properties": {
                    "path": {"type": "string", "description": "工作区相对路径"},
                    "old_string": {"type": "string", "description": "要替换的确切原文"},
                    "new_string": {"type": "string", "description": "替换后的内容"},
                    "replace_all": {"type": "boolean", "description": "true 时替换所有出现；默认唯一匹配"},
                    "edits": {
                        "type": "array",
                        "description": "一次改多处：每项 old_string/new_string，按顺序应用；传了它就别再传顶层 old_string/new_string。",
                        "items": {
                            "type": "object",
                            "required": ["old_string", "new_string"],
                            "properties": {
                                "old_string": {"type": "string"},
                                "new_string": {"type": "string"},
                                "replace_all": {"type": "boolean"},
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
                "把整个文件写成给定内容；append=true 时追加到已有文件末尾（不存在则新建）。"
                "新建、整篇重写或长文分段用它，只改局部用 edit_file。覆盖前必须先用 read_file "
                "读过，读过之后文件又被改过会失败；新建不需要先读，写前自动备份。"
                "内容很长时分节写、用 append 追加，避免一次参数流断掉导致整次写入失败。"
            ),
            input_schema={
                "type": "object",
                "required": ["path", "content"],
                "properties": {
                    "path": {"type": "string", "description": "工作区相对文件路径"},
                    "content": {"type": "string", "description": "完整文件内容；append 时为追加片段"},
                    "append": {
                        "type": "boolean",
                        "description": "为 true 时追加到已有文件末尾（不存在则新建）",
                    },
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
