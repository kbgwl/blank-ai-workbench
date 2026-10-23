"""`world.state`：对标 Codex rollout 里的 `world_state{full:true, state:{…}}`（见 docs/REA-WAVE4-CODEX-RUNTIME.md §4）。

四条判定，每一条都能失败：

1. 指令正文绝不进账本，只留「哪一层 + 内容指纹」。把 `layer_ref` 退回 `str(layer)` 就红——
   那是本轮实测到的真实缺陷：一条 `turn.context` 4145 字节，整份 AGENTS.md 连主机路径一起
   进了事件流、证据包和全文索引。
2. 指纹真的指认内容：改一个字节，sha 必须变。写死常量或只取文件名的实现过不了这条。
3. 主机路径不进账本：cwd、解释器、PATH 条目都只留名字。往 PATH 里塞一个带私人名目录的条目，
   事件里不许出现那个名字。
4. 去重成立且变更可指认：世界没变只发一条；换一版指令、换一个 shell 才发第二条。
   把秒级时钟或 `created_at` 混进指纹，这条就红（同一个世界连发几十条）。
"""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from blank_api.db import get_session_factory
from blank_api.models.project import Project
from blank_api.models.task import AgentEvent, Task
from blank_api.services.agent import governance
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.evidence import build_evidence
from blank_api.services.agent.graph import AgentSession
from blank_api.services.instructions import InstructionLayer, content_sha, load_layers, read_layer
from blank_api.services.shell_env import ShellSnapshot
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.gateway import ToolGateway

TASK_ID = "task-world-1"
MARKER = "只此一份的指令正文标记 ALPHA-7731"


def _adapter(model: str = "test-model") -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(
            model=model, reasoning_effort="", verbosity="", context_window=8000, supports_tools=True
        )
    )


def _layer(path: str, text: str, source: str = "project") -> InstructionLayer:
    return InstructionLayer(source=source, path=path, content=text, truncated=False)


async def _seed() -> None:
    from blank_api.db import Base, get_engine

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with get_session_factory()() as session:
        session.add(Project(id="project-world", name="环境快照"))
        session.add(Task(id=TASK_ID, project_id="project-world", title="世界", goal="世界", max_steps=10))
        await session.commit()


@pytest.fixture
async def seeded():
    await _seed()


def _session(tmp_path, *, layers=(), shell=None) -> AgentSession:
    factory = get_session_factory()
    return AgentSession(
        adapter=_adapter(),  # type: ignore[arg-type]
        gateway=ToolGateway([]),
        context=ToolContext(root=tmp_path, task_id=TASK_ID, session_factory=factory),
        recorder=EventRecorder(factory, TASK_ID),
        session_factory=factory,
        provider_id="provider-1",
        provider_name="mock",
        goal="世界",
        max_steps=10,
        instruction_layers=list(layers),
        shell=shell,
    )


async def _events(type_name: str) -> list[dict]:
    """按 seq 有序取回事件载荷：seq 是确定性的，主键不是。"""
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                AgentEvent.__table__.select()
                .where(AgentEvent.type == type_name)
                .order_by(AgentEvent.seq)
            )
        ).fetchall()
    return [row.payload for row in rows]


# --- 1/2：内容用指纹指认，正文一个字都不抄 --------------------------------------


def test_layer_ref_carries_fingerprint_not_the_text(tmp_path):
    content = f"前置说明\n{MARKER}\n收尾"
    ref = governance.layer_ref(_layer(str(tmp_path / "AGENTS.md"), content))
    blob = json.dumps(ref, ensure_ascii=False)
    assert MARKER not in blob, "指令正文被抄进事件流：证据包和全文索引都会带上它"
    assert str(tmp_path) not in blob, "主机绝对路径没掩码"
    assert ref["chars"] == len(content)
    assert set(ref) == {"source", "path", "chars", "sha", "truncated"}
    # 指纹必须等于独立算出的内容哈希，否则「指认版本」这句话没有依据
    assert ref["sha"] == hashlib.sha256(content.encode("utf-8")).hexdigest()[:40]


def test_fingerprint_moves_with_a_single_byte():
    base = governance.layer_ref(_layer("p", "本项目要求 一律用中文回复"))
    edited = governance.layer_ref(_layer("p", "本项目要求 一律用英文回复"))
    assert base["sha"] != edited["sha"], "一字之差指纹不变，就等于无法指认改了没有"
    same = governance.layer_ref(_layer("其它路径", "本项目要求 一律用中文回复"))
    assert same["sha"] == base["sha"], "同内容应同指纹：路径不同不该被说成指令变了"


def test_string_layer_is_referenced_not_copied():
    ref = governance.layer_ref(f"任务级附加指令 {MARKER}")
    blob = json.dumps(ref, ensure_ascii=False)
    assert MARKER not in blob
    assert ref["sha"] == content_sha(f"任务级附加指令 {MARKER}")


