"""notebook_edit：Claude Code 的 NotebookEdit 语义。

移植的是判据，不是文案：

* 只改**一个单元格**：``replace``（默认）/ ``insert`` / ``delete``。整份重写交给
  write_file，两条工具的分工与 Edit/Write 一致。
* ``cell_id`` 就是 read_file 在 ``<cell id="...">`` 里显示的那个 id。读过的
  notebook 才允许改——和 edit_file / write_file 共用同一道读后写闸门。
* 文件必须是合法 Jupyter notebook JSON（顶层对象带 ``cells`` 数组）。解析不了
  就明确说"这不是 notebook"；把它当普通 JSON 文件改会让 Jupyter 打不开。
* 写回保留原文件的缩进与结尾换行；写前备份、成功后落 FileChange。

磁盘侧函数 ``_notebook_on_disk`` 由宿主与 fs-broker 共用——换执行边界不换语义。
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from blank_api.models.task import FileChange
from blank_api.services.log_safety import contains_secret
from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome, ok
from blank_api.services.tools.editing import (
    _call,
    _changed_lines,
    _guard_for,
    _redacted_diff,
    _routed,
)
from blank_api.services.tools.file_state import check_write_guard
from blank_api.services.tools.patch import risky_patch_path, write_backup
from blank_api.services.tools.paths import resolve_for_write
from blank_api.services.workspace import open_verified

NOTEBOOK_SUFFIX = ".ipynb"
# 单份 notebook 的写上限：超过它连备份都是一次几十兆的复制，失败面比收益大。
MAX_NOTEBOOK_BYTES = 8 * 1024 * 1024
# 每个输出元素最多带多少字、每格最多带几个输出：Read 的用途是理解，不是回放。
MAX_OUTPUT_CHARS = 400
MAX_OUTPUTS = 5
EDIT_MODES = ("replace", "insert", "delete")
CELL_TYPES = ("code", "markdown")


def normalize_notebook_path(root: Path | str, raw: str) -> str:
    """Claude 的 notebook_path 是绝对路径；我们按工作区相对路径落盘。

    工作区内的绝对路径先压成相对路径（模型从 read_file 的回执里抄路径时经常
    是绝对形式），工作区外的一律原样交给 ``resolve_for_write`` 报越界。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    candidate = Path(text)
    if not candidate.is_absolute():
        return text
    root_path = Path(root).resolve()
    try:
        return candidate.resolve().relative_to(root_path).as_posix()
    except (OSError, ValueError):
        return text


def cell_ids(cells: list) -> list[str]:
    """按 Read 的渲染口径给每格一个 id：有就用自己的，没有就合成 ``cell-<序号>``。

    合成的 id 只用于"这一次读取 → 下一次编辑"的定位，不写回文件；这样没有 id 的
    老 notebook 不会因为一次编辑被整份改写。重复 id（不合法的 notebook）后者退到
    序号形式，保证每个 id 唯一、指代明确。
    """
    found: list[str] = []
    used: set[str] = set()
    for index, cell in enumerate(cells):
        value = cell.get("id") if isinstance(cell, dict) else None
        candidate = value if isinstance(value, str) and value else f"cell-{index}"
        if candidate in used:
            candidate = f"cell-{index}"
        suffix = 1
        base = candidate
        while candidate in used:
            candidate = f"{base}-{suffix}"
            suffix += 1
        used.add(candidate)
        found.append(candidate)
    return found


def _source_text(cell: dict) -> str:
    source = cell.get("source")
    if isinstance(source, list):
        return "".join(str(item) for item in source)
    return "" if source is None else str(source)


def _source_for(text: str) -> list[str]:
    """nbformat 的规范形状：按行拆开，除最后一行外每行带换行符。"""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized:
        return []
    return normalized.splitlines(keepends=True)


