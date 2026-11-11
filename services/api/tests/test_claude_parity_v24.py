"""Claude Code 编程语义移植的判据（N 批）。

四件事各钉一组：

1. **读后写硬闸门**：没读过不许改，读过之后文件被改过也不许改。以前这条只是
   系统提示里的一句话，模型跳过时没有任何东西会失败。
2. **edit_file 的精确串语义**：唯一匹配、``replace_all``、no-op 失败、找不到时带回
   邻近内容、CRLF 归一后按原风格写回、写前备份。
3. **write_file 的整文件语义**：新建免读，覆盖必读，内容相同不算一次变更。
4. **shell 硬拦截**：git 完整性与 shell 写文件被直接拒绝；引号里的字样、``2>&1``、
   ``2>nul``、``>/dev/null`` 不许误杀。

另加两条结构性判据：新工具必须同时在注册表与各工具名集合里；``parallel_read_tools``
必须默认开启（并行是 Claude 的默认行为，不是实验能力）。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from blank_api.services import features as registry
from blank_api.services.agent import graph
from blank_api.services.safety import SafetyPolicy
from blank_api.services.tools import editing
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.editing import (
    _edit_file,
    _edit_on_disk,
    _write_file as _write_file_tool,
    _write_on_disk,
)
from blank_api.services.tools.file_state import FileStateCache, check_write_guard
from blank_api.services.tools.filesystem import _read_file


def _write(root: Path, relative: str, text: str, *, newline: str = "\n") -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(text.replace("\n", newline).encode("utf-8"))
    return target


def _read(cache: FileStateCache, root: Path, relative: str) -> None:
    """模拟一次真正的 read_file：size/mtime/sha 都从磁盘现取。"""
    raw = (root / relative).read_bytes()
    stat = (root / relative).stat()
    cache.record_read(
        relative,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        sha256=hashlib.sha256(raw).hexdigest() if stat.st_size <= 512 * 1024 else "",
    )


def test_read_state_survives_export_restore(tmp_path: Path) -> None:
    """审批/提问会结束当前运行、恢复时是新会话——读状态必须跟着检查点走。

    不补这一格，用户答完一句"就用这个"再写，模型会被要求把刚读过的文件重读一遍
    （实测形状见 test_unknown_facts_e2e_v147 的读→提问→回答→写）。
    """
    _write(tmp_path, "main.py", "print('hi')\n")
    first = FileStateCache()
    _read(first, tmp_path, "main.py")
    saved = first.export()

    second = FileStateCache()
    second.restore(saved)

    assert second.guard("main.py")["was_read"] is True
    assert second.guard("main.py")["sha256"] == first.guard("main.py")["sha256"]
    # 空/坏数据不当成读过：缺键就是没读过。
    broken = FileStateCache()
    broken.restore(None)
    assert broken.guard("main.py")["was_read"] is False


def _edit(root: Path, relative: str, old: str, new: str, *, guard=None, replace_all=False):
    return _edit_on_disk(
        root,
        "task-1",
        {"path": relative, "old_string": old, "new_string": new, "replace_all": replace_all},
        guard=guard,
    )


def _write_file(root: Path, relative: str, content: str, *, guard=None):
    return _write_on_disk(
        root, "task-1", {"path": relative, "content": content}, guard=guard
    )


# --------------------------------------------------------------------------- #
# 读后写硬闸门
# --------------------------------------------------------------------------- #


def test_guard_starts_out_not_read():
    guard = FileStateCache().guard("a.py")

    assert guard["was_read"] is False
    assert guard["size"] is None


def test_guard_is_fresh_right_after_reading(tmp_path):
    cache = FileStateCache()
    _write(tmp_path, "a.py", "x = 1\n")
    _read(cache, tmp_path, "a.py")

    assert cache.guard("a.py")["was_read"] is True
    assert check_write_guard(tmp_path / "a.py", cache.guard("a.py")) is None


def test_guard_turns_stale_after_the_file_changes_on_disk(tmp_path):
    cache = FileStateCache()
    _write(tmp_path, "a.py", "x = 1\n")
    _read(cache, tmp_path, "a.py")
    _write(tmp_path, "a.py", "x = 2 and much longer\n")

    error = check_write_guard(tmp_path / "a.py", cache.guard("a.py"))

    assert error is not None and error["code"] == "stale"
    # 事实要能对账：读到时多大、磁盘上现在多大
    assert error["read"]["size"] != error["disk"]["size"]


def test_sha256_closes_the_gap_when_stat_looks_unchanged(tmp_path):
    """预检之后、写入之前被换掉：size 与 mtime 都一致，只有内容变了。"""
    target = _write(tmp_path, "a.py", "x = 1\n")
    stat = target.stat()
    guard = {
        "path": "a.py",
        "was_read": True,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": hashlib.sha256(b"x = 0\n").hexdigest(),  # 不是磁盘上的这一份
    }

    error = check_write_guard(target, guard, target.read_bytes())

    assert error is not None and error["code"] == "stale"


def test_recording_a_write_makes_the_next_edit_need_no_reread(tmp_path):
    cache = FileStateCache()
    _write(tmp_path, "a.py", "x = 1\n")
    _read(cache, tmp_path, "a.py")
    assert _edit(tmp_path, "a.py", "x = 1", "x = 2", guard=cache.guard("a.py"))["path"] == "a.py"
    cache.record_write(tmp_path, "a.py")

    assert check_write_guard(tmp_path / "a.py", cache.guard("a.py")) is None


def test_forget_puts_the_guard_back_to_not_read(tmp_path):
    cache = FileStateCache()
    _write(tmp_path, "a.py", "x = 1\n")
    _read(cache, tmp_path, "a.py")
    cache.forget("a.py")

    assert cache.guard("a.py")["was_read"] is False


def test_guard_keys_normalise_separators_and_dot_slash(tmp_path):
    cache = FileStateCache()
    _write(tmp_path, "src/a.py", "x = 1\n")
    _read(cache, tmp_path, "src/a.py")

    assert cache.guard(".\\src\\a.py")["was_read"] is True


def test_no_cache_injected_means_no_gate(tmp_path):
    """单测/旧集成直接构造 ToolContext 时 file_state 为空：行为必须与以前一致。"""
    target = _write(tmp_path, "a.py", "x = 1\n")

    assert check_write_guard(target, None) is None


def test_new_file_needs_no_prior_read(tmp_path):
    guard = FileStateCache().guard("brand-new.py")

    assert check_write_guard(tmp_path / "brand-new.py", guard) is None


# --------------------------------------------------------------------------- #
# edit_file：精确串语义
# --------------------------------------------------------------------------- #


def test_edit_refuses_an_existing_file_that_was_never_read(tmp_path):
    _write(tmp_path, "a.py", "x = 1\n")

    result = _edit(tmp_path, "a.py", "x = 1", "x = 2", guard=FileStateCache().guard("a.py"))

    assert result["guard_error"]["code"] == "not_read"
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 1\n"


def test_edit_replaces_the_exact_unique_match(tmp_path):
    cache = FileStateCache()
    _write(tmp_path, "a.py", "a = 1\nb = 2\n")
    _read(cache, tmp_path, "a.py")

    result = _edit(tmp_path, "a.py", "b = 2", "b = 3", guard=cache.guard("a.py"))

    assert result["replacements"] == 1
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "a = 1\nb = 3\n"


def test_edit_refuses_an_ambiguous_old_string(tmp_path):
    cache = FileStateCache()
    _write(tmp_path, "a.py", "same\nother\nsame\n")
    _read(cache, tmp_path, "a.py")

    result = _edit(tmp_path, "a.py", "same", "different", guard=cache.guard("a.py"))

    assert result["ambiguous"] is True
    assert result["occurrences"] == 2
    assert result["lines"] == [1, 3]
    # 拒绝就是没改
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "same\nother\nsame\n"


def test_replace_all_takes_every_occurrence(tmp_path):
    cache = FileStateCache()
    _write(tmp_path, "a.py", "same\nother\nsame\n")
    _read(cache, tmp_path, "a.py")

    result = _edit(
        tmp_path, "a.py", "same", "different", guard=cache.guard("a.py"), replace_all=True
    )

    assert result["replacements"] == 2
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "different\nother\ndifferent\n"


def test_not_found_comes_back_with_nearby_context(tmp_path):
    cache = FileStateCache()
    _write(tmp_path, "a.py", "def greet():\n    return 'hi'\n")
    _read(cache, tmp_path, "a.py")

    result = _edit(tmp_path, "a.py", "def greet():\n    return 'hello'", "x", guard=cache.guard("a.py"))

    assert result["not_found"] is True
    assert "greet" in result["near"]


def test_identical_old_and_new_is_a_no_op_and_fails(tmp_path):
    _write(tmp_path, "a.py", "x = 1\n")

    result = _edit(tmp_path, "a.py", "x = 1", "x = 1")

    assert result["no_op"] is True


def test_edit_never_creates_a_file(tmp_path):
    cache = FileStateCache()

    result = _edit(tmp_path, "missing.py", "a", "b", guard=cache.guard("missing.py"))

    assert result["file_missing"] is True
    assert not (tmp_path / "missing.py").exists()


def test_crlf_is_matched_as_lf_and_written_back_as_crlf(tmp_path):
    cache = FileStateCache()
    _write(tmp_path, "a.py", "a = 1\nb = 2\n", newline="\r\n")
    _read(cache, tmp_path, "a.py")

    result = _edit(tmp_path, "a.py", "b = 2\n", "b = 3\n", guard=cache.guard("a.py"))

    raw = (tmp_path / "a.py").read_bytes()
    assert result["path"] == "a.py"
    assert raw == b"a = 1\r\nb = 3\r\n"


def test_edit_writes_a_backup_before_touching_the_file(tmp_path):
    cache = FileStateCache()
    _write(tmp_path, "a.py", "x = 1\n")
    _read(cache, tmp_path, "a.py")

    result = _edit(tmp_path, "a.py", "x = 1", "x = 2", guard=cache.guard("a.py"))

    backup = tmp_path / result["backup_path"]
    assert backup.is_file()
    assert backup.read_text(encoding="utf-8") == "x = 1\n"


def test_edit_refuses_stale_content_and_leaves_the_file_alone(tmp_path):
    cache = FileStateCache()
    _write(tmp_path, "a.py", "x = 1\n")
    _read(cache, tmp_path, "a.py")
    # 用户（或格式化工具）在读过之后改了它
    _write(tmp_path, "a.py", "x = 99\n")

    result = _edit(tmp_path, "a.py", "x = 1", "x = 2", guard=cache.guard("a.py"))

    assert result["guard_error"]["code"] == "stale"
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 99\n"


# --------------------------------------------------------------------------- #
# write_file：整文件语义
# --------------------------------------------------------------------------- #


def test_write_creates_a_new_file_without_reading(tmp_path):
    guard = FileStateCache().guard("new/dir/a.py")

    result = _write_file(tmp_path, "new/dir/a.py", "print('hi')\n", guard=guard)

    assert result["existed"] is False
    assert (tmp_path / "new/dir/a.py").read_text(encoding="utf-8") == "print('hi')\n"


def test_write_refuses_to_overwrite_a_file_that_was_never_read(tmp_path):
    _write(tmp_path, "a.py", "old\n")

    result = _write_file(tmp_path, "a.py", "new\n", guard=FileStateCache().guard("a.py"))

    assert result["guard_error"]["code"] == "not_read"
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "old\n"


def test_write_with_identical_content_reports_unchanged(tmp_path):
    cache = FileStateCache()
    _write(tmp_path, "a.py", "same\n")
    _read(cache, tmp_path, "a.py")

    result = _write_file(tmp_path, "a.py", "same\n", guard=cache.guard("a.py"))

    assert result.get("unchanged") is True
    assert "backup_path" not in result


def test_write_overwrites_after_reading_and_keeps_a_backup(tmp_path):
    cache = FileStateCache()
    _write(tmp_path, "a.py", "old\n")
    _read(cache, tmp_path, "a.py")

    result = _write_file(tmp_path, "a.py", "new\n", guard=cache.guard("a.py"))

    assert result["existed"] is True
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "new\n"
    assert (tmp_path / result["backup_path"]).read_text(encoding="utf-8") == "old\n"


def test_write_refuses_stale_content(tmp_path):
    cache = FileStateCache()
    _write(tmp_path, "a.py", "old\n")
    _read(cache, tmp_path, "a.py")
    _write(tmp_path, "a.py", "changed by someone else\n")

    result = _write_file(tmp_path, "a.py", "new\n", guard=cache.guard("a.py"))

    assert result["guard_error"]["code"] == "stale"
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "changed by someone else\n"


# --------------------------------------------------------------------------- #
# apply_patch 也过同一道闸门
# --------------------------------------------------------------------------- #


def _patch(rel: str, old: str, new: str) -> str:
    return f"--- a/{rel}\n+++ b/{rel}\n@@ -1,1 +1,1 @@\n-{old}\n+{new}\n"


def test_apply_patch_refuses_an_unread_existing_file(tmp_path):
    from blank_api.services.tools.patch import _patch_on_disk

    _write(tmp_path, "a.py", "x = 1\n")

    result = _patch_on_disk(
        tmp_path,
        "task-1",
        {"path": "a.py", "patch": _patch("a.py", "x = 1", "x = 2")},
        guard=FileStateCache().guard("a.py"),
    )

    assert result["guard_error"]["code"] == "not_read"
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 1\n"


def test_apply_patch_goes_through_after_reading(tmp_path):
    from blank_api.services.tools.patch import _patch_on_disk

    cache = FileStateCache()
    _write(tmp_path, "a.py", "x = 1\n")
    _read(cache, tmp_path, "a.py")

    result = _patch_on_disk(
        tmp_path,
        "task-1",
        {"path": "a.py", "patch": _patch("a.py", "x = 1", "x = 2")},
        guard=cache.guard("a.py"),
    )

    assert result["path"] == "a.py"
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 2\n"


# --------------------------------------------------------------------------- #
# 工具处理器：read 记状态、edit 用状态、写完重记
# --------------------------------------------------------------------------- #


async def test_read_then_edit_then_edit_again_without_rereading(tmp_path, monkeypatch):
    monkeypatch.setenv("BLANK_FEATURE_FS_BROKER", "0")

    async def fake_record_change(context, relative, change_type, diff, backup_rel):
        return "change-1"

    monkeypatch.setattr(editing, "_record_change", fake_record_change)
    cache = FileStateCache()
    context = ToolContext(root=tmp_path, task_id="task-1", session_factory=None, file_state=cache)
    _write(tmp_path, "a.py", "a = 1\n")

    assert (await _read_file(context, {"path": "a.py"})).ok
    assert cache.guard("a.py")["was_read"] is True

    first = await _edit_file(context, {"path": "a.py", "old_string": "a = 1", "new_string": "a = 2"})
    # 成功写后状态被重记：第二次编辑不用再读一遍
    second = await _edit_file(context, {"path": "a.py", "old_string": "a = 2", "new_string": "a = 3"})

    assert first.ok and second.ok
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "a = 3\n"


async def test_write_handler_rerecords_so_the_next_write_needs_no_reread(tmp_path, monkeypatch):
    monkeypatch.setenv("BLANK_FEATURE_FS_BROKER", "0")

    async def fake_record_change(context, relative, change_type, diff, backup_rel):
        return "change-1"

    monkeypatch.setattr(editing, "_record_change", fake_record_change)
    cache = FileStateCache()
    context = ToolContext(root=tmp_path, task_id="task-1", session_factory=None, file_state=cache)
    _write(tmp_path, "a.py", "v1\n")

    assert (await _read_file(context, {"path": "a.py"})).ok
    first = await _write_file_tool(context, {"path": "a.py", "content": "v2-long\n"})
    # 内容长度变了：没重记的话第二次会被判成 stale
    second = await _write_file_tool(context, {"path": "a.py", "content": "v3-longer\n"})

    assert first.ok and second.ok
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "v3-longer\n"


async def test_edit_handler_reports_stale_as_a_failed_outcome(tmp_path, monkeypatch):
    monkeypatch.setenv("BLANK_FEATURE_FS_BROKER", "0")
    cache = FileStateCache()
    context = ToolContext(root=tmp_path, task_id="task-1", session_factory=None, file_state=cache)
    _write(tmp_path, "a.py", "a = 1\n")
    await _read_file(context, {"path": "a.py"})
    _write(tmp_path, "a.py", "a = 1\n# changed\n")

    outcome = await _edit_file(context, {"path": "a.py", "old_string": "a = 1", "new_string": "a = 2"})

    assert outcome.ok is False
    assert outcome.data["file_state_error"]["code"] == "stale"
    assert "重新 read_file" in outcome.message


# --------------------------------------------------------------------------- #
# 注册表与各工具名集合
# --------------------------------------------------------------------------- #


def test_edit_tools_are_registered_everywhere_they_matter():
    from blank_api.services.permissions import ARGUMENT_SCOPED_TOOLS, EDIT_TOOLS
    from blank_api.services.tools import build_default_tools
    from blank_api.services.tools.gateway import PATH_WRITE_TOOLS
    from blank_api.services.tools.registry import CORE_TOOLS, LEAN_TOOLS

    names = {tool.name for tool in build_default_tools()}

    assert {"edit_file", "write_file"} <= names
    assert {"edit_file", "write_file"} <= PATH_WRITE_TOOLS
    assert {"edit_file", "write_file"} <= EDIT_TOOLS
    assert "edit_file" in CORE_TOOLS and "edit_file" in LEAN_TOOLS
    # 参数级风险（高风险路径 / 明文密钥）要靠这两个工具自己算
    assert "edit_file" in ARGUMENT_SCOPED_TOOLS
    assert "write_file" in ARGUMENT_SCOPED_TOOLS


def test_parallel_read_tools_is_stable_and_on_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("BLANK_FEATURE_PARALLEL_READ_TOOLS", raising=False)
    monkeypatch.setenv("BLANK_SETTINGS_FILE", str(tmp_path / "settings.json"))

    flag = registry.FEATURES["parallel_read_tools"]

    assert flag.stage == registry.STABLE
    assert flag.default is True
    assert registry.resolve("parallel_read_tools", {}) == (True, registry.SRC_DEFAULT)


def test_system_prompt_carries_the_ported_rules():
    prompt = graph.SYSTEM_PROMPT

    for fragment in (
        "edit_file",
        "write_file",
        "apply_patch",
        "写之前先读",
        "同一批里一起发",
        "路径:行号",
        "不要原样重试",
    ):
        assert fragment in prompt, f"系统提示漏了移植规则：{fragment}"


# --------------------------------------------------------------------------- #
# shell 硬拦截
# --------------------------------------------------------------------------- #


def _policy() -> SafetyPolicy:
    return SafetyPolicy(shell_enabled=True, network_enabled=True, confirm_all_commands=False)


@pytest.mark.parametrize(
    "command",
    [
        "git commit --no-verify -m x",
        "git -c commit.gpgsign=false commit -m x",
        "git commit --no-gpg-sign -m x",
        "git rebase -i HEAD~2",
        "git rebase --interactive main",
        "git add -i",
        "git add --interactive",
    ],
)
def test_git_integrity_flags_are_denied(command):
    verdict = _policy().gate("run_shell", {"command": command}, Path.cwd())

    assert verdict.denied
    assert verdict.rule == "builtin:destructive"


@pytest.mark.parametrize(
    "command",
    [
        "echo hi > out.txt",
        "echo hi >> out.txt",
        "python app.py > log.txt",
        "Set-Content -Path a.txt -Value x",
        "Add-Content -Path a.txt -Value x",
        "Out-File -FilePath a.txt",
        "Get-Content a.txt | tee b.txt",
    ],
)
def test_shell_file_writes_are_denied(command):
    verdict = _policy().gate("run_shell", {"command": command}, Path.cwd())

    assert verdict.denied
    assert verdict.rule == "builtin:shell_file_write"


@pytest.mark.parametrize(
    "command",
    [
        'echo hi > "out.txt"',
        "python app.py > 'log.txt'",
        'Set-Content -Path "a.txt" -Value x',
    ],
)
def test_quoted_redirect_targets_are_still_writes(command):
    """引号只影响"操作符在不在引号里"，不影响"目标是不是文件"。"""
    verdict = _policy().gate("run_shell", {"command": command}, Path.cwd())

    assert verdict.denied, f"漏判：{command} -> {verdict.as_dict()}"
    assert verdict.rule == "builtin:shell_file_write"


def test_quoted_absolute_path_outside_workspace_is_an_escape(tmp_path):
    """抹引号内容只该用于匹配命令名，不能连带把路径检查也抹掉。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("x", encoding="utf-8")

    verdict = _policy().gate("run_shell", {"command": f'type "{outside}"'}, workspace)

    assert verdict.denied, f"引号路径逃逸漏判：{verdict.as_dict()}"
    assert verdict.rule == "capability:path_escape"


@pytest.mark.parametrize(
    "command",
    [
        'git commit -m "fix --no-verify doc"',
        'git commit -m "explain commit.gpgsign=false"',
        'echo "a > b"',
        "npm test 2>&1",
        "npm test 2>nul",
        "npm test > /dev/null",
        "npm test",
        "git status",
        "git commit -m x",
    ],
)
def test_flags_in_quotes_and_device_redirects_are_not_killed(command):
    verdict = _policy().gate("run_shell", {"command": command}, Path.cwd())

    assert verdict.allowed, f"被误杀：{command} -> {verdict.as_dict()}"
