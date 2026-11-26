"""交付的改动边界：从"总结说改了哪些"换成"可核对的路径集合"。

来历：Better Harness 判「本轮改了哪些文件只有 agent 自述」——graph.py 校验时其实
算过 ``covered_mutations`` 与 ``covered_fingerprint``（用来判"校验之后又改了没有"），
但这组事实只在校验节点内部用，没有进交付；用户看到的改动清单来自模型总结，可以漏、
也可以多写。

判据钉在这两处：

* ``as_dict`` 不带 ``changed_paths`` → 第 1 条红（前端只能回去解析文本）；
* ``_delivery_facts`` 只认工具结果 → 第 2 条红（shell 里新建的文件漏掉，名单不全）。
"""

from __future__ import annotations

from pathlib import Path

from blank_api.services.agent import delivery
from blank_api.services.agent.graph import (
    _deleted_paths,
    _delivery_facts,
    _review_git_facts,
    _review_scope,
)


def test_structured_payload_carries_changed_paths() -> None:
    facts = delivery.DeliveryFacts(
        completed="改好了",
        changed_files=2,
        files=("a.py", "b.py"),
        changed_paths=("a.py", "b.py", "gen/schema.py"),
    )
    payload = delivery.as_dict(facts)
    assert payload["changed_paths"] == ["a.py", "b.py", "gen/schema.py"]
    # 计数与名单必须是同一份事实：只给数字，用户没法验收。
    assert payload["changed_files"] == 2


def test_payload_falls_back_to_files_when_paths_absent() -> None:
    facts = delivery.DeliveryFacts(changed_files=1, files=("only.py",))
    assert delivery.as_dict(facts)["changed_paths"] == ["only.py"]


def test_delivery_facts_merge_files_seen_by_validation_fingerprint() -> None:
    """工具结果里只有 a.py；shell 里生成的 b.py 只在指纹里——两边都要进改动边界。"""
    state = {
        "tool_results": [
            {
                "ok": True,
                "tool": "write_file",
                "data": {"path": "a.py", "change_type": "modified", "diff": "@@ -1 +1 @@\n"},
            }
        ],
        "validation": {
            "command": "pytest -q",
            "status": "ok",
            "exit_code": 0,
            "covered_mutations": 1,
            "covered_files": ["a.py"],
            "covered_fingerprint": {"a.py": "h1", "b.py": "h2"},
        },
    }
    facts = _delivery_facts("改好了。\n", state)
    assert facts.changed_paths == ("a.py", "b.py")
    assert facts.changed_files == 2, "计数要跟着边界走，不能还按工具结果算"
    payload = delivery.as_dict(facts)
    assert payload["changed_paths"] == ["a.py", "b.py"]
    # 四行正文里也要看得见这两个名字，而不是只给一个数字。
    assert "a.py" in payload["text"] and "b.py" in payload["text"]


def test_delivery_facts_use_receipt_counts_when_diff_is_absent() -> None:
    """写类工具只回执计数：交付仍要能算改动文件与 hunk 数，不能依赖全文 diff。"""
    state = {
        "tool_results": [
            {
                "ok": True,
                "tool": "write_file",
                "data": {
                    "path": "a.py",
                    "change_type": "modified",
                    "added": 3,
                    "removed": 1,
                    "hunks": 2,
                },
            }
        ],
        "validation": {},
    }

    facts = _delivery_facts("改好了。\n", state)

    assert facts.changed_paths == ("a.py",)
    assert facts.diff_count == 2


def test_review_scope_uses_receipt_counts_when_diff_is_absent() -> None:
    """审查范围同样只靠回执计数：+3/-1 要出现在给审查模型的清单里。"""
    state = {
        "tool_results": [
            {
                "ok": True,
                "tool": "edit_file",
                "data": {
                    "path": "a.py",
                    "change_type": "modified",
                    "added": 3,
                    "removed": 1,
                    "hunks": 2,
                },
            }
        ]
    }

    text, paths = _review_scope(state)

    assert paths == ["a.py"]
    assert "+3/-1" in text


