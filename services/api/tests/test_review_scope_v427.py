"""审查要有确定的输入边界。

原来审查的 prompt 只说"请审查工具结果、测试结论和验收条件"——范围全靠审查模型
自己从对话里翻。翻漏了就是"审了一遍什么也没看到"，翻多了就去挑无关文件的刺。

DSH 的做法是先产出确定性范围（change-scope 只输出路径，不做语义判断），语义审查
交给 reviewer。这里抄同一层。
"""

from __future__ import annotations

from blank_api.services.agent.graph import _review_scope


def _state(*rows: dict) -> dict:
    return {"tool_results": list(rows)}


def _write(path: str, diff: str, kind: str = "modified", ok: bool = True) -> dict:
    return {"ok": ok, "tool": "write_file", "data": {"path": path, "change_type": kind, "diff": diff}}


def test_the_scope_lists_every_touched_file_with_its_size():
    text, paths = _review_scope(_state(
        _write("deliverables/a.html", "@@ -0,0 +1,12 @@\n" + "+x\n" * 12, "created"),
        _write("src/x.py", "@@ -1 +1,2 @@\n-a\n+b\n+c"),
    ))

    assert paths == ["deliverables/a.html", "src/x.py"]
    assert "+12/-0" in text and "created" in text
    assert "+2/-1" in text


def test_the_scratchpad_is_out_of_scope():
    """`.blank/scratchpad` 是模型自己的自测区，不是交付物；审它等于凭空挑刺。"""
    _, paths = _review_scope(_state(
        _write("deliverables/a.html", "+x"),
        _write(".blank/scratchpad/t.js", "+x", "created"),
    ))

    assert paths == ["deliverables/a.html"]


def test_failed_writes_are_not_in_scope():
    """写失败的文件根本没变，把它列进审查范围会让审查对着一份不存在的改动说话。"""
    _, paths = _review_scope(_state(_write("a.py", "+x", ok=False), _write("b.py", "+y")))

    assert paths == ["b.py"]


def test_read_only_result_rows_are_ignored():
    """read_file / glob 也带 data.path，但它们不是改动。"""
    rows = [
        {"ok": True, "tool": "read_file", "data": {"path": "a.py"}},
        {"ok": True, "tool": "glob", "data": {"path": "src"}},
    ]
    text, paths = _review_scope({"tool_results": rows})

    assert paths == []
    assert "没有改动任何文件" in text


def test_an_empty_scope_says_so_instead_of_handing_over_an_empty_list():
    text, paths = _review_scope({"tool_results": []})

    assert paths == []
    assert "没有改动任何文件" in text, "空清单必须有一句话，否则审查模型会去别处找东西审"


def test_the_same_file_touched_twice_is_merged():
    """多次编辑同一个文件：审查范围里应该只有一条，行数累加。"""
    text, paths = _review_scope(_state(
        _write("a.py", "@@ -1 +1 @@\n+a"),
        _write("a.py", "@@ -5 +5 @@\n+b\n+c"),
    ))

    assert paths == ["a.py"]
    assert text.count("a.py") == 1
    assert "+3/-0" in text
