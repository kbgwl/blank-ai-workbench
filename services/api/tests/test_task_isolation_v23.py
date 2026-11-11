"""J3：任务级隔离（独立根 + 唯一 AppContainer 包）的接线判据。

这些用例不证明"AppContainer 真的拦得住"——那是 `E:/tmp/j3-probe/` 的行为探针与
`tests/test_sandbox_appcontainer*` 的事。这里钉的是**接线**：谁在什么条件下被拒绝、
哪些调用必须换进任务包执行、收尾时包与副本是否一起消失。

每条判据都能翻红：

* 包名去掉任务键 → 唯一性红；包名加随机数 → 稳定性红；
* `secure_tree` 漏掉源对象库授权 → `_source_objects` 用例红；
* filesystem/patch 只看 `broker_enabled()` → 隔离路由用例红（开关默认关）；
* `_settle_isolation` 先后顺序写反 → 事件序红；失败也 harvest → 失败用例红；
* 子代理收尸不删包 → `_drop_profile` 用例红。
"""

from __future__ import annotations

import asyncio
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from blank_api.services import subagent, worktree
from blank_api.services.agent import runtime as runtime_module
from blank_api.services.models.base import ModelResponse, Usage
from blank_api.services.sandbox import task_isolation, windows_appcontainer as wac
from blank_api.services.sandbox.policy import SandboxPolicy
from blank_api.services.tools import filesystem
from blank_api.services.tools import fs_broker
from blank_api.services.tools import patch as patch_module
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.shell import execute_command

WIN_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="AppContainer 只在 Windows 上存在")


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "a.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "init"],
        cwd=path,
        check=True,
    )
    return path


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def add(self, event: str, node: str, payload: dict) -> None:
        self.events.append((event, payload))


# --------------------------------------------------------------------------- #
# 包名与源对象库
# --------------------------------------------------------------------------- #
def test_profile_name_is_stable_and_unique():
    first = task_isolation.profile_name("task-a")
    assert first == task_isolation.profile_name("task-a"), "同一任务重跑必须落到同一个包"
    assert first != task_isolation.profile_name("task-b"), "不同任务共用一个包 = 共用一个身份"
    assert first.startswith(task_isolation.PROFILE_PREFIX + "-")
    assert len(first) == len(task_isolation.PROFILE_PREFIX) + 1 + 12
    assert task_isolation.profile_name("") != task_isolation.profile_name(" "), "空键也不能撞成一个包"