def test_layers_digest_orders_matter():
    a, b = _layer("p", "第一层"), _layer("q", "第二层", source="user")
    assert governance.instructions_digest([a, b]) == governance.instructions_digest([a, b])
    assert governance.instructions_digest([a, b]) != governance.instructions_digest([b, a]), (
        "同样两层换个顺序，模型看到的提示词就不同；顺序不进指纹就是撒谎"
    )


# --- 1：turn.context 里的 instructions 也改成引用（本轮实测的泄漏点） -----------


async def test_turn_context_instructions_are_references(seeded, tmp_path):
    session = _session(tmp_path, layers=[_layer(str(tmp_path / "AGENTS.md"), f"# 约定\n{MARKER}\n" + "补" * 2000)])
    payload = await session.emit_governance("call_model", 0, _adapter(), "")
    blob = json.dumps(payload, ensure_ascii=False)
    assert MARKER not in blob
    field_bytes = len(json.dumps(payload["instructions"], ensure_ascii=False))
    assert field_bytes < 400, f"instructions 字段 {field_bytes} 字节：正文没被指纹取代"
    assert all(set(item) == {"source", "path", "chars", "sha", "truncated"} for item in payload["instructions"])


# --- 3：主机路径不进账本 --------------------------------------------------------


def _shell(tmp_path, *, path_entries=("C:\\Windows\\System32",), created_at="2026-10-22T10:00:00") -> ShellSnapshot:
    return ShellSnapshot(
        cwd=str(tmp_path),
        policy="minimal",
        shell="C:\\Windows\\System32\\cmd.exe",
        python="C:\\Users\\linprivate\\env\\python-3.12\\python.exe",
        venv="",
        path_entries=tuple(path_entries),
        env={"PATH": ";".join(path_entries), "SYSTEMROOT": "C:\\Windows"},
        created_at=created_at,
        venv_pinned=False,
    )


async def test_environment_snapshot_keeps_names_not_host_paths(seeded, tmp_path):
    shell = _shell(tmp_path, path_entries=("C:\\Users\\linprivate\\bin", "C:\\Windows\\System32"))
    session = _session(tmp_path, shell=shell)
    payload = await session.emit_world_state("call_model")
    blob = json.dumps(payload, ensure_ascii=False)
    assert "linprivate" not in blob, "PATH/解释器里的私人目录进了事件流"
    assert str(tmp_path) not in blob
    local = payload["state"]["environments"]["local"]
    assert local["status"] == "captured"
    assert local["shell"] == "cmd.exe" and "\\" not in local["shell"]
    assert local["python"] == "python.exe"
    assert local["path_entries"] == 2, "PATH 要说清有几条，但不给出是哪几条"
    assert local["env_names"] == ["PATH", "SYSTEMROOT"]


async def test_environment_snapshot_without_a_shell_layer(seeded, tmp_path):
    payload = await _session(tmp_path).emit_world_state("call_model")
    local = payload["state"]["environments"]["local"]
    # 没装配 shell 层也是事实，要写成 unknown，不能编一个像是探到的 shell 名
    assert local["status"] == "unknown"
    assert local["shell"] == "" and local["path_entries"] == 0


# --- 4：全量快照的形状 + 去重纪律 ----------------------------------------------


async def test_world_state_shape_matches_codex_full_snapshot(seeded, tmp_path):
    session = _session(tmp_path, layers=[_layer(str(tmp_path / "AGENTS.md"), "# 约定")])
    payload = await session.emit_world_state("call_model")
    assert payload["full"] is True, "Codex 的语义是这条即全量，读的人不必往前拼增量"
    state = payload["state"]
    assert set(state) == {
        "operating_mode",
        "instructions",
        "environments",
        "context_window_guidance",
        "current_date",
        "timezone",
    }
    assert state["operating_mode"]["instructions"] == governance.instructions_digest(session.instruction_layers)
    assert state["context_window_guidance"]["context_window"] == session.compactor.context_window
    assert state["operating_mode"]["model"] == "test-model"


async def test_same_world_emits_once_changed_world_emits_again(seeded, tmp_path):
    session = _session(tmp_path, layers=[_layer(str(tmp_path / "AGENTS.md"), "旧的一版约定")])
    await session.emit_world_state("call_model")
    await session.emit_world_state("call_model")
    rows = await _events("world.state")
    assert len(rows) == 1, "环境没变就连发，时间线会被同一个世界灌满"

    session.instruction_layers = [_layer(str(tmp_path / "AGENTS.md"), "改过的一个字节")]
    await session.emit_world_state("call_model")
    rows = await _events("world.state")
    assert len(rows) == 2, "改了指令文件却没有第二条：指纹没算进内容"
    first, second = rows[0]["state"], rows[1]["state"]
    assert first["operating_mode"]["instructions"] != second["operating_mode"]["instructions"]
    assert [item["sha"] for item in first["instructions"]] != [item["sha"] for item in second["instructions"]]


