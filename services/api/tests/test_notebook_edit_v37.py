"""NotebookEdit 对齐 Claude 的判据（Q6）。

四层，每层都能单独失败：

1. **纯函数**：cell id 的取用/合成/去重、Read 的 ``<cell id="...">`` 渲染、源码按行
   归一、写回保留原缩进与结尾换行。
2. **磁盘语义**：replace / insert / delete、改类型时清掉代码格字段、错误形状
   （缺 cell_id、cell 不存在、不是 JSON、不是 notebook）、写前备份。
3. **读后写闸门**：没读过不许改；读过之后文件被改过也不许改——与 edit_file /
   write_file 共用同一条规则，Read 渲染出来的 id 直接可用于 NotebookEdit。
4. **工具面与接线**：schema 必填/枚举、参数级风险、注册表/权限/场景/子代理/
   证据链各名单、fs-broker 分派。

另钉一条：Read 对 .ipynb 的渲染与编辑用的 id 必须同一口径——两边漂移时
"Read 看到 id、NotebookEdit 说没有"会变成无法解释的失败。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from blank_api.db import Base, get_engine, get_session_factory
from blank_api.models.project import Project
from blank_api.models.task import FileChange, Task
from blank_api.services.agent import graph
from blank_api.services.agent.payload import DROP_DIFF_TOOLS
from blank_api.services.agent.stall import MUTATING_TOOLS as STALL_MUTATING
from blank_api.services.agent.verifier import _WRITE_TOOLS as VERIFIER_WRITE_TOOLS
from blank_api.services.agent.scenarios import DOCS, DEV, write_allowed
from blank_api.services.permissions import (
    ALLOW,
    ASK,
    DENY,
    EDIT_TOOLS,
    MODE_ACCEPT_EDITS,
    MODE_PLAN,
    PermissionPolicy,
)
from blank_api.services.tools import build_default_tools
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.file_state import FileStateCache
from blank_api.services.tools.filesystem import _read_file
from blank_api.services.tools.gateway import PATH_WRITE_TOOLS, ToolGateway, _write_target
from blank_api.services.tools.notebook import (
    NOTEBOOK_SUFFIX,
    _notebook_edit,
    _notebook_on_disk,
    cell_ids,
    dump_notebook,
    normalize_notebook_path,
    render_notebook,
)
from blank_api.services.tools.registry import DEFERRED_TOOLS, LEAN_TOOLS, PARALLEL_SAFE_TOOLS, ToolRegistry


# --------------------------------------------------------------------------- #
# 装置
# --------------------------------------------------------------------------- #


def _cell(kind: str = "code", source: str = "x = 1\n", cid: str | None = None, **extra):
    cell = {
        "cell_type": kind,
        "metadata": {},
        "source": source.splitlines(keepends=True),
    }
    if cid:
        cell["id"] = cid
    if kind == "code":
        cell["outputs"] = extra.pop("outputs", [])
        cell["execution_count"] = extra.pop("execution_count", None)
    cell.update(extra)
    return cell


def _book(*cells) -> dict:
    return {
        "cells": list(cells),
        "metadata": {"kernelspec": {"name": "python3"}},
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def _write_book(root: Path, relative: str = "demo.ipynb", book: dict | None = None, *, indent: int = 1) -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(book or _book(_cell(cid="a")), ensure_ascii=False, indent=indent) + "\n", encoding="utf-8")
    return target


def _read_cache(cache: FileStateCache, root: Path, relative: str) -> None:
    raw = (root / relative).read_bytes()
    stat = (root / relative).stat()
    cache.record_read(
        relative,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        sha256=hashlib.sha256(raw).hexdigest() if stat.st_size <= 512 * 1024 else "",
    )


def _edit(root: Path, **arguments):
    return _notebook_on_disk(root, "task-1", {"notebook_path": "demo.ipynb", **arguments})


def _loaded(root: Path, relative: str = "demo.ipynb") -> dict:
    return json.loads((root / relative).read_text(encoding="utf-8"))


@pytest.fixture
async def db():
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    return get_session_factory()


async def _seed_task(factory, task_id: str) -> None:
    async with factory() as session:
        session.add(Project(id=f"p-{task_id}", name="p"))
        session.add(Task(id=task_id, project_id=f"p-{task_id}", title="t", goal="g"))
        await session.commit()


# --------------------------------------------------------------------------- #
# 1. 纯函数
# --------------------------------------------------------------------------- #


def test_cell_ids_use_real_ids_and_synthesize_missing_ones():
    cells = [_cell(cid="real"), _cell(), _cell(cid="")]
    assert cell_ids(cells) == ["real", "cell-1", "cell-2"]


def test_cell_ids_disambiguate_duplicates_so_every_reference_is_unambiguous():
    cells = [_cell(cid="same"), _cell(cid="same"), _cell()]
    assert cell_ids(cells) == ["same", "cell-1", "cell-2"]


def test_render_uses_the_same_cell_ids_and_shows_type_and_source():
    book = _book(_cell("markdown", "# title\n", cid="m1"), _cell(cid="c1"))
    text, ids = render_notebook(json.dumps(book), path="demo.ipynb")

    assert ids == ["m1", "c1"]
    assert '<cell id="m1" type="markdown">' in text
    assert "# title" in text
    assert '<cell id="c1" type="code">' in text
    assert "x = 1" in text
    assert text.index('id="m1"') < text.index('id="c1"')


def test_render_includes_textual_outputs_and_caps_them():
    outputs = [
        {"output_type": "stream", "name": "stdout", "text": ["hello\n"]},
        {"output_type": "execute_result", "data": {"text/plain": ["42"]}},
        {"output_type": "error", "ename": "ValueError", "evalue": "bad"},
    ]
    text, _ = render_notebook(json.dumps(_book(_cell(cid="a", outputs=outputs))))

    assert "<output>" in text
    assert "hello" in text and "42" in text and "ValueError: bad" in text
    long = _cell(cid="b", outputs=[{"output_type": "stream", "text": "y" * 5000}])
    rendered, _ = render_notebook(json.dumps(_book(long)))
    assert "y" * 5000 not in rendered
    assert "y" * 500 not in rendered


def test_render_returns_none_for_broken_or_foreign_json():
    assert render_notebook("not json") is None
    assert render_notebook('{"cells": "nope"}') is None
    assert render_notebook("[1, 2]") is None


def test_dump_keeps_indent_and_trailing_newline():
    root = Path()
    book = _book(_cell(cid="a"))
    two = json.dumps(book, ensure_ascii=False, indent=2) + "\n"
    assert dump_notebook(book, two) == two
    assert dump_notebook(book, json.dumps(book) + "\n").endswith("\n")
    assert dump_notebook(book, json.dumps(book)).count("\n") == 0


def test_dump_preserves_crlf_so_a_windows_notebook_is_not_rewritten_line_by_line():
    book = _book(_cell(cid="a"))
    crlf = json.dumps(book, ensure_ascii=False, indent=1).replace("\n", "\r\n") + "\r\n"
    assert dump_notebook(book, crlf) == crlf


def test_absolute_path_inside_the_workspace_is_accepted(tmp_path: Path):
    inside = tmp_path / "demo.ipynb"
    assert normalize_notebook_path(tmp_path, str(inside)) == "demo.ipynb"
    outside = tmp_path.parent / "elsewhere.ipynb"
    assert normalize_notebook_path(tmp_path, str(outside)) == str(outside)


# --------------------------------------------------------------------------- #
# 2. 磁盘语义
# --------------------------------------------------------------------------- #


def test_replace_rewrites_one_cell_and_keeps_outputs(tmp_path: Path):
    _write_book(tmp_path, book=_book(_cell(cid="a"), _cell("markdown", "text\n", cid="b")))
    result = _edit(tmp_path, cell_id="b", new_source="new text\n")

    book = _loaded(tmp_path)
    assert result["edit_mode"] == "replace"
    assert book["cells"][1]["source"] == ["new text\n"]
    assert book["cells"][0]["source"] == ["x = 1\n"]


def test_replace_can_change_the_cell_type_and_drops_code_only_fields(tmp_path: Path):
    _write_book(tmp_path, book=_book(_cell(cid="a", outputs=[{"output_type": "stream", "text": "hi"}])))
    _edit(tmp_path, cell_id="a", new_source="# heading\n", cell_type="markdown")

    cell = _loaded(tmp_path)["cells"][0]
    assert cell["cell_type"] == "markdown"
    assert "outputs" not in cell and "execution_count" not in cell


def test_insert_after_the_named_cell_and_at_the_front_without_one(tmp_path: Path):
    _write_book(tmp_path, book=_book(_cell(cid="a"), _cell(cid="b")))
    first = _edit(tmp_path, edit_mode="insert", cell_id="a", cell_type="markdown", new_source="note\n")
    second = _edit(tmp_path, edit_mode="insert", cell_type="code", new_source="y = 2\n")

    cells = _loaded(tmp_path)["cells"]
    assert [cell["source"] for cell in cells] == [["y = 2\n"], ["x = 1\n"], ["note\n"], ["x = 1\n"]]
    assert cells[0]["cell_type"] == "code"
    assert cells[2]["cell_type"] == "markdown"
    assert first["cell_id"] != second["cell_id"]
    assert first["cell_id"] not in {"a", "b"} and second["cell_id"] not in {"a", "b"}


def test_delete_removes_exactly_one_cell(tmp_path: Path):
    _write_book(tmp_path, book=_book(_cell(cid="a"), _cell("markdown", "gone\n", cid="b"), _cell(cid="c")))
    _edit(tmp_path, edit_mode="delete", cell_id="b", new_source="")

    assert [cell["id"] for cell in _loaded(tmp_path)["cells"]] == ["a", "c"]


def test_missing_cell_lists_the_available_ids(tmp_path: Path):
    _write_book(tmp_path, book=_book(_cell(cid="a")))
    result = _edit(tmp_path, cell_id="nope", new_source="x\n")

    assert result["cell_missing"] is True
    assert result["cells"] == ["a"]


def test_required_arguments_are_enforced_before_touching_the_file(tmp_path: Path):
    _write_book(tmp_path)
    before = (tmp_path / "demo.ipynb").read_bytes()

    assert _edit(tmp_path, edit_mode="delete", new_source="")["missing_cell_id"] is True
    assert _edit(tmp_path, cell_id="a", edit_mode="replace")["missing_source"] is True
    assert (
        _edit(tmp_path, edit_mode="insert", new_source="x\n")["missing_cell_type"] is True
    )
    assert _edit(tmp_path, cell_id="a", new_source="x\n", edit_mode="append")["bad_mode"] is True
    assert (
        _edit(tmp_path, cell_id="a", new_source="x\n", cell_type="raw")["bad_cell_type"] is True
    )
    assert (tmp_path / "demo.ipynb").read_bytes() == before


def test_non_notebook_paths_and_broken_json_are_refused(tmp_path: Path):
    (tmp_path / "notes.txt").write_text("{}", encoding="utf-8")
    assert _notebook_on_disk(tmp_path, "t", {"notebook_path": "notes.txt", "new_source": "x"})[
        "not_a_notebook_path"
    ] is True

    (tmp_path / "broken.ipynb").write_text("{oops", encoding="utf-8")
    assert _notebook_on_disk(
        tmp_path, "t", {"notebook_path": "broken.ipynb", "cell_id": "a", "new_source": "x"}
    )["invalid_json"] is True

    (tmp_path / "flat.ipynb").write_text('{"cells": 1}', encoding="utf-8")
    assert _notebook_on_disk(
        tmp_path, "t", {"notebook_path": "flat.ipynb", "cell_id": "a", "new_source": "x"}
    )["not_a_notebook"] is True

    assert _notebook_on_disk(
        tmp_path, "t", {"notebook_path": "nope.ipynb", "cell_id": "a", "new_source": "x"}
    )["file_missing"] is True


def test_replacing_with_identical_source_is_a_no_op_and_leaves_the_file_alone(tmp_path: Path):
    _write_book(tmp_path)
    before = (tmp_path / "demo.ipynb").read_bytes()
    result = _edit(tmp_path, cell_id="a", new_source="x = 1\n")

    assert result["no_op"] is True
    assert (tmp_path / "demo.ipynb").read_bytes() == before


def test_a_successful_edit_writes_a_restorable_backup(tmp_path: Path):
    _write_book(tmp_path)
    before = (tmp_path / "demo.ipynb").read_bytes()
    result = _edit(tmp_path, cell_id="a", new_source="x = 2\n")

    backup = tmp_path / result["backup_path"]
    assert backup.read_bytes() == before
    assert result["changed_lines"] >= 1
    assert "x = 2" in result["diff"]


async def test_the_handler_records_a_file_change_and_reports_the_edited_cell(tmp_path: Path, db):
    task_id = f"t-{uuid.uuid4().hex[:8]}"
    await _seed_task(db, task_id)
    _write_book(tmp_path)
    cache = FileStateCache()
    _read_cache(cache, tmp_path, "demo.ipynb")
    context = ToolContext(root=tmp_path, task_id=task_id, session_factory=db, file_state=cache)

    outcome = await _notebook_edit(
        context, {"notebook_path": "demo.ipynb", "cell_id": "a", "new_source": "z = 9\n"}
    )

    assert outcome.ok is True, outcome.message
    assert outcome.data["cell_id"] == "a"
    assert outcome.data["edit_mode"] == "replace"
    assert outcome.data["change_type"] == "modified"
    assert "替换单元格" in outcome.output
    async with db() as session:
        rows = list((await session.execute(
            select(FileChange).where(FileChange.task_id == task_id)
        )).scalars())
    assert [row.path for row in rows] == ["demo.ipynb"]
    # 写成功后状态重记：下一次编辑不需要再读，而且记的是新文件而不是改写前那份。
    guard = cache.guard("demo.ipynb")
    raw = (tmp_path / "demo.ipynb").read_bytes()
    assert guard["was_read"] is True
    assert guard["size"] == len(raw)
    assert guard["sha256"] == hashlib.sha256(raw).hexdigest()


async def test_the_handler_maps_every_error_shape_to_an_actionable_message(tmp_path: Path, db):
    task_id = f"t-{uuid.uuid4().hex[:8]}"
    await _seed_task(db, task_id)
    _write_book(tmp_path)
    cache = FileStateCache()
    context = ToolContext(root=tmp_path, task_id=task_id, session_factory=db, file_state=cache)

    unread = await _notebook_edit(
        context, {"notebook_path": "demo.ipynb", "cell_id": "a", "new_source": "q\n"}
    )
    assert unread.ok is False
    assert unread.data["file_state_error"]["code"] == "not_read"

    _read_cache(cache, tmp_path, "demo.ipynb")
    missing = await _notebook_edit(
        context, {"notebook_path": "demo.ipynb", "cell_id": "zzz", "new_source": "q\n"}
    )
    assert missing.ok is False
    assert "zzz" in missing.message and "a" in missing.message

    no_source = await _notebook_edit(context, {"notebook_path": "demo.ipynb", "cell_id": "a"})
    assert no_source.ok is False
    assert "new_source" in no_source.message


# --------------------------------------------------------------------------- #
# 3. 读后写闸门与 Read 渲染
# --------------------------------------------------------------------------- #


async def test_read_renders_cells_so_the_model_can_see_the_ids_to_edit(tmp_path: Path):
    _write_book(tmp_path, book=_book(_cell("markdown", "# title\n", cid="m1"), _cell(cid="c1")))
    cache = FileStateCache()
    context = ToolContext(root=tmp_path, task_id="t", session_factory=None, file_state=cache)

    outcome = await _read_file(context, {"path": "demo.ipynb"})

    assert outcome.ok is True
    assert outcome.data["notebook"] is True
    assert outcome.data["cells"] == ["m1", "c1"]
    assert '<cell id="m1"' in outcome.output and '<cell id="c1"' in outcome.output
    assert cache.guard("demo.ipynb")["was_read"] is True


async def test_read_falls_back_to_raw_text_for_a_broken_notebook(tmp_path: Path):
    (tmp_path / "broken.ipynb").write_text("{not json", encoding="utf-8")
    context = ToolContext(root=tmp_path, task_id="t", session_factory=None)

    outcome = await _read_file(context, {"path": "broken.ipynb"})

    assert outcome.ok is True
    assert outcome.output == "{not json"
    assert "notebook" not in outcome.data


async def test_read_with_offset_and_limit_returns_the_raw_line_window(tmp_path: Path):
    _write_book(tmp_path)
    context = ToolContext(root=tmp_path, task_id="t", session_factory=None)

    outcome = await _read_file(context, {"path": "demo.ipynb", "offset": 1, "limit": 1000})

    assert outcome.ok is True
    assert "<cell" not in outcome.output
    assert '"cells"' in outcome.output
    assert "notebook" not in outcome.data


def test_editing_needs_a_prior_read_and_a_fresh_one(tmp_path: Path):
    _write_book(tmp_path, book=_book(_cell(cid="a")))
    cache = FileStateCache()

    refused = _notebook_on_disk(
        tmp_path, "t", {"notebook_path": "demo.ipynb", "cell_id": "a", "new_source": "x\n"},
        guard=cache.guard("demo.ipynb"),
    )
    assert refused["guard_error"]["code"] == "not_read"

    _read_cache(cache, tmp_path, "demo.ipynb")
    ok = _notebook_on_disk(
        tmp_path, "t", {"notebook_path": "demo.ipynb", "cell_id": "a", "new_source": "x\n"},
        guard=cache.guard("demo.ipynb"),
    )
    assert "guard_error" not in ok

    _read_cache(cache, tmp_path, "demo.ipynb")
    _write_book(tmp_path, book=_book(_cell(cid="a")), indent=2)  # 外部改动
    stale = _notebook_on_disk(
        tmp_path, "t", {"notebook_path": "demo.ipynb", "cell_id": "a", "new_source": "y\n"},
        guard=cache.guard("demo.ipynb"),
    )
    assert stale["guard_error"]["code"] == "stale"


# --------------------------------------------------------------------------- #
# 4. 工具面与接线
# --------------------------------------------------------------------------- #


def _tool():
    return next(tool for tool in build_default_tools() if tool.name == "notebook_edit")


def test_schema_pins_the_required_fields_and_enums():
    schema = _tool().input_schema
    assert schema["required"] == ["notebook_path", "new_source"]
    props = schema["properties"]
    assert props["edit_mode"]["enum"] == ["replace", "insert", "delete"]
    assert props["cell_type"]["enum"] == ["code", "markdown"]
    assert schema["additionalProperties"] is False


def test_risk_is_medium_but_rises_for_sensitive_paths_and_plaintext_secrets():
    tool = _tool()
    assert tool.risk_level == "medium"
    assert tool.resolve_risk({"notebook_path": "demo.ipynb", "new_source": "x = 1"}) == "medium"
    assert tool.resolve_risk({"notebook_path": ".git/demo.ipynb", "new_source": "x = 1"}) == "high"
    assert tool.resolve_risk(
        {"notebook_path": "demo.ipynb", "new_source": 'token = "sk-abcdefghijklmnopqrstuvwx"'}
    ) == "high"


def test_the_tool_is_core_and_searchable_but_not_parallel_or_lean():
    assert "notebook_edit" in {tool.name for tool in build_default_tools()}
    assert "notebook_edit" not in DEFERRED_TOOLS
    assert "notebook_edit" not in PARALLEL_SAFE_TOOLS
    assert "notebook_edit" not in LEAN_TOOLS

    registry = ToolRegistry(build_default_tools())
    for query in ("notebook", "ipynb", "jupyter", "单元格", "笔记本"):
        assert "notebook_edit" in [item.name for item in registry.search(query)[0]], query


def test_permission_lists_treat_it_as_a_file_write():
    assert "notebook_edit" in EDIT_TOOLS
    assert "notebook_edit" in PATH_WRITE_TOOLS
    assert "notebook_edit" in DROP_DIFF_TOOLS
    assert "notebook_edit" in graph.MUTATING_TOOLS
    assert "notebook_edit" in STALL_MUTATING
    assert "notebook_edit" in VERIFIER_WRITE_TOOLS

    from blank_api.services.agent.planner import _TOOLS_BY_KIND
    from blank_api.services.permissions import ARGUMENT_SCOPED_TOOLS, path_scope
    from blank_api.services.subagent import ISOLATED_TOOLS

    assert "notebook_edit" in ISOLATED_TOOLS
    assert "notebook_edit" in _TOOLS_BY_KIND["edit"]

    assert "notebook_edit" in ARGUMENT_SCOPED_TOOLS
    # 文件级授权必须落在 notebook 路径上，不能退化成"整个工具"
    assert path_scope({"notebook_path": "src/demo.ipynb"}) == "src/demo.ipynb"

    args = {"notebook_path": "demo.ipynb", "new_source": "x"}
    tool = _tool()
    plan = PermissionPolicy(mode=MODE_PLAN).decide(tool, args)
    assert plan.outcome == DENY and plan.rule == "mode:plan"
    accept = PermissionPolicy(mode=MODE_ACCEPT_EDITS).decide(tool, args)
    assert accept.outcome == ALLOW
    risky = PermissionPolicy(mode=MODE_ACCEPT_EDITS).decide(
        tool, {"notebook_path": ".git/demo.ipynb", "new_source": "x"}
    )
    assert risky.outcome == ASK


def test_gateway_reads_notebook_path_for_the_scenario_extension_gate():
    assert _write_target("notebook_edit", {"notebook_path": "a.ipynb"}) == "a.ipynb"
    gateway = ToolGateway(build_default_tools(), scenario="docs")
    assert gateway._path_allowed("notebook_edit", {"notebook_path": "a.ipynb"}) is False
    assert gateway._path_allowed("notebook_edit", {"notebook_path": "a.md"}) is True
    # 开发场景不按后缀限制
    assert write_allowed(DEV, "a.ipynb") is True
    assert write_allowed(DOCS, "a.ipynb") is False


def test_the_broker_dispatches_the_same_disk_semantics(tmp_path: Path):
    from blank_api.services.tools import fs_broker

    _write_book(tmp_path, book=_book(_cell(cid="a")))
    envelope = fs_broker._op_dispatch(
        tmp_path,
        {
            "op": "notebook_edit",
            "args": {
                "notebook_path": "demo.ipynb",
                "cell_id": "a",
                "new_source": "broker = True\n",
                "task_id": "t",
                "guard": None,
            },
        },
    )

    result = envelope["result"]
    assert result["edit_mode"] == "replace"
    assert _loaded(tmp_path)["cells"][0]["source"] == ["broker = True\n"]


def test_the_file_state_gate_covers_the_notebook_suffix():
    assert NOTEBOOK_SUFFIX == ".ipynb"
