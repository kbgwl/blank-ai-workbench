"""run_tests 的测试命令识别：Go / pytest / npm / 空目录的优先级与返回值。

这些断言不真正执行 go/pytest，只验证 detect_test_command 的判定——所以不需要 Go 装在 CI 上。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from blank_api.services.tools.testing import detect_test_command


def test_detect_go_mod_returns_go_test(tmp_path: Path):
    root = tmp_path / "goproj"
    root.mkdir()
    (root / "go.mod").write_text("module example.com/strutil\n\ngo 1.23\n", encoding="utf-8")
    argv, shell = detect_test_command(root)
    assert argv[:3] == ["go", "test", "./..."]
    assert "-count=1" in argv
    assert shell is False  # go 直接执行，不走 shell，也不会被 docker 的 python 替换误伤


def test_detect_prefers_package_json_test_script_over_go(tmp_path: Path):
    root = tmp_path / "mix"
    root.mkdir()
    (root / "go.mod").write_text("module example\n\ngo 1.23\n", encoding="utf-8")
    (root / "package.json").write_text('{"scripts":{"test":"npm test"}}', encoding="utf-8")
    argv, shell = detect_test_command(root)
    assert argv == ["npm", "test"]
    assert shell is True


def test_detect_pytest_marker_before_go_glob(tmp_path: Path):
    root = tmp_path / "py"
    root.mkdir()
    (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    argv, _ = detect_test_command(root)
    assert argv and "pytest" in " ".join(argv)


def test_detect_empty_workspace_returns_none(tmp_path: Path):
    root = tmp_path / "empty"
    root.mkdir()
    assert detect_test_command(root) is None


def test_detect_npm_test_script(tmp_path: Path):
    root = tmp_path / "js"
    root.mkdir()
    (root / "package.json").write_text('{"scripts":{"test":"jest"}}', encoding="utf-8")
    argv, shell = detect_test_command(root)
    assert argv == ["npm", "test"]
    assert shell is True
