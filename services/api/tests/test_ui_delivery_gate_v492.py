"""前端交付闸门：改了页面就必须在最新代码上跑过 browser_check。

真机 7ce98397 / 37032c7b：Agent 交了一个前端导航全坏的 shopbench，靠人工复核才发现。
提示词里写「交付前用 browser_check」不够——模型可以跳过，验证节点照样放行。
这里把要求变成机器判据：没有最新代码之后的检查回执、或检查报错，就判失败并回执行节点。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from blank_api.services.agent.graph import route_after_validation, run_validation_node
from blank_api.services.agent.planner import repair_hint
from blank_api.services.agent.transition import explain


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    async def add(self, kind: str, node: str, payload: dict | None = None) -> None:
        self.events.append((kind, node, payload or {}))


class _Context:
    def __init__(self, root: Path) -> None:
        self.root = root


class _Session:
    def __init__(self, root: Path) -> None:
        self.recorder = _Recorder()
        self.context = _Context(root)
        self.readonly = False
        self.hook_context: list[dict] = []


def _write(path: str) -> dict:
    return {
        "tool": "write_file",
        "status": "ok",
        "ok": True,
        "data": {"path": path, "change_type": "modified"},
    }


def _browser(
    *,
    ok: bool = True,
    url: str = "file:///tmp/public/index.html",
    console: list[dict] | None = None,
    failed: list[dict] | None = None,
    asserts: list[dict] | None = None,
    steps: list[dict] | None = None,
) -> dict:
    return {
        "tool": "browser_check",
        "status": "ok" if ok else "failed",
        "ok": ok,
        "message": "" if ok else "页面打开了，但控制台报错或断言/动作不通过",
        "data": {
            "url": url,
            "console": console or [],
            "failed_requests": failed or [],
            "asserts": asserts
            if asserts is not None
            else [{"selector": "body", "count": 1, "ok": True}],
            "steps": steps if steps is not None else [{"do": "click", "ok": True}],
        },
    }


def _state(results: list[dict], objective: str = "做前端页面") -> dict:
    return {
        "goal_spec": {"objective": objective, "done_when": [], "done_checks": []},
        "tool_results": results,
        "repair_rounds": 0,
    }


def test_ui_write_without_browser_check_fails_and_routes_back(tmp_path: Path) -> None:
    session = _Session(tmp_path)
    update = asyncio.run(
        run_validation_node(_state([_write("public/index.html")]), session)
    )

    validation = update["validation"]
    assert validation["status"] == "failed"
    assert validation["passed"] is False
    assert validation["command"] == "ui_check"
    assert "browser_check" in validation["output_tail"]
    assert update["repair_rounds"] == 1
    assert update["ui_repair_rounds"] == 1
    assert "browser_check" in update["repair_hint"]
    assert "reload" in update["repair_hint"]
    kinds = [kind for kind, _, _ in session.recorder.events]
    assert "ui.check_failed" in kinds
    assert "repair.started" in kinds
    assert any(
        item.get("event") == "repair.started" and "browser_check" in item.get("text", "")
        for item in session.hook_context
    )


def test_ui_failure_after_budget_exhausted_gets_one_reserved_repair(tmp_path: Path) -> None:
    session = _Session(tmp_path)
    state = {
        "budget_exhausted": True,
        "validation": {"status": "failed", "command": "ui_check"},
        "repair_rounds": 1,
        "ui_repair_rounds": 1,
    }
    assert route_after_validation(state, session) == "execute_action"
    assert explain("run_validation", "execute_action", state)["rule"] == "ui_repair_reserve"

    # 第二次 UI 自检仍失败时不再无限续杯，按预算收尾进审查。
    state["ui_repair_rounds"] = 2
    assert route_after_validation(state, session) == "review_result"


def test_non_ui_validation_after_budget_exhausted_still_goes_review(tmp_path: Path) -> None:
    session = _Session(tmp_path)
    state = {
        "budget_exhausted": True,
        "validation": {"status": "failed", "command": "pytest"},
        "repair_rounds": 1,
        "ui_repair_rounds": 0,
    }
    assert route_after_validation(state, session) == "review_result"


def test_validation_auto_smoke_covers_when_model_never_called_browser(
    tmp_path: Path, monkeypatch
) -> None:
    async def fake_smoke(root, **_kwargs):
        return {"status": "passed", "url": "http://127.0.0.1:4321/", "auto": True, "asserts": 1, "steps": 1}

    monkeypatch.setattr("blank_api.services.agent.ui_smoke.auto_ui_check", fake_smoke)
    session = _Session(tmp_path)
    update = asyncio.run(
        run_validation_node(_state([_write("public/index.html")]), session)
    )

    assert update["validation"]["ui_check"]["status"] == "passed"
    assert update["validation"]["ui_check"]["auto"] is True
    assert any(kind == "ui.check_auto_passed" for kind, _, _ in session.recorder.events)


def test_browser_check_before_the_last_ui_write_is_stale(tmp_path: Path) -> None:
    session = _Session(tmp_path)
    update = asyncio.run(
        run_validation_node(
            _state([_browser(), _write("public/index.html")]), session
        )
    )

    assert update["validation"]["status"] == "failed"
    assert update["validation"]["command"] == "ui_check"
    assert "最新代码" in update["validation"]["output_tail"]


def test_browser_check_after_the_last_ui_write_passes(tmp_path: Path) -> None:
    session = _Session(tmp_path)
    update = asyncio.run(
        run_validation_node(
            _state([_write("public/index.html"), _browser()]), session
        )
    )

    validation = update["validation"]
    # 没有测试配置时既有结论是 skipped；前端自检通过就保持原结论，不额外加修复轮。
    assert validation["status"] == "skipped"
    assert validation["ui_check"]["status"] == "passed"
    assert validation["ui_check"]["steps"] == 1
    assert "repair_rounds" not in update
    assert any(kind == "ui.check_passed" for kind, _, _ in session.recorder.events)


def test_failed_browser_check_blocks_delivery(tmp_path: Path) -> None:
    session = _Session(tmp_path)
    update = asyncio.run(
        run_validation_node(
            _state([_write("public/index.html"), _browser(ok=False)]), session
        )
    )

    assert update["validation"]["status"] == "failed"
    assert update["validation"]["command"] == "ui_check"
    assert "页面仍有问题" in update["validation"]["output_tail"]


def test_missing_browser_is_an_environment_skip_not_a_page_failure(tmp_path: Path) -> None:
    row = _browser(ok=False)
    row["status"] = "error"
    row["output"] = "没有找到 node 运行时，浏览器检查不可用（设置 BLANK_NODE 指向 node.exe）"
    session = _Session(tmp_path)
    update = asyncio.run(
        run_validation_node(_state([_write("public/index.html"), row]), session)
    )

    assert update["validation"]["status"] == "skipped"
    assert update["validation"]["ui_check"]["status"] == "unavailable"
    assert "repair_rounds" not in update
    assert any(kind == "ui.check_unavailable" for kind, _, _ in session.recorder.events)
    assert all(kind != "ui.check_failed" for kind, _, _ in session.recorder.events)


def test_console_errors_block_even_if_the_probe_said_ok(tmp_path: Path) -> None:
    session = _Session(tmp_path)
    update = asyncio.run(
        run_validation_node(
            _state(
                [
                    _write("public/index.html"),
                    _browser(console=[{"level": "error", "text": "switchView is not defined"}]),
                ]
            ),
            session,
        )
    )

    assert update["validation"]["status"] == "failed"
    assert "switchView is not defined" in update["validation"]["output_tail"]


def test_failed_requests_block_delivery(tmp_path: Path) -> None:
    session = _Session(tmp_path)
    update = asyncio.run(
        run_validation_node(
            _state(
                [
                    _write("public/index.html"),
                    _browser(failed=[{"url": "/api/state", "error": "net::ERR_FAILED"}]),
                ]
            ),
            session,
        )
    )

    assert update["validation"]["status"] == "failed"
    assert "/api/state" in update["validation"]["output_tail"]


def test_opening_without_asserting_anything_is_not_verification(tmp_path: Path) -> None:
    session = _Session(tmp_path)
    update = asyncio.run(
        run_validation_node(
            _state([_write("public/index.html"), _browser(asserts=[])]), session
        )
    )

    assert update["validation"]["status"] == "failed"
    assert "没有断言" in update["validation"]["output_tail"]


def test_opening_without_any_action_is_not_verification(tmp_path: Path) -> None:
    session = _Session(tmp_path)
    update = asyncio.run(
        run_validation_node(
            _state([_write("public/index.html"), _browser(steps=[])]), session
        )
    )

    assert update["validation"]["status"] == "failed"
    assert "reload" in update["validation"]["output_tail"]


def test_a_failed_step_blocks_delivery(tmp_path: Path) -> None:
    session = _Session(tmp_path)
    update = asyncio.run(
        run_validation_node(
            _state(
                [
                    _write("public/index.html"),
                    _browser(steps=[{"do": "click", "ok": False, "text": "导航", "actual": "无变化"}]),
                ]
            ),
            session,
        )
    )

    assert update["validation"]["status"] == "failed"
    assert "导航" in update["validation"]["output_tail"]


def test_non_ui_change_does_not_require_a_browser(tmp_path: Path) -> None:
    session = _Session(tmp_path)
    update = asyncio.run(
        run_validation_node(
            _state([_write("server.py")], objective="修复后端接口"), session
        )
    )

    assert update["validation"]["status"] == "skipped"
    assert "ui_check" not in update["validation"]
    assert all(kind != "ui.check_failed" for kind, _, _ in session.recorder.events)


def test_script_next_to_html_counts_as_ui(tmp_path: Path) -> None:
    (tmp_path / "public").mkdir()
    (tmp_path / "public" / "index.html").write_text("<html></html>", encoding="utf-8")
    session = _Session(tmp_path)
    update = asyncio.run(
        run_validation_node(
            _state([_write("public/app.js")], objective="修复导航"), session
        )
    )

    assert update["validation"]["status"] == "failed"
    assert update["validation"]["command"] == "ui_check"


def test_backend_script_without_html_or_ui_words_is_not_gated(tmp_path: Path) -> None:
    session = _Session(tmp_path)
    update = asyncio.run(
        run_validation_node(
            _state([_write("scripts/build.js")], objective="修复构建脚本"), session
        )
    )

    assert update["validation"]["status"] == "skipped"
    assert "ui_check" not in update["validation"]


def test_ui_repair_hint_names_the_real_actions() -> None:
    hint = repair_hint(
        {"kind": "ui_unverified", "detail": "public/index.html 没有检查回执"}, 1
    )
    assert "browser_check" in hint
    assert "steps" in hint
    assert "reload" in hint
    assert "控制台" in hint
    # 不要把模型引去改测试或继续堆代码。
    assert "重跑测试" not in hint
