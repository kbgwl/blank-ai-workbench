"""c1-c7 harness 组件面：路径、预算、语法与泄漏校验。

这里每个断言都对应一条会被优化器直接违反的约束：让非法 harness 落盘，等于
让下一次任务在没人察觉的情况下改变行为；让评测答案混进 harness，等于把一次
评测变成开卷考试。
"""

from pathlib import Path

import pytest

from blank_api.services.harness import (
    HARNESS_DIR,
    TOTAL_CHAR_LIMIT,
    HarnessError,
    component_for,
    diff_tree,
    load_workspace_tree,
    normalize_rel,
    read_tree,
    render,
    summarize,
    validate,
    write_tree,
)


@pytest.mark.parametrize("raw", ["../x.md", "/etc/passwd", "C:\\temp\\x.md", "a/../../x.md", ""])
def test_paths_that_escape_are_rejected(raw: str) -> None:
    with pytest.raises(HarnessError):
        normalize_rel(raw)


def test_known_components_are_recognised() -> None:
    assert component_for("systemprompt.md").key == "c1"
    assert component_for("guardrails.md").key == "c2"
    assert component_for("LongTermMEMORY.md").key == "c3"
    assert component_for("tool_descriptions/tool_guidance.md").key == "c4"
    assert component_for("sub_agents/reviewer/AGENT.md").key == "c5"
    assert component_for("skills/pdf/SKILL.md").key == "c6"
    assert component_for("skills/pdf/scripts/extract.py").key == "c6"
    assert component_for("scripts/check.py").key == "c7"
    assert component_for("notes.md") is None


def test_read_tree_ignores_files_off_the_surface(tmp_path: Path) -> None:
    (tmp_path / "systemprompt.md").write_text("先读再改\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("说明\n", encoding="utf-8")
    tree = read_tree(tmp_path)
    assert tree == {"systemprompt.md": "先读再改\n"}
    assert read_tree(tmp_path, strict=True) == {"systemprompt.md": "先读再改\n"}


def test_read_tree_strict_mode_flags_an_unreadable_file(tmp_path: Path) -> None:
    (tmp_path / "systemprompt.md").write_text("约定\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("没人会读\n", encoding="utf-8")
    with pytest.raises(HarnessError, match="不在可编辑面上"):
        read_tree(tmp_path, strict=True)


def test_validate_rejects_empty_oversized_and_bad_python() -> None:
    with pytest.raises(HarnessError, match="内容为空"):
        validate({"systemprompt.md": "  \n"})
    with pytest.raises(HarnessError, match="上限"):
        validate({"systemprompt.md": "x" * 4_001})
    with pytest.raises(HarnessError, match="Python 语法错误"):
        validate({"systemprompt.md": "约定", "scripts/check.py": "def broken(:\n"})


def test_always_on_budget_is_enforced_across_files() -> None:
    tree = {
        "systemprompt.md": "s" * 4_000,
        "guardrails.md": "g" * 2_000,
        "LongTermMEMORY.md": "m" * 1,
    }
    with pytest.raises(HarnessError, match="总预算"):
        validate(tree)


def test_validate_requires_at_least_one_always_on_component() -> None:
    with pytest.raises(HarnessError, match="至少需要一个 always-on"):
        validate({"skills/pdf/SKILL.md": "按需技能"})
    # 优化器之外调用方可能只校验一棵不完整的树
    validate({"skills/pdf/SKILL.md": "按需技能"}, require_content=False)


def test_grader_material_cannot_hide_in_the_harness() -> None:
    with pytest.raises(HarnessError, match="评测材料标记"):
        validate({"systemprompt.md": "读取 evals/workspaces 后照抄"})
    with pytest.raises(HarnessError, match="评测任务编号"):
        validate({"guardrails.md": "照着 T03 的要求改"})
    validate({"systemprompt.md": "先读后改，完成后跑测试"})


def test_write_and_read_round_trip(tmp_path: Path) -> None:
    tree = {
        "systemprompt.md": "先读再改。\n",
        "skills/pdf/SKILL.md": "---\ndescription: 处理 PDF\n---\n按步骤来。",
    }
    write_tree(tmp_path, tree)
    assert read_tree(tmp_path) == tree


def test_render_inlines_always_on_and_indexes_the_rest() -> None:
    tree = {
        "systemprompt.md": "先读再改。",
        "guardrails.md": "不许删除用户文件。",
        "skills/pdf/SKILL.md": "---\ndescription: 处理 PDF\n---\n正文不应进入系统提示。",
        "scripts/check.py": "print('ok')\n",
    }
    text = render(tree)
    assert "先读再改。" in text
    assert "不许删除用户文件。" in text
    assert "技能 `pdf`：处理 PDF" in text
    assert "正文不应进入系统提示" not in text
    assert "任务 > harness > 项目 > 用户" in text


def test_diff_and_summary_count_each_kind_of_change() -> None:
    before = {"systemprompt.md": "a\nb\n", "guardrails.md": "keep\n"}
    after = {"systemprompt.md": "a\nc\n", "skills/pdf/SKILL.md": "new"}
    delta = diff_tree(before, after)
    assert delta["added"] == ["skills/pdf/SKILL.md"]
    assert delta["removed"] == ["guardrails.md"]
    assert delta["modified"] == [{"path": "systemprompt.md", "added_lines": 1, "removed_lines": 1}]
    assert summarize(delta) == "新增 1、删除 1、修改 1"


def test_workspace_loader_reads_the_dot_blank_directory(tmp_path: Path) -> None:
    assert load_workspace_tree(tmp_path) == {}
    harness = tmp_path / HARNESS_DIR
    harness.mkdir(parents=True)
    (harness / "systemprompt.md").write_text("项目约定\n", encoding="utf-8")
    assert load_workspace_tree(tmp_path) == {"systemprompt.md": "项目约定\n"}