def _output_text(output: dict) -> str:
    kind = str(output.get("output_type") or "")
    if kind == "stream":
        text = output.get("text")
    elif kind in ("execute_result", "display_data"):
        data = output.get("data") or {}
        text = data.get("text/plain") if isinstance(data, dict) else None
    elif kind == "error":
        name = str(output.get("ename") or "")
        value = str(output.get("evalue") or "")
        return f"{name}: {value}".strip()
    else:
        return ""
    if isinstance(text, list):
        text = "".join(str(item) for item in text)
    return "" if text is None else str(text)


def _clip(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    text = text.rstrip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def render_notebook(content: str, *, path: str = "") -> tuple[str, list[str]] | None:
    """把 notebook JSON 渲染成 Read 的单元格形状；不是 notebook 时返回 None。

    返回 ``(渲染文本, 单元格 id 列表)``。解析失败不在这里报错：Read 对不能解析的
    .ipynb 仍然应该给出原始内容（至少模型能看见文件确实坏了）。
    """
    try:
        book = json.loads(content)
    except (TypeError, ValueError):
        return None
    if not isinstance(book, dict) or not isinstance(book.get("cells"), list):
        return None
    ids = cell_ids(book["cells"])
    chunks: list[str] = []
    for cell, cell_id in zip(book["cells"], ids):
        if not isinstance(cell, dict):
            continue
        kind = str(cell.get("cell_type") or "code")
        chunks.append(f'<cell id="{cell_id}" type="{kind}">')
        source = _source_text(cell).replace("\r\n", "\n").replace("\r", "\n")
        if source:
            chunks.append(source.rstrip("\n"))
        outputs = cell.get("outputs") if isinstance(cell.get("outputs"), list) else []
        rendered = 0
        for output in outputs:
            if rendered >= MAX_OUTPUTS:
                break
            if not isinstance(output, dict):
                continue
            text = _clip(_output_text(output))
            if not text:
                continue
            chunks.append("<output>")
            chunks.append(text)
            chunks.append("</output>")
            rendered += 1
        chunks.append("</cell>")
    header = f"<notebook path=\"{path}\" cells=\"{len(ids)}\">" if path else ""
    tail = "</notebook>" if path else ""
    body = "\n".join(chunks)
    text = "\n".join(part for part in (header, body, tail) if part)
    return text, ids


_INDENT = re.compile(r"\n([ \t]+)\"")


def _detect_style(original: str) -> tuple[int | str | None, tuple[str, str] | None]:
    """从原文里认出缩进与分隔符，写回时沿用，避免整份 diff。"""
    if "\n" not in original.strip():
        return None, (",", ":")
    match = _INDENT.search(original)
    if not match:
        return 1, None
    indent = match.group(1)
    return ("\t" if indent.startswith("\t") else len(indent)), None


def dump_notebook(book: dict, original: str) -> str:
    indent, separators = _detect_style(original)
    text = json.dumps(book, ensure_ascii=False, indent=indent, separators=separators)
    if original.endswith("\n"):
        text += "\n"
    if "\r\n" in original:
        # Windows 下保存过的 notebook 是 CRLF；json.dumps 只出 \n，原样写回会把
        # 整份文件变成"每一行都改了"。字符串里的换行已被 JSON 转义，这里只会
        # 命中排版用的那些换行。
        text = text.replace("\n", "\r\n")
    return text


def _canonical(book: dict, original: str) -> str:
    return dump_notebook(book, original)


def _retag(cell: dict, cell_type: str) -> None:
    cell["cell_type"] = cell_type
    if not isinstance(cell.get("metadata"), dict):
        cell["metadata"] = {}
    if cell_type == "code":
        if not isinstance(cell.get("outputs"), list):
            cell["outputs"] = []
        cell.setdefault("execution_count", None)
    else:
        cell.pop("outputs", None)
        cell.pop("execution_count", None)


def _new_id(taken: list[str], taken_set: set[str]) -> str:
    for _ in range(50):
        candidate = uuid.uuid4().hex[:8]
        if candidate not in taken_set:
            return candidate
    index = len(taken)
    while f"cell-{index}" in taken_set:
        index += 1
    return f"cell-{index}"


def _read_existing(root, relative: str) -> bytes:
    with open_verified(root, relative, mode="rb") as handle:
        return handle.read()


def _notebook_on_disk(root, task_id: str, arguments: dict, guard: dict | None = None) -> dict:
    """NotebookEdit 的磁盘侧全部动作。返回结构化结果，供宿主与 broker 原样带回。"""
    relative = normalize_notebook_path(root, str(arguments.get("notebook_path") or ""))
    if not relative:
        return {"missing_path": True}
    if not relative.lower().endswith(NOTEBOOK_SUFFIX):
        return {"not_a_notebook_path": True, "path": relative}
    mode = str(arguments.get("edit_mode") or "replace")
    if mode not in EDIT_MODES:
        return {"bad_mode": True, "path": relative, "mode": mode}
    cell_id = str(arguments.get("cell_id") or "").strip()
    cell_type = arguments.get("cell_type")
    if cell_type is not None:
        cell_type = str(cell_type)
        if cell_type not in CELL_TYPES:
            return {"bad_cell_type": True, "path": relative, "cell_type": cell_type}
    if mode in ("replace", "delete") and not cell_id:
        return {"missing_cell_id": True, "path": relative, "mode": mode}
    if mode == "insert" and not cell_type:
        return {"missing_cell_type": True, "path": relative}
    if "new_source" not in arguments:
        return {"missing_source": True, "path": relative}
    source = arguments.get("new_source")
    source = "" if source is None else str(source)

    target = resolve_for_write(root, relative)
    if not target.exists():
        return {"file_missing": True, "path": relative}
    try:
        raw = _read_existing(root, relative)
    except FileNotFoundError:
        return {"file_missing": True, "path": relative}
    guard_error = check_write_guard(target, guard, raw)
    if guard_error is not None:
        return {"guard_error": guard_error, "path": relative}
    if len(raw) > MAX_NOTEBOOK_BYTES:
        return {"too_large": True, "path": relative, "size": len(raw)}
    try:
        original = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {"not_text": True, "path": relative}
    try:
        book = json.loads(original)
    except ValueError:
        return {"invalid_json": True, "path": relative}
    if not isinstance(book, dict) or not isinstance(book.get("cells"), list):
        return {"not_a_notebook": True, "path": relative}

    cells = book["cells"]
    ids = cell_ids(cells)
    index = ids.index(cell_id) if cell_id in ids else -1
    if mode in ("replace", "delete") and index < 0:
        return {"cell_missing": True, "path": relative, "cell_id": cell_id, "cells": ids}
    if mode == "insert" and cell_id and index < 0:
        return {"cell_missing": True, "path": relative, "cell_id": cell_id, "cells": ids}

    reformatted = _canonical(book, original) != original
    if mode == "replace":
        cell = cells[index]
        if not isinstance(cell, dict):
            return {"not_a_notebook": True, "path": relative}
        if cell_type and cell_type != cell.get("cell_type"):
            _retag(cell, cell_type)
        cell["source"] = _source_for(source)
        touched = cell_id
    elif mode == "insert":
        position = 0 if not cell_id else index + 1
        taken = set(ids)
        cell = {
            "cell_type": cell_type,
            "metadata": {},
            "source": _source_for(source),
            "id": _new_id(ids, taken),
        }
        if cell_type == "code":
            cell["outputs"] = []
            cell["execution_count"] = None
        cells.insert(position, cell)
        touched = str(cell["id"])
    else:
        cells.pop(index)
        touched = cell_id

    updated = dump_notebook(book, original)
    if updated == original:
        return {"no_op": True, "path": relative}
    backup_rel = write_backup(root, task_id, relative, raw)
    payload = updated.encode("utf-8")
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
        "edit_mode": mode,
        "cell_id": touched,
        "cells": cell_ids(cells),
        "reformatted": reformatted,
    }


