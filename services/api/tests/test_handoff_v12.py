"""P2：指令分层 / 交接笔记 / 长期记忆 / shell 快照。

这一批的共同点是"让长任务活过上下文窗口"：AGENTS.md 决定一开始就知道什么，
notes 决定换窗口时还记得什么，memory 决定下一个任务不用重新踩坑，
shell 快照决定多步命令跑在同一个环境里。四者都是确定性的，不额外花模型钱。
"""

from datetime import datetime, timezone
import os
from pathlib import Path

import pytest

from blank_api.services.instructions import (
    LAYER_PROJECT,
    LAYER_TASK,
    LAYER_USER,
    compose,
    load_layers,
    provenance,
)
from blank_api.services.memory import MAX_MEMORY_CHARS, MemoryStore
from blank_api.services.notes import MAX_NOTE_CHARS, NoteError, NoteStore
from blank_api.services.shell_env import (
    POLICY_INHERIT,
    POLICY_MINIMAL,
    POLICY_STRICT,
    capture,
    filter_env,
)


# --------------------------------------------------------------------------- #
# 1. 指令分层
# --------------------------------------------------------------------------- #
def test_project_agents_md_is_loaded(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLANK_USER_INSTRUCTIONS_DIR", str(tmp_path / "nouser"))
    monkeypatch.delenv("BLANK_INSTRUCTIONS_FILE", raising=False)
    (tmp_path / "AGENTS.md").write_text("用中文写注释\n", encoding="utf-8")
    (tmp_path / ".blank").mkdir()
    (tmp_path / ".blank" / "AGENTS.md").write_text("提交信息用 conventional commits\n", encoding="utf-8")

    layers = load_layers(tmp_path)
    assert [item.source for item in layers] == [LAYER_PROJECT, LAYER_PROJECT]
    assert "中文写注释" in layers[0].content
    assert "conventional commits" in layers[1].content


def test_user_layer_sits_below_project(tmp_path: Path, monkeypatch) -> None:
    user_dir = tmp_path / "user"
    user_dir.mkdir()
    (user_dir / "AGENTS.md").write_text("先给方案再动手\n", encoding="utf-8")
    monkeypatch.setenv("BLANK_USER_INSTRUCTIONS_DIR", str(user_dir))
    (tmp_path / "AGENTS.md").write_text("本项目必须跑 pytest\n", encoding="utf-8")

    layers = load_layers(tmp_path)
    assert [item.source for item in layers] == [LAYER_USER, LAYER_PROJECT]


def test_duplicate_content_is_collapsed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLANK_USER_INSTRUCTIONS_DIR", str(tmp_path / "nouser"))
    (tmp_path / "AGENTS.md").write_text("同样的内容\n", encoding="utf-8")
    (tmp_path / ".blank").mkdir()
    (tmp_path / ".blank" / "AGENTS.md").write_text("同样的内容\n", encoding="utf-8")
    assert len(load_layers(tmp_path)) == 1


def test_missing_files_are_not_an_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLANK_USER_INSTRUCTIONS_DIR", str(tmp_path / "nouser"))
    assert load_layers(tmp_path) == []


def test_oversized_file_is_clipped_with_a_marker(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLANK_USER_INSTRUCTIONS_DIR", str(tmp_path / "nouser"))
    (tmp_path / "AGENTS.md").write_text("啊" * 20_000, encoding="utf-8")
    layer = load_layers(tmp_path)[0]
    assert layer.truncated is True
    assert "已截断" in layer.content
    assert len(layer.content) < 20_000


def test_compose_keeps_system_first_and_states_precedence(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLANK_USER_INSTRUCTIONS_DIR", str(tmp_path / "nouser"))
    (tmp_path / "AGENTS.md").write_text("用 ruff 格式化\n", encoding="utf-8")
    layers = load_layers(tmp_path, extra=["这次只改 main.py"])

    prompt = compose("BASE RULES", layers)
    assert prompt.startswith("BASE RULES")
    # 强层（任务）排在弱层之后，且都带标题
    assert prompt.index("本项目约定") < prompt.index("本次任务的附加指令")
    assert "越靠近当前任务优先级越高" in prompt
    assert "不能放宽安全规则" in prompt
    assert [item["source"] for item in provenance(layers)] == [LAYER_PROJECT, LAYER_TASK]


def test_compose_without_layers_is_identity() -> None:
    assert compose("BASE", []) == "BASE"


def test_total_budget_trims_the_weakest_layer_first(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BLANK_USER_INSTRUCTIONS_DIR", str(tmp_path / "nouser"))
    (tmp_path / "AGENTS.md").write_text("A" * 5_000, encoding="utf-8")
    layers = load_layers(tmp_path, extra=["重要：" + "B" * 3_000])
    prompt = compose("BASE", layers, total_limit=4_000)

    assert "受总预算限制已截断" in prompt
    # 强层必须完整保留
    assert "B" * 3_000 in prompt


# --------------------------------------------------------------------------- #
# 2. 交接笔记
# --------------------------------------------------------------------------- #
def test_notes_append_keeps_a_log(tmp_path: Path) -> None:
    store = NoteStore(tmp_path / "notes")
    store.write("t1", "改到一半：main.py 第 3 行", mode="replace", step=2)
    state = store.write("t1", "已排除：不要动 setup.py", mode="append", step=4)

    assert "改到一半" in state.content
    assert "第 4 步" in state.content
    assert state.content.count("- [") == 1


def test_notes_replace_drops_previous(tmp_path: Path) -> None:
    store = NoteStore(tmp_path / "notes")
    store.write("t1", "旧状态", mode="replace")
    state = store.write("t1", "新状态", mode="replace")
    assert "旧状态" not in state.content and "新状态" in state.content


def test_notes_are_capped_and_keep_the_tail(tmp_path: Path) -> None:
    store = NoteStore(tmp_path / "notes")
    store.write("t1", "早期内容" + "x" * (MAX_NOTE_CHARS + 2_000), mode="replace")
    state = store.read("t1")
    assert state.chars <= MAX_NOTE_CHARS + 40
    assert "较早的笔记已截断" in state.content


def test_notes_reject_path_traversal(tmp_path: Path) -> None:
    store = NoteStore(tmp_path / "notes")
    with pytest.raises(NoteError):
        store.write("../../etc/passwd", "x")
    with pytest.raises(NoteError):
        store.write("t1", "   ")


def test_notes_handoff_block_carries_the_state(tmp_path: Path) -> None:
    store = NoteStore(tmp_path / "notes")
    store.write("t1", "半成品在 feature/x 分支", mode="replace")
    block = store.read("t1").handoff()
    assert block.startswith("## 跨窗口交接笔记")
    assert "feature/x" in block
    assert NoteStore(tmp_path / "notes").read("nope").handoff() == ""


# --------------------------------------------------------------------------- #
# 3. 长期记忆固化
# --------------------------------------------------------------------------- #
def test_consolidation_is_idempotent(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory")
    workspace = tmp_path / "repo"
    workspace.mkdir()

    first = store.consolidate(
        workspace,
        task_id="t1",
        title="修复登录",
        summary="登录接口返回 500，已修好",
        notes="- auth.py 的 token 解析少了一个 await",
        facts=["验证命令 pytest -q → ok"],
        at=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )
    assert first.added

    second = store.consolidate(
        workspace,
        task_id="t1",
        title="修复登录",
        summary="登录接口返回 500，已修好",
        notes="- auth.py 的 token 解析少了一个 await",
        facts=["验证命令 pytest -q → ok"],
        at=datetime(2026, 10, 2, tzinfo=timezone.utc),
    )
    assert second.added == []
    assert second.skipped >= 1
    assert store.load(workspace).count("auth.py") == 1


def test_memory_search_matches_keywords(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory")
    workspace = tmp_path / "repo"
    workspace.mkdir()
    store.consolidate(
        workspace,
        task_id="t1",
        title="构建失败",
        summary="打包时 node 版本不够，升到 24 解决",
        facts=["ELECTRON_RUN_AS_NODE 会让 Electron 退化成纯 node"],
    )
    hits = store.search(workspace, "electron 版本", limit=3)
    assert hits and any("ELECTRON_RUN_AS_NODE" in hit.bullet for hit in hits)
    assert store.search(workspace, "完全无关的词组") == []


def test_memory_is_bounded_and_drops_oldest(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory")
    workspace = tmp_path / "repo"
    workspace.mkdir()
    for index in range(60):
        store.consolidate(
            workspace,
            task_id=f"t{index}",
            title=f"第{index}次",
            summary=f"第{index}次填充" + "y" * 900,
            at=datetime(2026, 1, 1 + index % 27, tzinfo=timezone.utc),
        )
    text = store.load(workspace)
    assert len(text) <= MAX_MEMORY_CHARS + 600
    assert "第59次" in text
    assert "第0次" not in text


def test_memory_paths_differ_per_workspace(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory")
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert store.path_for(a) != store.path_for(b)


# --------------------------------------------------------------------------- #
# 4. shell 快照与环境策略
# --------------------------------------------------------------------------- #
def test_secret_looking_variables_are_dropped() -> None:
    env = {
        "PATH": "/usr/bin",
        "HOME": "/home/x",
        "OPENAI_API_KEY": "sk-abcdefghijklmnop",
        "MY_TOKEN": "abc",
        "GITHUB_TOKEN": "ghp_abcdefghijkl",
        "LANG": "zh_CN.UTF-8",
    }
    kept = filter_env(env, policy=POLICY_MINIMAL)
    assert "PATH" in kept and "LANG" in kept
    assert "OPENAI_API_KEY" not in kept
    assert "MY_TOKEN" not in kept
    assert "GITHUB_TOKEN" not in kept


def test_inherit_policy_still_filters_credentials() -> None:
    env = {"PATH": "/usr/bin", "AWS_SECRET_ACCESS_KEY": "x", "CUSTOM_FLAG": "1"}
    kept = filter_env(env, policy=POLICY_INHERIT)
    assert kept.get("CUSTOM_FLAG") == "1"
    assert "AWS_SECRET_ACCESS_KEY" not in kept


def test_strict_policy_is_a_subset_of_minimal() -> None:
    env = {"PATH": "/usr/bin", "LANG": "C", "PYTHONPATH": "/x", "SYSTEMROOT": "C:\\Windows"}
    strict = set(filter_env(env, policy=POLICY_STRICT))
    minimal = set(filter_env(env, policy=POLICY_MINIMAL))
    assert strict <= minimal
    assert "PYTHONPATH" not in strict


def test_snapshot_records_shape_not_secrets(tmp_path: Path) -> None:
    sep = os.pathsep
    env = {"PATH": sep.join(["/a", "/b"]), "HOME": "/home/x", "API_KEY": "sk-1234567890abcdef"}
    snapshot = capture(tmp_path, env=env, policy=POLICY_MINIMAL)
    view = snapshot.as_dict()
    # 审计视图给的是「形状」：变量名、PATH 条数、解释器叫什么，一条主机绝对路径都不给。
    # 这里刻意用一份像真机器的 PATH（C:\Users\<用户名>\...）——`/a`、`/b` 看不出泄漏，
    # 而这条断言要拦的正是用户名进事件流那种事。
    host_path = r"C:\Users\linprivate\AppData\Local\Programs\Python"
    env2 = {"PATH": sep.join([host_path, r"C:\Windows\system32"]), "HOME": "/home/x"}
    view2 = capture(tmp_path, env=env2, policy=POLICY_MINIMAL).as_dict()
    assert "linprivate" not in repr(view2)
    assert ":\\" not in repr(view2).replace("\\\\", "\\")
    assert view2["path_entries"] == ["<workspace:Python>", "<workspace:system32>"]
    assert "API_KEY" not in view["env_names"]
    # 掩码后仍然数得出条数、认得出目录名——审计要的「当时有几个可执行入口」没丢。
    assert view["path_entries"] == ["<workspace:a>", "<workspace:b>"]
    assert view["cwd"] == f"<workspace:{tmp_path.resolve().name}>"
    assert "sk-1234567890abcdef" not in repr(view)


def test_snapshot_pins_the_project_venv(tmp_path: Path) -> None:
    venv = tmp_path / ".venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").write_text("", encoding="utf-8")
    snapshot = capture(tmp_path, env={"PATH": "/usr/bin"}, policy=POLICY_MINIMAL)
    assert snapshot.venv.endswith("python")
    assert snapshot.venv_pinned is True
    assert snapshot.env["PATH"].split(os.pathsep)[0] == str(venv)
    assert snapshot.env["VIRTUAL_ENV"].endswith(".venv")


def test_policy_cannot_be_widened_after_capture(tmp_path: Path) -> None:
    env = {"PATH": "/usr/bin", "CUSTOM_FLAG": "1"}
    snapshot = capture(tmp_path, env=env, policy=POLICY_STRICT)
    # 严格模式下跑起来的任务，不能因为某个工具要 inherit 就拿到更多变量
    assert "CUSTOM_FLAG" not in snapshot.env_for(POLICY_INHERIT)