def test_no_op_note_is_not_promoted_to_needs_user() -> None:
    """「无需处理」是说明不是交办；写进「需要你」等于让用户处理一件没事的事。"""
    from blank_api.services.agent.graph import _report_sections

    report = (
        "结论\n- 5 个文件已按类型复制，原件全部保留\n"
        "下一步\n- 目录列表中文显示乱码是 cmd 代码页所致，路径本身正常，无需处理"
    )
    conclusion, needs = _report_sections(report)
    assert "5 个文件" in conclusion
    assert needs == ""
    _, real = _report_sections("结论\n- 已复制。\n下一步\n- 确认是否保留原件")
    assert real == "确认是否保留原件"
    # 生成文档/PPT 后让用户打开看看排版，是可选项，不是接手任务。
    _, optional = _report_sections(
        "结论\n- 巡检复盘.pptx 已生成\n下一步\n- 打开文件看一遍排版，个别行换行位置更稳"
    )
    assert optional == ""
    _, edit_later = _report_sections(
        "结论\n- 巡检复盘.pptx 已生成\n下一步\n- 修改文案需改 build_ppt.py 后重跑 python build_ppt.py"
    )
    assert edit_later == ""


def test_snapshot_diff_reports_shell_side_changes(tmp_path) -> None:
    """shell 里 copy/生成的文件不在工具回执里，要靠快照比对补进改动边界。

    真机 daily-001：结论说复制了 5 个文件，改动行却只写「1 个文件（整理说明.md）」，
    因为 copy 是 run_shell 干的，write_file 回执里没有它们。
    """
    from blank_api.services.sandbox import SnapshotStore

    root = tmp_path / "repo"
    root.mkdir()
    (root / "keep.txt").write_text("same", encoding="utf-8")
    (root / "edit.txt").write_text("before", encoding="utf-8")
    (root / "gone.txt").write_text("bye", encoding="utf-8")
    store = SnapshotStore(tmp_path / "snaps")
    record = store.capture(str(root), reason="test", task_id="t")

    (root / "edit.txt").write_text("after and longer", encoding="utf-8")
    (root / "整理").mkdir()
    (root / "整理" / "copy.txt").write_text("copied", encoding="utf-8")
    (root / "gone.txt").unlink()

    changed = set(store.changed_since(record["id"], str(root)))
    assert {"edit.txt", "整理/copy.txt", "gone.txt"} <= changed
    assert "keep.txt" not in changed


def test_extra_paths_join_the_change_boundary() -> None:
    """快照比对出的 shell 产物与工具回执合并，重复路径只记一次。"""
    state = {
        "tool_results": [
            {
                "ok": True,
                "tool": "write_file",
                "data": {"path": "整理说明.md", "change_type": "added", "diff": ""},
            }
        ]
    }
    facts = _delivery_facts(
        "完成。\n",
        state,
        extra_paths=["整理/文本/a.txt", "整理说明.md", "整理/文本/a.txt"],
    )
    assert facts.changed_paths == ("整理说明.md", "整理/文本/a.txt")
    assert facts.changed_files == 2
    assert "整理/文本/a.txt" in delivery.as_dict(facts)["text"]


def test_deleted_paths_ignore_unrelated_changes_outside_the_workspace(tmp_path) -> None:
    """任务跑在仓库子目录时，仓库别处的删除不能算到它头上。

    真机：演示工作区 `demo/e9` 在 kbgai 仓库里，仓库里另一个早先删掉的
    `web/src/components/theme-toggle.tsx` 被算成了本任务的改动。
    """
    import subprocess

    repo = tmp_path / "repo"
    workspace = repo / "demo" / "e9"
    workspace.mkdir(parents=True)
    (repo / "outside.py").write_text("outside", encoding="utf-8")
    (workspace / "inside.py").write_text("inside", encoding="utf-8")
    git = lambda *args: subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)
    assert git("init", "-q").returncode == 0
    git("add", "-A")
    assert git("-c", "user.name=t", "-c", "user.email=t@e", "-c", "commit.gpgsign=false", "commit", "-qm", "init").returncode == 0

    (repo / "outside.py").unlink()
    (workspace / "inside.py").unlink()

    assert _deleted_paths(str(workspace)) == ["inside.py"]


