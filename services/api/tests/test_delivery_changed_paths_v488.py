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

from blank_api.services.agent import delivery
from blank_api.services.agent.graph import _delivery_facts


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
