"""审查要有确定的输入边界。

原来审查的 prompt 只说"请审查工具结果、测试结论和验收条件"——范围全靠审查模型
自己从对话里翻。翻漏了就是"审了一遍什么也没看到"，翻多了就去挑无关文件的刺。

DSH 的做法是先产出确定性范围（change-scope 只输出路径，不做语义判断），语义审查
交给 reviewer。这里抄同一层。
"""

from __future__ import annotations

from blank_api.services.agent.graph import _review_done_when, _review_scope, _scope_relative


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


# --------------------------------------------------------------------------- #
# 任务 3：done_when 是交付判据，必须逐条有证据
# --------------------------------------------------------------------------- #
def test_done_when_requires_met_with_evidence():
    """说 met 但证据为空不算证明：这正是「没跑校验却声称通过」的形态。"""
    rows, unproven = _review_done_when(
        ["页面能打开", "测试通过"],
        [
            {"text": "页面能打开", "status": "met", "evidence": "browser_check 无控制台错误"},
            {"text": "测试通过", "status": "met", "evidence": ""},
        ],
    )

    assert [row["status"] for row in rows] == ["met", "met"]
    assert unproven == ["测试通过"]


def test_done_when_missing_or_not_met_is_unproven():
    """缺条目、unverifiable、unmet 都算未证明；不能靠相似度替模型圆场。"""
    criteria = ["接口返回 200", "README 更新"]
    rows, unproven = _review_done_when(
        criteria,
        [{"text": "接口返回 200", "status": "unverifiable", "evidence": "没法直连"}],
    )

    assert unproven == ["接口返回 200", "README 更新"]
    assert rows[0]["status"] == "unverifiable"
    assert rows[1]["status"] == "missing"


def test_done_when_accepts_positional_answer_without_text():
    """审查模型只给同序数组时按位置对回，不因为少了 text 就整批判死。"""
    rows, unproven = _review_done_when(
        ["a", "b"],
        [
            {"status": "met", "evidence": "文件存在"},
            {"status": "met", "evidence": "命令通过"},
        ],
    )

    assert unproven == []
    assert [row["text"] for row in rows] == ["a", "b"]


def test_scope_relative_strips_prefix_and_drops_harness_paths():
    """git 输出是仓库相对路径；审查范围要回到工作区口径，并排除 harness 目录。"""
    assert _scope_relative("repo/sub/a.py", "repo/sub") == "a.py"
    assert _scope_relative("repo/other/a.py", "repo/sub") == ""
    assert _scope_relative("repo/sub/.blank/plans/p.md", "repo/sub") == ""
    assert _scope_relative("repo/sub/.blank/scratchpad/t.js", "repo/sub") == ""