_MODE_LABEL = {"replace": "替换", "insert": "插入", "delete": "删除"}


def _error_outcome(result: dict, marker: dict) -> ToolOutcome:
    path = str(result.get("path") or "")
    data = {"path": path, **marker}
    if result.get("missing_path"):
        return ToolOutcome(
            ok=False, status="error",
            message="缺少 notebook_path：请传工作区相对路径（.ipynb 文件）。", data=data,
        )
    if result.get("not_a_notebook_path"):
        return ToolOutcome(
            ok=False, status="error",
            message=f"notebook_path 必须指向 .ipynb 文件：{path}", data=data,
        )
    if result.get("bad_mode"):
        return ToolOutcome(
            ok=False, status="error",
            message=f"edit_mode 只能是 replace / insert / delete，收到：{result.get('mode')}",
            data=data,
        )
    if result.get("bad_cell_type"):
        return ToolOutcome(
            ok=False, status="error",
            message=f"cell_type 只能是 code 或 markdown，收到：{result.get('cell_type')}",
            data=data,
        )
    if result.get("missing_cell_id"):
        return ToolOutcome(
            ok=False, status="error",
            message=(
                f"{result.get('mode')} 需要 cell_id：就是 read_file 结果里 "
                "<cell id=\"...\"> 显示的那个 id。"
            ),
            data=data,
        )
    if result.get("missing_cell_type"):
        return ToolOutcome(
            ok=False, status="error",
            message="insert 需要 cell_type：新增的单元格是 code 还是 markdown。", data=data,
        )
    if result.get("missing_source"):
        return ToolOutcome(
            ok=False, status="error",
            message="缺少 new_source：请给出单元格的新源码（delete 时传空字符串即可）。", data=data,
        )
    if result.get("file_missing"):
        return ToolOutcome(
            ok=False, status="error",
            message=f"notebook 不存在：{path}。本工具只改已有 .ipynb；新建请用 write_file。",
            data=data,
        )
    if result.get("too_large"):
        return ToolOutcome(
            ok=False, status="error",
            message=f"notebook 太大（{result.get('size')} 字节），超过 {MAX_NOTEBOOK_BYTES} 字节上限。",
            data=data,
        )
    if result.get("not_text"):
        return ToolOutcome(
            ok=False, status="error",
            message=f"该文件不是 UTF-8 文本，无法作为 notebook 编辑：{path}", data=data,
        )
    if result.get("invalid_json"):
        return ToolOutcome(
            ok=False, status="error",
            message=(
                f"{path} 不是合法的 JSON，无法按 notebook 编辑。"
                "先读一遍确认它没有损坏；修 JSON 请用 write_file 整份重写。"
            ),
            data=data,
        )
    if result.get("not_a_notebook"):
        return ToolOutcome(
            ok=False, status="error",
            message=(
                f"{path} 的 JSON 里没有 cells 数组，不像 Jupyter notebook。"
                "改普通 JSON 请用 edit_file / write_file。"
            ),
            data=data,
        )
    if result.get("cell_missing"):
        cells = result.get("cells") or []
        hint = "、".join(str(item) for item in cells[:12]) or "（没有单元格）"
        return ToolOutcome(
            ok=False, status="error",
            message=(
                f"单元格 {result.get('cell_id')} 不存在。当前可用的 cell id：{hint}。"
                "先用 read_file 重新读一遍 notebook。"
            ),
            data={**data, "cell_id": result.get("cell_id"), "cells": cells},
        )
    if result.get("no_op"):
        return ToolOutcome(
            ok=False, status="error",
            message="新内容与原来相同，没有可做的修改。", data=data,
        )
    if "guard_error" in result:
        guard_error = result["guard_error"]
        return ToolOutcome(
            ok=False, status="error",
            message=str(guard_error.get("message") or "文件状态已变化，先重新读取再修改。"),
            data={"file_state_error": guard_error, **data},
        )
    return ToolOutcome(ok=False, status="error", message="notebook 编辑失败", data=data)


