"""隔离副本的生命周期钩子：非 git 工作区也能由用户决定副本落在哪。

三条判据，每条都能在撤掉实现后变红：

1. ``worktree_create`` 返回的路径必须真的被用上——副本里的内容来自钩子脚本，
   而不是从主工作区复制来的。只看 mode 字段不够：假实现照样可以标个 hook。
2. 钩子说建好了但路径不存在时，必须退回默认隔离并在 notes 里说明；
   直接信任一条不存在的路径，子代理下一步就会在"工作区不存在"上失败。
3. 显式 ``deny`` 是硬拒绝：不许兜底复制，也不许留下半份副本。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from blank_api.services.hooks import HookRunner
from blank_api.services.models.base import AdapterConfig, ModelAdapter, ModelResponse, Usage
from blank_api.services.subagent import SubAgentRequest, SubAgentRunner, run_subagent
from blank_api.services.tools.base import ToolContext
from blank_api.services.worktree import (
    create_worktree_with_hooks,
    remove_worktree_with_hooks,
)


def _command(script: Path, *args: Path | str) -> str:
    parts = [f'"{Path(sys.executable).as_posix()}"', f'"{script.as_posix()}"']
    parts.extend(f'"{Path(arg).as_posix() if isinstance(arg, Path) else arg}"' for arg in args)
    return " ".join(parts)


def _write_create_hook(tmp_path: Path, target: Path, *, create: bool = True) -> Path:
    script = tmp_path / "create_worktree.py"
    script.write_text(
        "import json, pathlib, sys\n"
        f"target = pathlib.Path(sys.argv[1])\n"
        + ("target.mkdir(parents=True, exist_ok=True)\n"
           "(target / 'main.py').write_text(\"print('hooked')\\n\", encoding='utf-8')\n"
           if create else "")
        + "json.load(sys.stdin)\n"
        "print(json.dumps({'path': sys.argv[1]}))\n",
        encoding="utf-8",
    )
    return script


def _write_remove_hook(tmp_path: Path, marker: Path) -> Path:
    script = tmp_path / "remove_worktree.py"
    script.write_text(
        "import json, pathlib, sys\n"
        f"payload = json.load(sys.stdin)\n"
        f"pathlib.Path(sys.argv[1]).write_text(json.dumps(payload), encoding='utf-8')\n"
        "print(json.dumps({'decision': 'allow'}))\n",
        encoding="utf-8",
    )
    return script


class FinalAdapter(ModelAdapter):
    def __init__(self) -> None:
        super().__init__(
            AdapterConfig(
                name="fake",
                kind="openai",
                base_url="http://localhost",
                api_key="",
                model="fake",
                supports_tools=False,
            )
        )

    async def invoke(self, messages, **kwargs) -> ModelResponse:
        return ModelResponse(text="ok", usage=Usage(1, 1), model="fake")

    async def stream(self, messages, **kwargs):  # pragma: no cover - 子代理不用它
        yield ""

    async def bind_tools(self, specs, messages, **kwargs) -> ModelResponse:
        return ModelResponse(text="这轮不需要工具", usage=Usage(4, 2), model="fake")


class FakeRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    async def add(self, event: str, node: str, payload: dict) -> None:
        self.events.append((event, node, payload))

    def of(self, event: str) -> list[dict]:
        return [payload for name, _, payload in self.events if name == event]


async def test_hook_supplied_path_is_used_for_a_non_git_workspace(workspace: Path, tmp_path: Path):
    plain, _ = await create_worktree_with_hooks(workspace, label="plain")
    try:
        assert plain.mode == "copy", "对照格：这个工作区不是 git 仓库，默认路径只能是整树复制"
    finally:
        await remove_worktree_with_hooks(plain)

    target = tmp_path / "hooked-tree"
    script = _write_create_hook(tmp_path, target)
    hooks = HookRunner.from_env(
        json.dumps({"worktree_create": [{"command": _command(script, target)}]})
    )
    tree, results = await create_worktree_with_hooks(workspace, label="probe", hooks=hooks)
    try:
        assert tree.mode == "hook"
        assert Path(tree.path) == target.resolve()
        # 内容来自钩子脚本，不是从主工作区复制的 print('hi')
        assert (tree.path / "main.py").read_text(encoding="utf-8") == "print('hooked')\n"
        assert results and Path(results[0].worktree_path) == target.resolve()
    finally:
        await remove_worktree_with_hooks(tree, hooks=hooks)
    assert not target.exists(), "钩子建的副本也要由清理逻辑删掉"


async def test_remove_hook_sees_the_copy_path(workspace: Path, tmp_path: Path):
    target = tmp_path / "hooked-remove"
    marker = tmp_path / "removed.json"
    create_script = _write_create_hook(tmp_path, target)
    remove_script = _write_remove_hook(tmp_path, marker)
    hooks = HookRunner.from_env(
        json.dumps(
            {
                "worktree_create": [{"command": _command(create_script, target)}],
                "worktree_remove": [{"command": _command(remove_script, marker)}],
            }
        )
    )
    tree, _ = await create_worktree_with_hooks(workspace, label="remove", hooks=hooks)
    results = await remove_worktree_with_hooks(tree, hooks=hooks)

    assert [item.status for item in results] == ["ok"]
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["path"] == str(tree.path)
    assert payload["mode"] == "hook"
    assert not tree.path.exists()


async def test_unusable_hook_path_falls_back_instead_of_faking_isolation(
    workspace: Path, tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("BLANK_WORKTREE_ROOT", str(tmp_path / "trees"))
    missing = tmp_path / "never-created"
    script = _write_create_hook(tmp_path, missing, create=False)
    hooks = HookRunner.from_env(
        json.dumps({"worktree_create": [{"command": _command(script, missing)}]})
    )
    tree, _ = await create_worktree_with_hooks(workspace, label="fallback", hooks=hooks)
    try:
        assert tree.mode == "copy"
        assert Path(tree.path) != missing
        assert tree.path.is_dir() and (tree.path / "main.py").is_file()
        assert "不可用" in tree.notes, "退回默认路径必须留下原因，不能静默换路"
    finally:
        await remove_worktree_with_hooks(tree, hooks=hooks)


async def test_deny_stops_isolation_instead_of_copying(workspace: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BLANK_WORKTREE_ROOT", str(tmp_path / "trees"))
    script = tmp_path / "deny.py"
    script.write_text(
        "import json, sys\n"
        "json.load(sys.stdin)\n"
        "print(json.dumps({'decision': 'deny', 'reason': '这台机器不许建副本'}))\n",
        encoding="utf-8",
    )
    hooks = HookRunner.from_env(
        json.dumps({"worktree_create": [{"command": _command(script)}]})
    )
    with pytest.raises(ValueError, match="拒绝"):
        await create_worktree_with_hooks(workspace, label="deny", hooks=hooks)
    trees = tmp_path / "trees"
    assert not trees.exists() or not any(trees.iterdir()), "被拒绝时不许留下兜底副本"


class FakeSession:
    """够 SubAgentRunner 用的会话形状：钩子必须从这里一层层传到 run_subagent。"""

    def __init__(self, root: Path, hooks: HookRunner) -> None:
        self.adapter = FinalAdapter()
        self.recorder = FakeRecorder()
        self.trace_id = "thread-118"
        self.depth = 0
        self.agent_path = "root"
        self.session_factory = None
        self.hooks = hooks
        self.context = ToolContext(root=root, task_id="task-118", session_factory=None)


async def test_runner_passes_session_hooks_down_to_the_subagent(
    client, workspace: Path, tmp_path: Path
):
    """真实入口是 SubAgentRunner：会话上的钩子必须传到副本创建，不能在中间被丢掉。"""
    target = tmp_path / "runner-tree"
    script = _write_create_hook(tmp_path, target)
    hooks = HookRunner.from_env(
        json.dumps({"worktree_create": [{"command": _command(script, target)}]})
    )
    session = FakeSession(workspace, hooks)
    runner = SubAgentRunner().bind(session)
    result = await runner(
        SubAgentRequest(goal="看一眼仓库结构", isolate=True, keep_worktree=True, max_steps=1)
    )
    try:
        assert result.ok, result.error
        assert result.worktree["mode"] == "hook"
        assert Path(result.worktree["path"]) == target.resolve()
    finally:
        if result.tree is not None:
            await remove_worktree_with_hooks(result.tree, hooks=hooks)


async def test_subagent_isolation_goes_through_the_hook(client, workspace: Path, tmp_path: Path):
    target = tmp_path / "subagent-tree"
    script = _write_create_hook(tmp_path, target)
    hooks = HookRunner.from_env(
        json.dumps({"worktree_create": [{"command": _command(script, target)}]})
    )
    recorder = FakeRecorder()
    result = await run_subagent(
        adapter=FinalAdapter(),
        tools=[],
        root=workspace,
        task_id="task-118",
        session_factory=None,
        recorder=recorder,
        request=SubAgentRequest(goal="看一眼仓库结构", isolate=True, keep_worktree=True, max_steps=1),
        agent_path="root/sub-1",
        depth=1,
        parent_thread_id="thread-118",
        hooks=hooks,
    )
    try:
        assert result.ok, result.error
        assert result.worktree["mode"] == "hook"
        assert Path(result.worktree["path"]) == target.resolve()
        assert "print('hooked')" in result.diff, "diff 要相对主工作区算出，证明跑在钩子给的副本里"
        created = recorder.of("worktree.created")
        assert created and created[0]["mode"] == "hook"
        hook_events = [item for item in recorder.of("hook.completed") if item["event"] == "worktree_create"]
        assert hook_events, "钩子执行必须留审计记录"
    finally:
        if result.tree is not None:
            await remove_worktree_with_hooks(result.tree, hooks=hooks)