def test_source_objects_points_at_the_real_object_store(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    objects = task_isolation._source_objects(repo)
    assert objects is not None and objects.is_dir()
    assert objects.resolve() == (repo / ".git" / "objects").resolve()


def test_source_objects_is_none_outside_git(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert task_isolation._source_objects(plain) is None


# --------------------------------------------------------------------------- #
# secure_tree：失败必须喊出来，不许"看起来隔离了"
# --------------------------------------------------------------------------- #
@WIN_ONLY
def test_secure_tree_fails_closed_when_profile_cannot_be_created(monkeypatch, tmp_path):
    tree = worktree.Worktree(path=tmp_path / "tree", mode="copy", base=tmp_path, name="t")
    tree.path.mkdir()

    def boom(name):
        raise RuntimeError("CreateAppContainerProfile -> 0x5")

    monkeypatch.setattr(wac, "profile", boom)
    with pytest.raises(RuntimeError):
        task_isolation.secure_tree(tree, "task-x")


def test_secure_tree_grants_source_objects_read_only(monkeypatch, tmp_path):
    repo = _git_repo(tmp_path / "repo")
    tree = worktree.Worktree(path=tmp_path / "tree", mode="git", base=repo, name="t")
    tree.path.mkdir()
    grants: list[tuple[str, str, bool]] = []

    monkeypatch.setattr(wac, "profile", lambda name: ("S-1-15-2-x", 0))
    monkeypatch.setattr(task_isolation.wr, "current_user_sid", lambda: "S-1-5-21-owner")
    monkeypatch.setattr(task_isolation.wr, "_set_private", lambda path, owner: None)
    monkeypatch.setattr(
        task_isolation.wr,
        "_grant",
        lambda path, rights, sid, recursive=False: (grants.append((path, rights, recursive)), (True, ""))[1],
    )
    name, sid = task_isolation.secure_tree(tree, "task-x", source=repo)
    assert sid == "S-1-15-2-x" and name == task_isolation.profile_name("task-x")
    assert (str(tree.path), "M", False) in grants
    objects = str((repo / ".git" / "objects").resolve())
    assert (objects, "RX", True) in grants, "漏掉源对象库授权，克隆里的 git add 会被拒"


def test_create_refuses_without_appcontainer(monkeypatch, tmp_path):
    monkeypatch.setattr(task_isolation.wac, "supported", lambda: False)
    with pytest.raises(task_isolation.TaskIsolationError, match="AppContainer"):
        task_isolation.create(tmp_path, "task-x")


def test_create_refuses_when_behavior_self_check_is_red(monkeypatch, tmp_path):
    """自检没绿就建根，执行层随后会把每条命令拒掉——任务根成了没人能进的副本。"""
    monkeypatch.setattr(task_isolation.wac, "supported", lambda: True)
    monkeypatch.setattr(
        task_isolation.wac, "self_check",
        lambda **_: wac.SelfCheck(attempted=True, error="AC 子进程没跑起来"),
    )
    with pytest.raises(task_isolation.TaskIsolationError, match="自检未通过"):
        task_isolation.create(tmp_path, "task-x")


def test_create_refuses_when_backend_would_not_be_appcontainer(monkeypatch, tmp_path):
    """Docker 在别的后端里优先级更高；建根前说清，而不是让每条命令撞"选中的不是 AC"。"""
    monkeypatch.setattr(task_isolation.wac, "supported", lambda: True)
    monkeypatch.setattr(
        task_isolation.wac, "self_check",
        lambda **_: wac.SelfCheck(
            attempted=True, secret_denied=True, workspace_read=True, workspace_write=True,
            plain_secret_read=True, bind=True, loopback=True, network_denied=True, plain_network=True,
        ),
    )
    from blank_api.services.sandbox import tiers

    monkeypatch.setattr(tiers, "resolve_tier", lambda *_, **__: SimpleNamespace(backend="docker"))
    with pytest.raises(task_isolation.TaskIsolationError, match="docker"):
        task_isolation.create(tmp_path, "task-x")


@WIN_ONLY
def test_remove_worktree_deletes_read_only_git_objects(tmp_path):
    """克隆里的 git 对象是只读文件；ignore_errors 会把"只删一半"读成成功（J3 实测）。"""
    tree_path = tmp_path / "tree"
    objects = tree_path / ".git" / "objects"
    objects.mkdir(parents=True)
    blob = objects / "pack-abc.idx"
    blob.write_text("x", encoding="utf-8")
    os.chmod(blob, stat.S_IREAD)
    worktree.remove_worktree(worktree.Worktree(path=tree_path, mode="git", base=tmp_path, name="t"))
    assert not tree_path.exists()


def test_release_reports_a_tree_that_survives_deletion(monkeypatch, tmp_path):
    tree_path = tmp_path / "tree"
    tree_path.mkdir()
    root = task_isolation.TaskRoot(
        task_id="t", path=tree_path, workspace=tmp_path,
        tree=worktree.Worktree(path=tree_path, mode="copy", base=tmp_path, name="t"),
        profile_name="BlankTask-x", sid="S-1-15-2-x", mode="copy",
    )
    monkeypatch.setattr(task_isolation.worktree, "remove_worktree", lambda tree: None)
    monkeypatch.setattr(task_isolation.wac, "delete_profile", lambda name: "")
    released = task_isolation.release(root)
    assert any("目录仍存在" in error for error in released["errors"]), released


def test_ac_read_roots_have_no_duplicates():
    roots = task_isolation.ac_read_roots()
    assert len(roots) == len(set(roots))
    assert all(Path(item).is_absolute() for item in roots)


# --------------------------------------------------------------------------- #
# 一次性 broker（--once）
# --------------------------------------------------------------------------- #
def _run_once(tmp_path: Path, request: dict) -> dict:
    import json

    request_path = tmp_path / "request.json"
    response_path = tmp_path / "response.json"
    request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    code = fs_broker.once(str(tmp_path), str(request_path), str(response_path))
    assert code == 0
    return json.loads(response_path.read_text(encoding="utf-8"))


def test_once_mode_reads_and_writes_files(tmp_path):
    (tmp_path / "hello.txt").write_text("你好\n", encoding="utf-8")
    response = _run_once(tmp_path, {
        "id": "r1", "op": "read_text_file", "args": {"path": "hello.txt"},
    })
    assert response["id"] == "r1" and response["ok"] is True
    assert response["result"]["payload"]["content"].strip() == "你好"


def test_once_mode_encodes_business_errors():
    # 路径越界是 ApiError：必须按类型回帧，而不是让调用方读到超时。
    import tempfile

    with tempfile.TemporaryDirectory() as root:
        response = _run_once(Path(root), {
            "id": "r2", "op": "read_text_file", "args": {"path": "../outside.txt"},
        })
    assert response["ok"] is False
    assert response["error"]["type"] == "api_error"


def test_once_mode_unknown_op_is_a_value_error(tmp_path):
    response = _run_once(tmp_path, {"id": "r3", "op": "nope", "args": {}})
    assert response["ok"] is False and response["error"]["type"] == "value"


def test_once_mode_writes_a_response_even_for_a_broken_request_file(tmp_path):
    response_path = tmp_path / "response.json"
    code = fs_broker.once(str(tmp_path), str(tmp_path / "missing.json"), str(response_path))
    assert code == 0
    assert '"ok": false' in response_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_call_isolated_refuses_without_a_profile(tmp_path):
    with pytest.raises(fs_broker.FsBrokerError) as caught:
        await fs_broker.call_isolated(tmp_path, "t", "list_tree", {}, profile_name="")
    assert caught.value.layer == fs_broker.LAYER_SPAWN
    assert caught.value.side_effects == fs_broker.SIDE_EFFECTS_NONE


# --------------------------------------------------------------------------- #
# 文件工具路由：隔离态不看 broker 开关
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_list_files_routes_into_the_isolation_profile(monkeypatch, tmp_path):
    monkeypatch.setattr(fs_broker, "broker_enabled", lambda: False)
    seen: dict = {}

    async def fake_isolated(root, task_id, op, args, *, profile_name):
        seen.update(root=root, op=op, args=args, profile=profile_name)
        return {
            "result": {"entries": [{"path": "a.txt", "name": "a.txt", "is_dir": False, "size": 3}]},
            "latency": {},
            "status": {"executed_by": "fs_broker_appcontainer"},
        }

    monkeypatch.setattr(fs_broker, "call_isolated", fake_isolated)
    context = ToolContext(
        root=tmp_path, task_id="t", session_factory=None, isolation_profile="BlankTask-x"
    )
    outcome = await filesystem._list_files(context, {})
    assert outcome.ok
    assert seen["op"] == "list_tree" and seen["profile"] == "BlankTask-x"


@pytest.mark.asyncio
async def test_read_file_keeps_host_path_when_no_profile(monkeypatch, tmp_path):
    monkeypatch.setattr(fs_broker, "broker_enabled", lambda: False)
    (tmp_path / "a.txt").write_text("host\n", encoding="utf-8")

    async def forbidden(*args, **kwargs):
        raise AssertionError("没有隔离包时不该走隔离通道")

    monkeypatch.setattr(fs_broker, "call_isolated", forbidden)
    context = ToolContext(root=tmp_path, task_id="t", session_factory=None)
    outcome = await filesystem._read_file(context, {"path": "a.txt"})
    assert outcome.ok and outcome.output.strip() == "host"


@pytest.mark.asyncio
async def test_apply_patch_routes_into_the_isolation_profile(monkeypatch, tmp_path):
    monkeypatch.setattr(fs_broker, "broker_enabled", lambda: False)
    seen: dict = {}

    async def fake_isolated(root, task_id, op, args, *, profile_name):
        seen.update(op=op, profile=profile_name, args=args)
        return {
            "result": {"result": {"missing_path": True}},
            "latency": {},
            "status": {"executed_by": "fs_broker_appcontainer"},
        }

    monkeypatch.setattr(fs_broker, "call_isolated", fake_isolated)
    context = ToolContext(
        root=tmp_path, task_id="t", session_factory=None, isolation_profile="BlankTask-y"
    )
    outcome = await patch_module._apply_patch(context, {"patch": "@@ -1 +1 @@\n-a\n+b\n"})
    assert outcome.ok is False and "没有文件路径" in outcome.message
    assert seen["op"] == "patch" and seen["profile"] == "BlankTask-y"
    assert seen["args"]["task_id"] == "t"


# --------------------------------------------------------------------------- #
# 命令执行：隔离态三条 fail-closed 路径
# --------------------------------------------------------------------------- #
def _enabled_resolution():
    return SimpleNamespace(
        tier="workspace",
        available=True,
        notice="",
        policy=SandboxPolicy(enabled=True),
        as_dict=lambda: {"tier": "workspace", "available": True},
    )


@pytest.mark.asyncio
async def test_command_is_refused_when_the_tier_has_no_sandbox(tmp_path):
    from blank_api.services.sandbox import tiers

    result = await execute_command(
        "echo should-not-run", tmp_path, 5, tier=tiers.TIER_OFF, isolation_profile="BlankTask-x"
    )
    assert result["isolation_blocked"] is True
    assert result["sandbox"] is None
    assert result["exit_code"] is None and result["stdout"] == ""
    assert "命令未执行" in result["stderr"]


@pytest.mark.asyncio
async def test_command_is_refused_for_host_bypass_under_isolation(monkeypatch, tmp_path):
    from blank_api.services import sandbox as sandbox_pkg
    from blank_api.services.sandbox import tiers

    monkeypatch.setattr(tiers, "resolve_tier", lambda requested="": _enabled_resolution())
    monkeypatch.setattr(sandbox_pkg, "get_sandbox", lambda policy=None: SimpleNamespace(name="local"))
    result = await execute_command(
        "echo should-not-run", tmp_path, 5, tier="workspace",
        host_bypass=True, isolation_profile="BlankTask-x",
    )
    assert result["isolation_blocked"] is True
    assert "不允许单次越界宿主执行" in result["stderr"]


@pytest.mark.asyncio
async def test_command_is_refused_when_the_backend_is_not_appcontainer(monkeypatch, tmp_path):
    from blank_api.services import sandbox as sandbox_pkg
    from blank_api.services.sandbox import tiers

    monkeypatch.setattr(tiers, "resolve_tier", lambda requested="": _enabled_resolution())
    monkeypatch.setattr(sandbox_pkg, "get_sandbox", lambda policy=None: SimpleNamespace(name="local"))
    result = await execute_command(
        "echo should-not-run", tmp_path, 5, tier="workspace", isolation_profile="BlankTask-x"
    )
    assert result["isolation_blocked"] is True
    assert "需要 AppContainer 后端" in result["stderr"]


# --------------------------------------------------------------------------- #
# 子代理：包与副本同生共死
# --------------------------------------------------------------------------- #
def test_subagent_result_hides_the_profile_from_events():
    result = subagent.SubAgentResult(ok=True, isolation_profile="BlankTask-x")
    assert "isolation_profile" not in result.as_dict()
    assert "BlankTask" not in repr(result)


def test_drop_profile_is_a_noop_for_empty_names(monkeypatch):
    called: list[str] = []
    monkeypatch.setattr(wac, "delete_profile", lambda name: called.append(name) or "")
    subagent._drop_profile("")
    assert called == []
    subagent._drop_profile("BlankTask-x")
    assert called == ["BlankTask-x"]


@pytest.mark.asyncio
async def test_cancelled_subagent_drops_both_tree_and_profile(monkeypatch, tmp_path):
    removed: list = []
    dropped: list[str] = []

    async def fake_remove(tree, *, hooks=None):
        removed.append(tree.path)

    monkeypatch.setattr(subagent, "remove_worktree_with_hooks", fake_remove)
    monkeypatch.setattr(subagent, "_drop_profile", lambda name: dropped.append(name))
    recorder = _Recorder()
    prepared = SimpleNamespace(
        agent_id="agent-1",
        agent_path="root/sub-1",
        request=SimpleNamespace(goal="g"),
        session=SimpleNamespace(recorder=recorder, hooks=None),
    )
    runner = subagent.SubAgentRunner()
    tracker = {
        "tree": SimpleNamespace(path=tmp_path / "tree", mode="copy"),
        "profile": "BlankTask-x",
    }
    await runner._cleanup_cancelled(prepared, tracker)
    assert removed == [tmp_path / "tree"]
    assert dropped == ["BlankTask-x"]
    assert tracker == {}


class _SilentAdapter:
    """不调工具、直接给结论的模型：只验证隔离根的建与收，不烧真模型。"""

    class config:
        name = "fake"

    last_provider = ""

    async def bind_tools(self, specs, chat):
        return ModelResponse(text="done", usage=Usage(), model="fake", response_id="r1")


@pytest.mark.asyncio
async def test_subagent_runs_inside_its_own_clone_and_cleans_up(monkeypatch, tmp_path):
    repo = _git_repo(tmp_path / "repo")
    dropped: list[str] = []
    grants: list[tuple[str, str]] = []

    monkeypatch.setattr(task_isolation, "enabled", lambda: True)
    monkeypatch.setattr(wac, "profile", lambda name: ("S-1-15-2-x", 0))
    monkeypatch.setattr(wac, "delete_profile", lambda name: dropped.append(name) or "")
    monkeypatch.setattr(task_isolation.wr, "current_user_sid", lambda: "S-1-5-21-owner")
    monkeypatch.setattr(task_isolation.wr, "_set_private", lambda path, owner: None)
    monkeypatch.setattr(
        task_isolation.wr,
        "_grant",
        lambda path, rights, sid, recursive=False: (grants.append((path, rights)), (True, ""))[1],
    )

    async def fake_account(**kwargs):
        return {}

    monkeypatch.setattr(subagent, "account_call", fake_account)
    result = await subagent.run_subagent(
        adapter=_SilentAdapter(),
        tools=[],
        root=repo,
        task_id="task-1",
        session_factory=None,
        recorder=_Recorder(),
        request=subagent.SubAgentRequest(goal="看一眼", isolate=True),
        agent_path="root/sub-1",
        depth=1,
        parent_thread_id="p",
    )
    assert result.ok, result.error
    assert result.summary == "done"
    assert result.isolation_profile == "", "非保留模式的包必须随副本一起删掉"
    assert len(dropped) == 1 and dropped[0].startswith(task_isolation.PROFILE_PREFIX)
    assert not Path(result.worktree["path"]).exists(), "子代理的克隆必须被删掉"


# --------------------------------------------------------------------------- #
# 运行时收尾：成功先合并再释放，失败只释放
# --------------------------------------------------------------------------- #
def _settle_scope(tmp_path: Path, task_id: str = "t"):
    iso = tmp_path / "iso"
    iso.mkdir(exist_ok=True)
    real = tmp_path / "real"
    real.mkdir(exist_ok=True)
    recorder = _Recorder()
    session = SimpleNamespace(
        context=ToolContext(
            root=iso, task_id=task_id, session_factory=None, isolation_profile="BlankTask-x"
        )
    )
    scope = SimpleNamespace(
        recorder=recorder,
        session=session,
        isolation=SimpleNamespace(path=iso, workspace=real),
        isolation_settled=False,
    )
    return scope, recorder, real


@pytest.mark.asyncio
async def test_settle_isolation_harvests_then_releases(monkeypatch, tmp_path):
    calls: list[tuple[str, str]] = []

    async def fake_harvest(root):
        calls.append(("harvest", str(root.path)))
        return {"applied": ["a.txt"]}

    async def fake_release(root):
        calls.append(("release", str(root.path)))
        return {"released": True, "errors": []}

    monkeypatch.setattr(task_isolation, "harvest_async", fake_harvest)
    monkeypatch.setattr(task_isolation, "release_async", fake_release)
    scope, recorder, real = _settle_scope(tmp_path)
    await runtime_module.AgentRuntime(None)._settle_isolation(scope, "succeeded")
    assert [name for name, _ in calls] == ["harvest", "release"]
    assert [event for event, _ in recorder.events] == ["isolation.harvested", "isolation.released"]
    assert scope.session.context.root == real
    assert scope.session.context.isolation_profile == ""


@pytest.mark.asyncio
async def test_settle_isolation_releases_without_harvest_on_failure(monkeypatch, tmp_path):
    calls: list[str] = []

    async def forbidden(root):  # pragma: no cover - 失败任务不许合并
        raise AssertionError("失败/取消的任务不该 harvest")

    async def fake_release(root):
        calls.append("release")
        return {"released": True, "errors": ["卸载任务卷失败"]}

    monkeypatch.setattr(task_isolation, "harvest_async", forbidden)
    monkeypatch.setattr(task_isolation, "release_async", fake_release)
    scope, recorder, real = _settle_scope(tmp_path)
    await runtime_module.AgentRuntime(None)._settle_isolation(scope, "failed")
    assert calls == ["release"]
    assert [event for event, _ in recorder.events] == ["isolation.release_errors"]
    errors = recorder.events[0][1]
    assert errors["errors"] == ["卸载任务卷失败"]
    assert scope.session.context.root == real
