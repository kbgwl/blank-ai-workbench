"""CLI 的装配层：样例解析、领域推断与两个只读子命令。"""

import json
from argparse import Namespace
from pathlib import Path

import pytest

from blank_api.services.harness.cli import (
    _cmd_library,
    _cmd_validate,
    _domain_of,
    _load_samples,
    build_parser,
)
from blank_api.services.harness.library import Library


def _samples_file(tmp_path: Path) -> Path:
    path = tmp_path / "samples.json"
    path.write_text(
        json.dumps(
            {
                "fixtures": {"py": {"a.py": "print(1)\n"}},
                "samples": [
                    {"id": "T01", "category": "新增功能", "fixture": "py", "title": "a"},
                    {"id": "T02", "category": "缺陷修复", "fixture": "py", "title": "b"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def test_samples_resolve_fixtures_and_filter(tmp_path: Path) -> None:
    samples, _ = _load_samples(_samples_file(tmp_path), only="T02", category="")
    assert [item["id"] for item in samples] == ["T02"]
    assert samples[0]["files"] == {"a.py": "print(1)\n"}


def test_unknown_fixture_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps({"fixtures": {}, "samples": [{"id": "T01", "fixture": "missing"}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="fixture"):
        _load_samples(path, only="", category="")


def test_domain_defaults_to_the_sample_categories() -> None:
    assert _domain_of([{"category": "新增功能"}, {"category": "新增功能"}], "") == "新增功能"
    assert _domain_of([{"category": "a"}, {"category": "b"}], "") == "a、b"
    assert _domain_of([], "指定") == "指定"
    assert _domain_of([], "") == "未标注"


async def test_validate_command_reports_success_and_failure(tmp_path: Path, capsys) -> None:
    harness = tmp_path / ".blank" / "harness"
    harness.mkdir(parents=True)
    (harness / "systemprompt.md").write_text("先读再改。\n", encoding="utf-8")

    ok = await _cmd_validate(Namespace(path="", workspace=str(tmp_path), strict=True, json=False))
    assert ok == 0
    assert "校验通过" in capsys.readouterr().out

    (harness / "systemprompt.md").write_text("照 evals/workspaces 抄\n", encoding="utf-8")
    bad = await _cmd_validate(Namespace(path="", workspace=str(tmp_path), strict=True, json=False))
    assert bad == 2
    assert "评测材料标记" in capsys.readouterr().err


async def test_validate_command_accepts_an_empty_harness(tmp_path: Path, capsys) -> None:
    code = await _cmd_validate(
        Namespace(path=str(tmp_path / "none"), workspace=".", strict=False, json=False)
    )
    assert code == 0
    assert "没有 harness 组件" in capsys.readouterr().out


def test_run_parser_exposes_reflection_and_regression_gates() -> None:
    args = build_parser().parse_args(
        [
            "run",
            "--regression-only",
            "T02",
            "--max-regression-drop",
            "2.5",
            "--max-token-growth",
            "0.5",
            "--reflect",
            "--reflect-rounds",
            "2",
        ]
    )
    assert args.regression_only == "T02"
    assert args.max_regression_drop == 2.5
    assert args.max_token_growth == 0.5
    assert args.reflect is True
    assert args.reflect_rounds == 2


async def test_library_command_prints_lessons(tmp_path: Path, capsys) -> None:
    library = Library(tmp_path, "解析器")
    library.add(
        "T01",
        {"systemprompt.md": "先读再改。"},
        experience={"cue": "c", "lesson": "先读测试", "evidence": "e"},
    )
    code = await _cmd_library(Namespace(root=str(tmp_path), domain="解析器", limit=2_000, json=False))
    assert code == 0
    out = capsys.readouterr().out
    assert "先读测试" in out
    assert "harnesses/T01/" in out
