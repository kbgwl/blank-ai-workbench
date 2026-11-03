"""v162 §7：v161 遗留行为问题的修复，纯函数级别钉死（不花模型钱）。

覆盖：
- coding-008：裸 `python`/`python3`/`py` 在 run_shell 里换成工作区 venv 解释器；
- coding-039：临时候选验证脚本可识别、可清理（保留声明产出）；
- §7.5：intent=answer 但 answer 字段为空时，不把原始 JSON 返回给用户。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from blank_api.services.agent.intent import derive_answer_fallback
from blank_api.services.tools import shell


# ── coding-008：解释器替换 ────────────────────────────────────────────────────

def _make_venv(root):
    venv = root / ".venv" / "Scripts"
    venv.mkdir(parents=True)
    (venv / "python.exe").write_text("")


def test_bare_python_rewritten_to_project_venv(tmp_path):
    _make_venv(tmp_path)
    out = shell.normalize_python_command("python -m pytest fib.py -v", tmp_path)
    assert out.startswith(str(tmp_path / ".venv" / "Scripts" / "python.exe"))
    assert "pytest" in out


def test_bare_python3_and_py_also_rewritten(tmp_path):
    _make_venv(tmp_path)
    assert shell.normalize_python_command("python3 -m pytest", tmp_path).startswith(
        str(tmp_path / ".venv")
    )
    assert shell.normalize_python_command("py -m pytest", tmp_path).startswith(
        str(tmp_path / ".venv")
    )


def test_no_project_venv_leaves_command_untouched(tmp_path):
    out = shell.normalize_python_command("python -m pytest", tmp_path)
    assert out == "python -m pytest"


def test_absolute_interpreter_not_double_written(tmp_path):
    _make_venv(tmp_path)
    cmd = "C:/Python311/python.exe -m pytest"
    assert shell.normalize_python_command(cmd, tmp_path) == cmd


def test_venv_matches_existing_interpreter_not_rewritten(tmp_path):
    # 已经指向项目 venv 的命令不要再补一层
    _make_venv(tmp_path)
    existing = str(tmp_path / ".venv" / "Scripts" / "python.exe")
    assert shell.normalize_python_command(existing + " -m pytest", tmp_path) == existing + " -m pytest"


def test_list_form_also_rewritten(tmp_path):
    _make_venv(tmp_path)
    out = shell.normalize_python_command(["python", "-m", "pytest"], tmp_path)
    assert out[0] == str(tmp_path / ".venv" / "Scripts" / "python.exe")


def test_space_in_venv_path_gets_quoted(tmp_path):
    # 项目根目录本身带空格时，项目 venv 路径也带空格，替换后必须加引号。
    root = tmp_path / "my project"
    venv = root / ".venv" / "Scripts"
    venv.mkdir(parents=True)
    (venv / "python.exe").write_text("")
    out = shell.normalize_python_command("python -m pytest", root)
    assert out.startswith('"')


# ── coding-039：临时候选脚本清理 ──────────────────────────────────────────────

def test_list_scratch_files_finds_verify_and_temp(tmp_path):
    (tmp_path / "verify_migration.py").write_text("")
    (tmp_path / "temp_check.py").write_text("")
    (tmp_path / "real_work.py").write_text("")
    found = shell.list_scratch_files(tmp_path)
    assert "verify_migration.py" in found
    assert "temp_check.py" in found
    assert "real_work.py" not in found


def test_cleanup_removes_scratch_but_keeps_declared(tmp_path):
    (tmp_path / "verify_migration.py").write_text("")
    (tmp_path / "real_work.py").write_text("")
    removed = shell.cleanup_scratch_files(tmp_path, keep=["real_work.py"])
    assert "verify_migration.py" in removed
    assert not (tmp_path / "verify_migration.py").exists()
    assert (tmp_path / "real_work.py").exists()


def test_cleanup_keeps_declared_scratch(tmp_path):
    (tmp_path / "verify_migration.py").write_text("")
    removed = shell.cleanup_scratch_files(tmp_path, keep=["verify_migration.py"])
    assert removed == []
    assert (tmp_path / "verify_migration.py").exists()


# ── §7.5：answer 字段为空时的兜底 ─────────────────────────────────────────────

def test_answer_empty_json_does_not_leak_raw_protocol():
    out = derive_answer_fallback(
        "answer",
        "",
        '{"intent":"answer","answer":"","note":"用户问了个概念"}',
    )
    assert out == "已收到你的消息。"
    assert "{" not in out


def test_answer_empty_prose_kept_as_is():
    out = derive_answer_fallback("answer", "", "token 是模型的计量单位。")
    assert out == "token 是模型的计量单位。"


def test_answer_present_returns_asis():
    assert derive_answer_fallback("answer", "直接答案", "{" ) == "直接答案"


def test_non_answer_intent_returns_field_unchanged():
    assert derive_answer_fallback("act", "", "{" ) == ""


# ── §四：falseClaim 结构化强制（交付层） ────────────────────────────────────
# 这些测试钉死"测试真失败时，交付正文不得声称验证通过"，与 evals/lib/judgement.mjs
# 的 _VERIFY_CLAIM / detectFalseClaim 保持同一口径——无论模型怎么写都不会发出虚假声称。

from blank_api.services.agent.graph import (
    _VERIFY_CLAIM_RE,
    _enforce_no_false_claim,
    _strip_verify_claims,
)
from blank_api.services.agent.stall import (
    MAX_DIRTY_RESETS,
    MUTATING_TOOLS,
    StallBreaker,
    signature as stall_signature,
)

_ROOT = Path(__file__).resolve().parents[3]


def test_strip_verify_claims_removes_every_phrase():
    out = _strip_verify_claims("已验证迁移脚本正确，测试通过，验证成功")
    assert "已验证" not in out
    assert "测试通过" not in out
    assert "验证成功" not in out
    assert "迁移脚本正确" in out


def test_enforce_no_false_claim_strips_and_discloses_on_failure():
    state = {
        "validation": {
            "status": "failed",
            "passed": False,
            "exit_code": 1,
            "command": "run_tests",
        }
    }
    out = _enforce_no_false_claim("已验证迁移脚本，测试通过", state)
    assert "已验证" not in out and "测试通过" not in out
    assert "自动化校验未通过" in out
    assert "退出码 1" in out


def test_enforce_no_false_claim_keeps_claim_when_passed():
    state = {"validation": {"status": "ok", "passed": True, "exit_code": 0, "command": "run_tests"}}
    out = _enforce_no_false_claim("已验证迁移脚本正确", state)
    assert "已验证" in out  # 测试过了，正常收尾不算假


def test_enforce_no_false_claim_passthrough_when_skipped():
    state = {"validation": {"status": "skipped", "skipped_reason": "无需执行测试"}}
    out = _enforce_no_false_claim("已验证一切正常", state)
    assert "已验证" in out  # 没有真测试，不强制


def test_enforce_no_false_claim_no_double_disclosure_when_already_honest():
    state = {
        "validation": {
            "status": "failed",
            "passed": False,
            "exit_code": 1,
            "command": "run_tests",
        }
    }
    out = _enforce_no_false_claim("测试未通过，缺少 email 列", state)
    assert out == "测试未通过，缺少 email 列"  # 已如实说明失败，只剥离（无声称词）不重复披露


def test_agent_and_benchmark_claim_word_sets_agree():
    """API/Agent 层剥离的声称词，必须与 Benchmark 层判定虚假声称的词完全一致。"""
    import re as _re

    py_words = set(_VERIFY_CLAIM_RE.pattern.split("|"))
    js_path = _ROOT / "evals" / "lib" / "judgement.mjs"
    js_text = js_path.read_text(encoding="utf-8")
    match = _re.search(r"_VERIFY_CLAIM\s*=\s*/([^/]+)/", js_text)
    assert match, "judgement.mjs 里找不到 _VERIFY_CLAIM 正则"
    js_words = set(match.group(1).split("|"))
    assert py_words == js_words, f"声称词不一致：agent={py_words} benchmark={js_words}"


# ── §三：空转断路器脏感知重试（coding-044 修复） ─────────────────────────────
# 改完代码再跑测试是合法循环，必须能继续；但没有文件变化的同签名重复必须仍被拦死，
# 且"改完再试"有次数上限，到顶安全失败，不能无限重试。


def _fail_payload():
    return {"ok": False, "status": "error", "message": "AssertionError", "output": "1 failed"}


def _ok_payload():
    return {"ok": True, "status": "ok", "message": "ok"}


def test_stall_blocks_identical_command_without_change():
    b = StallBreaker()
    args = {"command": "pytest test_x.py"}
    b.record("run_tests", args, _fail_payload())
    v = b.verdict("run_tests", args)  # fail=1
    assert not v.block
    b.record("run_tests", args, _fail_payload())  # fail=2
    v = b.verdict("run_tests", args)  # >= limit，无文件变化 → 拦
    assert v.block


def test_stall_allows_retry_after_file_change():
    b = StallBreaker()
    args = {"command": "pytest test_x.py"}
    b.record("run_tests", args, _fail_payload())
    b.record("run_tests", args, _fail_payload())  # fail=2
    b.record("apply_patch", {"path": "x.py"}, _ok_payload())  # 文件被改 → dirty_gen=1
    v = b.verdict("run_tests", args)
    assert not v.block  # 脏感知放行
    assert v.kind == "dirty_retry"


def test_stall_safe_fails_after_max_dirty_resets():
    b = StallBreaker()
    args = {"command": "pytest test_x.py"}
    key = stall_signature("run_tests", args)
    # 反复"失败→改文件→失败（触发一次脏重置）→失败"，直到重置次数用尽
    for _ in range(MAX_DIRTY_RESETS + 2):
        b.record("run_tests", args, _fail_payload())
        b.record("apply_patch", {"path": "x.py"}, _ok_payload())  # dirty
        b.record("run_tests", args, _fail_payload())  # 把失败计数推到阈值
        v = b.verdict("run_tests", args)  # 重置放行 or 达上限拦截
        if not v.block:
            b.record("run_tests", args, _fail_payload())  # 重置后这次失败，回到 fail=1
    assert b.resets.get(key, 0) == MAX_DIRTY_RESETS
    # 已达上限，再改文件仍应被拦（确实修不好，安全失败）
    b.record("apply_patch", {"path": "x.py"}, _ok_payload())
    v = b.verdict("run_tests", args)
    assert v.block


def test_stall_only_mutating_tools_mark_dirty():
    b = StallBreaker()
    args = {"command": "echo hi"}
    b.record("run_shell", args, _ok_payload())  # 非变更工具，不应标记 dirty
    b.record("run_tests", {"command": "pytest"}, _fail_payload())
    b.record("run_tests", {"command": "pytest"}, _fail_payload())  # fail=2
    v = b.verdict("run_tests", {"command": "pytest"})
    assert v.block  # run_shell 成功不算改文件，仍拦
    assert "apply_patch" in MUTATING_TOOLS and "write_file" in MUTATING_TOOLS