async def _record_change(
    context: ToolContext, relative: str, diff: str, backup_rel: str
) -> str:
    change_id = str(uuid.uuid4())
    async with context.session_factory() as session:
        session.add(
            FileChange(
                id=change_id,
                task_id=context.task_id,
                path=relative,
                change_type="modified",
                diff=diff,
                backup_path=backup_rel,
            )
        )
        await session.commit()
    return change_id


async def _notebook_edit(context: ToolContext, arguments: dict) -> ToolOutcome:
    from blank_api.services.tools import fs_broker as broker

    relative = normalize_notebook_path(
        context.root, str(arguments.get("notebook_path") or "")
    )
    marker: dict = {}
    guard = _guard_for(context, relative)
    request = {**arguments, "notebook_path": relative, "task_id": context.task_id, "guard": guard}
    if _routed(context):
        try:
            envelope = await _call(context, "notebook_edit", request)
        except broker.FsBrokerError as exc:
            return broker.broker_failure_outcome("notebook_edit", exc)
        result = envelope["result"]["result"]
        marker = broker.broker_success_marker("notebook_edit", envelope)
    else:
        result = _notebook_on_disk(context.root, context.task_id, request, guard=guard)
    if any(key in result for key in (
        "missing_path", "not_a_notebook_path", "bad_mode", "bad_cell_type",
        "missing_cell_id", "missing_cell_type", "missing_source", "file_missing",
        "too_large", "not_text", "invalid_json", "not_a_notebook", "cell_missing",
        "no_op", "guard_error",
    )):
        return _error_outcome(result, marker)
    relative = str(result["path"])
    change_id = await _record_change(
        context, relative, str(result["diff"]), str(result["backup_path"])
    )
    if context.file_state is not None:
        context.file_state.record_write(context.root, relative)
    mode = str(result["edit_mode"])
    note = "；文件原格式不是标准缩进，已按原缩进重排" if result.get("reformatted") else ""
    return ok(
        f"已修改 notebook：{relative}（{_MODE_LABEL.get(mode, mode)}单元格 "
        f"{result['cell_id']}，{result['changed_lines']} 行变更{note}）",
        path=relative,
        change_type="modified",
        change_id=change_id,
        backup_path=str(result["backup_path"]),
        reversible=True,
        edit_mode=mode,
        cell_id=str(result["cell_id"]),
        cells=result.get("cells") or [],
        diff=str(result["diff"]),
        **marker,
    )


