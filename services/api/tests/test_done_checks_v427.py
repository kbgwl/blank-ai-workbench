"""完成判据从"一句话"变成"逐条核对"。

原来的状态：`set_goal` 收 `done_when: string[]`，而 verifier 只用它判"有没有交付文本"
（`verifier.py` 的 `goal_met`）——写了五条验收，一条也没被核对过。

现在每条判据可以带一个 `check`，由 verifier 只读事实去核：
  path_exists / file_contains / command

三条纪律：
1. **没有可执行判据 ≠ 失败**（记 unverifiable）——"页面好看"这种没法用事实回答。
2. **没跑成 ≠ 没过**（记 unknown）——命令没跑、文件读不动，都是没拿到结论。
3. **第一版不设 blocking**——判据是模型自己写的，写歪了不该把好交付判死。
"""

from __future__ import annotations

import pathlib

from blank_api.services.agent.verifier import verify
from blank_api.services.plan import CHECK_TYPES, normalize_goal


def _workspace(tmp_path: pathlib.Path) -> pathlib.Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "login.py").write_text("def login():\n    raise Error('密码错误')\n", encoding="utf-8")
    return tmp_path


def _goal(pairs: list[tuple[str, dict]]) -> dict:
    return normalize_goal(
        {"objective": "加登录", "done_when": [{"text": t, "check": c} for t, c in pairs]}
    )


def _verify(goal: dict, root: pathlib.Path, rows: list[dict] | None = None) -> dict:
    return verify(
        goal_spec=goal,
        tool_results=rows or [{"tool": "write_file", "ok": True, "status": "ok", "data": {"path": "src/login.py", "diff": "+x"}}],
        changed_paths=["src/login.py"],
        report="已完成。",
        root=root,
    )


def test_a_missing_file_fails_its_criterion(tmp_path):
    root = _workspace(tmp_path)
    goal = _goal([("登录文件在", {"type": "path_exists", "path": "src/login.py"}),
                  ("配置也在", {"type": "path_exists", "path": "src/nope.py"})])

    result = _verify(goal, root)
    by_text = {item["text"]: item for item in result["evidence"]["done_when"]}

    assert by_text["登录文件在"]["status"] == "passed"
    assert by_text["配置也在"]["status"] == "failed"
    assert result["checks"]["done_when"] is False
    assert any("判据没过" in f for f in result["findings"])
    assert result["blocking"] is False, "判据是模型写的，第一版不该据此判死"


def test_file_contains_checks_the_actual_bytes(tmp_path):
    root = _workspace(tmp_path)
    goal = _goal([("错误提示在", {"type": "file_contains", "path": "src/login.py", "text": "密码错误"}),
                  ("有日志", {"type": "file_contains", "path": "src/login.py", "text": "logger.info"})])

    result = _verify(goal, root)
    by_text = {item["text"]: item for item in result["evidence"]["done_when"]}

    assert by_text["错误提示在"]["status"] == "passed"
    assert by_text["有日志"]["status"] == "failed"


def test_a_command_counts_only_if_it_really_ran_and_passed(tmp_path):
    root = _workspace(tmp_path)
    goal = _goal([("单测过", {"type": "command", "cmd": "pytest -q tests/test_login.py"})])

    passed = _verify(goal, root, rows=[
        {"tool": "run_tests", "ok": True, "status": "ok", "data": {"command": "pytest -q tests/test_login.py", "exit_code": 0}},
    ])
    assert passed["evidence"]["done_when"][0]["status"] == "passed"

    failed = _verify(goal, root, rows=[
        {"tool": "run_tests", "ok": False, "status": "failed", "data": {"command": "pytest -q tests/test_login.py", "exit_code": 1}},
    ])
    assert failed["evidence"]["done_when"][0]["status"] == "failed"

    never = _verify(goal, root, rows=[])
    assert never["evidence"]["done_when"][0]["status"] == "unknown", "没跑过不是没过"
    assert never["checks"].get("done_when") is None, "没跑成不该参与好坏判定"


def test_a_criterion_without_a_check_is_not_a_failure(tmp_path):
    root = _workspace(tmp_path)
    goal = normalize_goal({"objective": "加登录", "done_when": ["页面好看", "登录文件在"]})
    # 纯字符串判据：没有可执行的 check
    goal["done_checks"] = [{}, {"type": "path_exists", "path": "src/login.py"}]

    result = _verify(goal, root)
    statuses = [item["status"] for item in result["evidence"]["done_when"]]

    assert statuses == ["unverifiable", "passed"]
    assert all("判据没过" not in f for f in result["findings"])


def test_without_a_root_the_file_checks_say_unknown(tmp_path):
    goal = _goal([("登录文件在", {"type": "path_exists", "path": "src/login.py"})])

    result = verify(goal_spec=goal, tool_results=[{"tool": "write_file", "ok": True, "status": "ok", "data": {"path": "x", "diff": "+"}}],
                    changed_paths=["x"], report="完成", root=None)

    assert result["evidence"]["done_when"][0]["status"] == "unknown"


def test_unknown_check_types_are_dropped_but_the_text_survives():
    goal = normalize_goal({
        "objective": "x",
        "done_when": [{"text": "接口返回 200", "check": {"type": "http", "path": "/login"}}],
    })

    assert goal["done_when"] == ["接口返回 200"], "文本要留着——它是给人看的"
    assert goal["done_checks"] == [{}], "认不出的类型不执行"
    assert "http" not in CHECK_TYPES