def test_deleted_files_join_the_change_boundary(tmp_path) -> None:
    """shell 里 rm 掉的文件也要算改动：否则交付里的"改动 N 个文件"会漏掉它。"""
    import subprocess

    root = tmp_path / "repo"
    root.mkdir()
    git = lambda *args: subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)
    assert git("init", "-q").returncode == 0
    (root / "keep.py").write_text("x = 1\n", encoding="utf-8")
    (root / "gone.py").write_text("y = 2\n", encoding="utf-8")
    git("add", "-A")
    assert git("-c", "user.name=t", "-c", "user.email=t@e", "-c", "commit.gpgsign=false", "commit", "-qm", "init").returncode == 0
    (root / "gone.py").unlink()

    state = {
        "tool_results": [
            {
                "ok": True,
                "tool": "write_file",
                "data": {"path": "keep.py", "change_type": "modified", "diff": "@@ -1 +1 @@\n"},
            }
        ],
        "validation": {"command": "pytest -q", "status": "ok", "exit_code": 0, "covered_mutations": 1},
    }
    facts = _delivery_facts("改好了。\n", state, str(root))
    assert "gone.py" in facts.changed_paths
    assert facts.changed_files == 2


def _git_repo(tmp_path) -> tuple[Path, Path, str]:
    """建一个带初始提交的仓库；工作区取仓库子目录，覆盖前缀换算。"""
    import subprocess

    repo = tmp_path / "repo"
    workspace = repo / "demo" / "e9"
    workspace.mkdir(parents=True)
    (workspace / "tracked.py").write_text("v1\n", encoding="utf-8")
    git = lambda *args: subprocess.run(  # noqa: E731 - 测试夹具，就地定义更清楚
        ["git", *args], cwd=str(repo), capture_output=True, text=True
    )
    assert git("init", "-q").returncode == 0
    git("add", "-A")
    assert (
        git(
            "-c", "user.name=t", "-c", "user.email=t@e",
            "-c", "commit.gpgsign=false", "commit", "-qm", "init",
        ).returncode
        == 0
    )
    base = git("rev-parse", "HEAD").stdout.strip()
    return repo, workspace, base


def test_review_git_facts_groups_workspace_changes(tmp_path) -> None:
    """四组路径是硬事实：工作区内的未暂存/未跟踪要分组，仓库别处的不算。"""
    repo, workspace, base = _git_repo(tmp_path)
    (repo / "outside.py").write_text("outside\n", encoding="utf-8")
    (workspace / "tracked.py").write_text("v2\n", encoding="utf-8")
    (workspace / "new.py").write_text("n\n", encoding="utf-8")

    facts = _review_git_facts(str(workspace), base)

    groups = facts["paths"]
    assert facts["base_sha"] == base
    assert facts["head_sha"] == base
    assert groups["unstaged"] == ["tracked.py"]
    assert groups["untracked"] == ["new.py"]
    assert groups["staged"] == []
    assert groups["committed"] == []
    assert "outside.py" not in sum(groups.values(), [])


def test_review_git_facts_lists_commits_since_task_start(tmp_path) -> None:
    """任务里自己提交过：基点是开局 HEAD，committed 组要把它列出来。"""
    repo, workspace, base = _git_repo(tmp_path)
    (workspace / "later.py").write_text("later\n", encoding="utf-8")
    import subprocess

    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@e",
         "-c", "commit.gpgsign=false", "commit", "-qm", "later"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )

    facts = _review_git_facts(str(workspace), base)

    assert facts["merge_base_sha"] == base
    assert facts["head_sha"] != base
    assert facts["paths"]["committed"] == ["later.py"]