def notebook_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="notebook_edit",
            description=(
                "编辑 Jupyter notebook（.ipynb）里的单个单元格：replace（默认）/ insert / delete。"
                "编辑前必须先用 read_file 读过该 notebook——Read 会按 <cell id=\"...\"> 展示单元格，"
                "cell_id 就是那里显示的 id。replace 与 delete 必须给 cell_id；insert 必须给 "
                "cell_type（code 或 markdown），新单元格插在指定 cell 之后，省略 cell_id 时插到最前面。"
                "只改一个单元格时用它，不要用 write_file 整份重写。写前自动保存可回滚备份。"
            ),
            input_schema={
                "type": "object",
                "required": ["notebook_path", "new_source"],
                "properties": {
                    "notebook_path": {
                        "type": "string",
                        "description": "工作区相对路径（也接受工作区内的绝对路径），必须以 .ipynb 结尾",
                    },
                    "cell_id": {
                        "type": "string",
                        "description": "要编辑的单元格 id，即 read_file 的 <cell id=\"...\"> 里显示的那个",
                    },
                    "new_source": {
                        "type": "string",
                        "description": "单元格的新源码；delete 时忽略，但仍需给出（可传空字符串）",
                    },
                    "cell_type": {
                        "type": "string",
                        "enum": ["code", "markdown"],
                        "description": "insert 时必填；replace 时可用来改单元格类型；不传沿用原类型",
                    },
                    "edit_mode": {
                        "type": "string",
                        "enum": ["replace", "insert", "delete"],
                        "description": "默认 replace",
                    },
                },
                "additionalProperties": False,
            },
            risk_level="medium",
            risk_for=lambda arguments: "high"
            if risky_patch_path(str(arguments.get("notebook_path") or ""))
            or contains_secret(str(arguments.get("new_source") or ""))
            else "medium",
            handler=_notebook_edit,
            timeout_seconds=20.0,
        )
    ]