async def test_snapshot_clock_does_not_count_as_a_new_world(seeded, tmp_path):
    """两条只差 created_at 的 shell 快照必须算同一个世界。

    这条是给指纹划的界：秒级时钟进了指纹，去重就永久失效——M1b 的
    `boundary_measured_seconds_ago` 已经踩过一次，不能在新事件上重演。
    """
    session = _session(tmp_path, shell=_shell(tmp_path, created_at="2026-10-22T10:00:00"))
    await session.emit_world_state("call_model")
    session.shell = _shell(tmp_path, created_at="2026-10-22T10:00:01")
    await session.emit_world_state("call_model")
    assert len(await _events("world.state")) == 1

    # 真的换了环境（PATH 从一条变成两条）必须留痕
    session.shell = _shell(tmp_path, path_entries=("C:\\Windows\\System32", "D:\\tools\\bin"))
    await session.emit_world_state("call_model")
    assert len(await _events("world.state")) == 2, "PATH 条数变了是真换了环境，必须留痕"


# --- 真实读文件路径：哈希能对上磁盘上的那一版 ----------------------------------


async def test_hash_matches_the_file_on_disk(seeded, tmp_path):
    """落库的哈希必须能被独立复算：否则「指认版本」只是自说自话。"""
    target = tmp_path / "AGENTS.md"
    raw = "# 本项目约定\r\n- 回复一律用中文\r\n- 不要提交 .env\r\n"
    # 用 write_bytes：write_text 在 Windows 上会把 "\n" 翻成 "\r\n"，
    # 于是 "\r\n" 变 "\r\r\n"，读回来多出一个空行——第一版就被这个假象判成「哈希对不上」。
    target.write_bytes(raw.encode("utf-8"))
    layers = load_layers(tmp_path)
    assert [item.path for item in layers] == [str(target)]
    session = _session(tmp_path, layers=layers)
    payload = await session.emit_world_state("call_model")
    recorded = payload["state"]["instructions"][0]["sha"]
    # 独立算一遍：换行归一 + strip（这就是真正被拼进提示词的那段文本）
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n").strip()
    assert recorded == hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:40]
    assert recorded == content_sha(read_layer(target).content)
    # 改文件 → 指纹变（把哈希算在读取之前就红在这里）
    target.write_bytes((raw + "- 新增一条约定\r\n").encode("utf-8"))
    session.instruction_layers = load_layers(tmp_path)
    changed = (await session.emit_world_state("call_model"))["state"]["instructions"][0]["sha"]
    assert changed != recorded


# --- 证据包：最后一条就是终态，且带条数 ----------------------------------------


# --- 接线：真调用路径上必须真的发得出（变异 M6 专门打这条） ---------------------


async def test_world_state_is_emitted_on_the_real_call_path(seeded, tmp_path):
    """前面所有用例都直接调 `emit_world_state`，那只证明builder 会说话。

    这条走真正的 `call_model`：把 graph 里那一行接线摘掉，事件就没了，这条必须红。
    做法沿用 #29 —— 让快照之后的第一步（压缩）先炸，事件却已经落下。
    """
    content = f"# 约定 {MARKER}"
    session = _session(tmp_path, layers=[_layer(str(tmp_path / "AGENTS.md"), content)], shell=_shell(tmp_path))

    async def explode(*_a, **_k):
        raise RuntimeError("压缩这一步先炸")

    session.compactor.build = explode  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await session.call_model([{"role": "user", "content": "跑一下"}], "call_model", step=0)

    rows = await _events("world.state")
    assert len(rows) == 1, "模型调用真发生了，事件流里却查不到当时的那个世界"
    assert rows[0]["state"]["instructions"][0]["sha"] == content_sha(content)
    assert rows[0]["state"]["context_window_guidance"]["context_window"] == session.compactor.context_window
    assert MARKER not in json.dumps(rows[0], ensure_ascii=False)


async def test_evidence_pack_carries_current_world_and_change_count(seeded, tmp_path):
    session = _session(tmp_path, layers=[_layer(str(tmp_path / "AGENTS.md"), "第一版")])
    await session.emit_world_state("call_model")
    session.instruction_layers = [_layer(str(tmp_path / "AGENTS.md"), "第二版")]
    await session.emit_world_state("call_model")

    evidence = await build_evidence(get_session_factory(), TASK_ID)
    world = evidence["world"]
    assert world["snapshots"] == 2
    # current 必须是最后一条（全量语义）：拿第一条当现状会把改过的指令说成没改
    assert world["current"]["state"]["operating_mode"]["instructions"] == governance.instructions_digest(
        session.instruction_layers
    )
    assert [item["node"] for item in world["changes"]] == ["call_model"]
    assert world["current"]["full"] is True
    # 证据包会被分享、进全文索引：主机路径不许在里面
    assert str(tmp_path) not in json.dumps(world, ensure_ascii=False, default=str)
