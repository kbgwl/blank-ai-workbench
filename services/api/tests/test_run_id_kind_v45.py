"""#71：`run_id` 四种形状的读侧命名。

账本里实测共存四种形状（#46 量出来的 873 行 / 11 个 distinct）：

- 空串：回合归属上线前的账，**当时没记**，不是第 0 轮；
- `team-<轮 id>-<消息 id>`：群聊发言（`-resume` / `-approval` 后缀属同一轮）；
- `task:<uuid>:attempt:N`：同步 `POST /tasks/{id}/run` 的兜底键；
- `agent_runs.id`（uuid 或测试里的任意串）：排队入口的真实运行 id。

这里只给读侧一个名字，不改存量值、不做数据迁移。证据包 `usage.turns[]` 因此能直接
回答"这一行是哪种出处"，而不必让界面自己猜前缀。
"""

from types import SimpleNamespace

from blank_api.services.agent.evidence import run_id_kind, turn_rollup


def _row(run_id: str, *, node: str = "plan", trace_id: str = "t1"):
    return SimpleNamespace(
        run_id=run_id,
        trace_id=trace_id,
        task_id="task-1",
        node=node,
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        cached_tokens=0,
        cache_write_tokens=0,
        reasoning_tokens=0,
        latency_ms=1,
    )


def test_four_shapes_are_named_per_the_measured_ledger() -> None:
    assert run_id_kind("") == "unrecorded"
    assert run_id_kind(None) == "unrecorded"
    assert run_id_kind("team-0162c5af-87646534") == "team"
    assert run_id_kind("task:41286168-6c0b-4ded-acbd-33c28d5a6a9d:attempt:1") == "attempt"
    assert run_id_kind("41286168-6c0b-4ded-acbd-33c28d5a6a9d") == "run"
    # 不认识的非空串仍是一轮：界面不能把它念成"没记"
    assert run_id_kind("run-alpha") == "run"


def test_attempt_shape_requires_the_full_suffix() -> None:
    assert run_id_kind("task:abc:attempt:2") == "attempt"
    assert run_id_kind("task:abc:attempt") == "run"
    assert run_id_kind("task:abc:attempt:x") == "run"
    assert run_id_kind("x-task:abc:attempt:2") == "run"


def test_turn_rollup_carries_the_kind_and_still_drops_empty() -> None:
    rows = [
        _row("team-1-2"),
        _row("task:abc:attempt:1"),
        _row("run-alpha"),
        _row(""),
    ]
    turns = turn_rollup(rows)
    assert [(item["run_id"], item["kind"]) for item in turns] == [
        ("run-alpha", "run"),
        ("task:abc:attempt:1", "attempt"),
        ("team-1-2", "team"),
    ]
    assert sum(item["total_tokens"] for item in turns) == 45
