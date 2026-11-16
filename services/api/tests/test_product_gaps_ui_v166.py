"""UI 对比暴露的四条产品差距：交付结构、验证通道、清场、安全编码。

每一条都先在真实任务事件流里看到过（ui-ours 任务 076ad3a1…）：
- 交付只有「4 个文件」计数，没有文件名与入口，用户按「文件结构 + 怎么打开」验收不了；
- `node _selftest.js` 22 项全过（seq 91），校验通道仍写「未做自动化校验」；
- 模型两次 `del _selftest.js` 被安全策略拒（seq 100/136），临时文件留在工作区；
- 前端把分类/日期直接拼进 innerHTML，注入成立。
"""

from __future__ import annotations

from blank_api.services.agent import delivery, graph, intent
from blank_api.services.tools import shell


def _write(path: str, change_type: str = "created") -> dict:
    return {
        "tool": "write_file",
        "status": "ok",
        "ok": True,
        "data": {"path": path, "change_type": change_type},
        "output": "",
    }


def _shell(command: str, exit_code: int = 0) -> dict:
    ok = exit_code == 0
    return {
        "tool": "run_shell",
        "status": "ok" if ok else "failed",
        "ok": ok,
        "data": {"command": command, "exit_code": exit_code},
        "output": "PASS 空状态\nPASS 新增流水\nRESULT pass=22 fail=0",
    }


def _shell_out(command: str, exit_code: int, output: str) -> dict:
    ok = exit_code == 0
    return {
        "tool": "run_shell",
        "status": "ok" if ok else "failed",
        "ok": ok,
        "data": {"command": command, "exit_code": exit_code},
        "output": output,
    }


# ── 交付：文件名与入口 ────────────────────────────────────────────────────


def test_delivery_lists_files_and_html_entrypoint():
    text = delivery.render(
        delivery.DeliveryFacts(
            completed="记账页交付完成",
            changed_files=3,
            diff_count=4,
            files=("index.html", "styles.css", "app.js"),
        )
    )
    assert "改动：3 个文件（index.html、styles.css、app.js）" in text
    assert "入口 index.html" in text


def test_delivery_caps_the_name_list_but_keeps_the_count():
    files = tuple(f"file{i}.txt" for i in range(7))
    text = delivery.render(delivery.DeliveryFacts(completed="完成", changed_files=7, files=files))
    assert "7 个文件" in text
    assert "file0.txt" in text and "file4.txt" in text
    assert "file5.txt" not in text


def test_delivery_does_not_invent_an_entrypoint_without_html():
    text = delivery.render(
        delivery.DeliveryFacts(completed="完成", changed_files=1, files=("app.py",))
    )
    assert "入口" not in text


# ── 验证：消费本轮已跑通的自测 ────────────────────────────────────────────


def test_self_run_validation_is_adopted_after_last_mutation():
    validation = graph._self_run_validation([_write("app.js"), _shell("node _selftest.js")])
    assert validation is not None
    assert validation["status"] == "ok"
    assert validation["passed"] is True
    assert validation["command"] == "node _selftest.js"
    assert validation["exit_code"] == 0
    assert validation["source"] == "self_run"


def test_self_run_before_last_mutation_is_ignored():
    assert graph._self_run_validation([_shell("node _selftest.js"), _write("app.js")]) is None


def test_non_test_shell_command_is_not_validation():
    assert graph._self_run_validation([_write("app.js"), _shell("node app.js")]) is None


def test_failed_self_run_is_a_failed_validation_not_a_skip():
    validation = graph._self_run_validation(
        [_write("app.js"), _shell("node _selftest.js", exit_code=1)]
    )
    assert validation is not None
    assert validation["status"] == "failed"
    assert validation["passed"] is False


def test_pytest_exit5_is_not_evidence_of_failure():
    """pytest exit 5 = 没收集到测试，不是测试没通过。

    严格评测把隐藏测试移出工作区后，agent 常跑 `pytest --collect-only` 或裸 `pytest`
    自检，拿到 exit 5。旧逻辑把它当失败判据，已经改对的代码被反复「修复」到失败
    （2026-11-16 coding-008 t2 / coding-012 t2 实测）。
    """
    results = [
        _write("fib.py"),
        _shell_out("python -m pytest fib.py -q --collect-only", 5, "no tests ran in 0.01s"),
    ]
    assert graph._self_run_validation(results) is None


def test_pytest_exit5_without_pytest_is_still_failed():
    """exit 5 豁免只给 pytest 的「没收集到测试」，不能顺带放过别家脚本的非零退出。"""
    validation = graph._self_run_validation(
        [_write("app.js"), _shell_out("node _selftest.js", 5, "no tests ran")]
    )
    assert validation is not None
    assert validation["status"] == "failed"


def test_pytest_exit1_stays_failed():
    validation = graph._self_run_validation(
        [_write("fib.py"), _shell_out("pytest -q", 1, "1 failed, 0 passed in 0.02s")]
    )
    assert validation is not None
    assert validation["status"] == "failed"


# ── 清场：只移动本轮新建的临时脚本 ────────────────────────────────────────


def test_scratch_quarantine_moves_only_agent_created_temp_files(tmp_path):
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
    (tmp_path / "_selftest.js").write_text("console.log('x')", encoding="utf-8")
    (tmp_path / "keep.js").write_text("// 用户原有", encoding="utf-8")

    moved = shell.quarantine_scratch_files(
        tmp_path, created=["index.html", "_selftest.js"], keep=[]
    )

    assert [src for src, _ in moved] == ["_selftest.js"]
    assert not (tmp_path / "_selftest.js").exists()
    assert (tmp_path / "index.html").exists()
    assert (tmp_path / "keep.js").exists()
    dest = tmp_path / ".blank" / "scratchpad" / "_selftest.js"
    assert dest.exists()


def test_scratch_quarantine_never_touches_a_file_it_did_not_create(tmp_path):
    (tmp_path / "_selftest.js").write_text("用户自己的脚本", encoding="utf-8")
    assert shell.quarantine_scratch_files(tmp_path, created=[], keep=[]) == []
    assert (tmp_path / "_selftest.js").exists()


def test_scratch_quarantine_keeps_a_user_named_deliverable(tmp_path):
    (tmp_path / "_selftest.js").write_text("用户点名要的", encoding="utf-8")
    moved = shell.quarantine_scratch_files(
        tmp_path, created=["_selftest.js"], keep=["_selftest.js"]
    )
    assert moved == []
    assert (tmp_path / "_selftest.js").exists()


# ── 提示词：安全编码与临时目录 ────────────────────────────────────────────


def test_system_prompt_carries_owasp_and_output_encoding_rules():
    prompt = graph.SYSTEM_PROMPT
    assert "OWASP" in prompt
    assert "innerHTML" in prompt


def test_report_instruction_points_temp_scripts_at_scratchpad():
    assert ".blank/scratchpad" in intent.REPORT_INSTRUCTION


# ── 接线：不是只有纯函数对，节点里真的这么走 ──────────────────────────────


class _Recorder:
    def __init__(self) -> None:
        self.kinds: list[str] = []

    async def add(self, kind: str, node: str, payload: dict | None = None) -> None:
        self.kinds.append(kind)


async def test_validation_node_adopts_self_run_evidence():
    from types import SimpleNamespace

    session = SimpleNamespace(recorder=_Recorder(), readonly=False)
    out = await graph.run_validation_node(
        {"tool_results": [_write("app.js"), _shell("node _selftest.js")]}, session
    )
    assert out["validation"]["source"] == "self_run"
    assert out["validation"]["passed"] is True
    assert out["validation"]["command"] == "node _selftest.js"
    assert "validation.completed" in session.recorder.kinds


async def test_validation_node_failed_self_run_enters_repair():
    from types import SimpleNamespace

    session = SimpleNamespace(recorder=_Recorder(), readonly=False)
    out = await graph.run_validation_node(
        {"tool_results": [_write("app.js"), _shell("node _selftest.js", exit_code=1)]}, session
    )
    assert out["validation"]["status"] == "failed"
    assert out["validation"]["passed"] is False
    assert out["repair_rounds"] == 1


async def test_summarize_quarantine_drops_scratch_from_delivery_facts(tmp_path):
    from types import SimpleNamespace

    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
    (tmp_path / "_selftest.js").write_text("console.log('x')", encoding="utf-8")
    session = SimpleNamespace(
        context=SimpleNamespace(root=str(tmp_path)), recorder=_Recorder()
    )
    state = {"tool_results": [_write("index.html"), _write("_selftest.js")]}

    out = await graph._quarantine_scratch(state, session, "summarize_result")

    assert [item["data"]["path"] for item in out["tool_results"]] == ["index.html"]
    assert (tmp_path / ".blank" / "scratchpad" / "_selftest.js").exists()
    assert session.recorder.kinds == ["workspace.quarantined"]


async def test_quarantine_leaves_a_lone_scratch_file_alone(tmp_path):
    """本轮只新建了一个临时命名脚本时，它可能就是用户要的交付物，不能收走。"""
    from types import SimpleNamespace

    (tmp_path / "verify_backup.py").write_text("print('ok')", encoding="utf-8")
    session = SimpleNamespace(
        context=SimpleNamespace(root=str(tmp_path)), recorder=_Recorder()
    )
    state = {"tool_results": [_write("verify_backup.py")]}

    out = await graph._quarantine_scratch(state, session, "summarize_result")

    assert (tmp_path / "verify_backup.py").exists()
    assert out is state
    assert session.recorder.kinds == []
