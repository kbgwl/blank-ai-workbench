"""v483：人工闸门可以「打回到指定步骤」（移植自 LoopForge 的 human gate）。

语义：驳回时带上计划步骤 id，该步及其之后全部重开成 pending，并把「从这一步重做」
写进下一次模型调用。普通驳回（不带 id）行为不变——只否掉这次调用，计划不动。

2026-11-21 复评改判据：原来这里有两条只读源码文本 assert 子串的判据
（``'session.rewind_request' in source``、``'"plan.rewound"' in source``）。
字符串断言的问题不是不优雅，是守不住行为——把参数丢掉、把事件名留在注释里，
断言依旧绿，而那正是这一版刚修完的丢参缺陷的形状。现在换成跑真实结算函数与
真实记账函数的行为判据；打回的纯函数语义在 ``test_plan_model.py::test_rewind_*``。
"""

from __future__ import annotations

from blank_api.schemas.task import TaskApprovalIn


def test_approval_accepts_a_send_back_step():
    payload = TaskApprovalIn(approve=False, send_back_step_id="step-2")
    assert payload.approve is False
    assert payload.send_back_step_id == "step-2"


def test_send_back_is_optional_and_defaults_to_plain_reject():
    payload = TaskApprovalIn(approve=False)
    assert payload.send_back_step_id == ""
    # 老客户端不会带这个字段，反序列化不能因此报错。
    assert TaskApprovalIn.model_validate({"approve": True}).send_back_step_id == ""


# ── 结算：对得上就重开，对不上就如实报没退 ────────────────────────────────


def test_settlement_reports_a_rewind_for_a_real_step():
    """对得上：返回 rewound 形状，带重开后的步骤、revision 与可据以行动的指令。"""
    from blank_api.services.agent.graph import rewind_gate_settlement

    state = {
        "plan_steps": [
            {"id": "s1", "step": "调研", "status": "completed"},
            {"id": "s2", "step": "实现", "status": "completed"},
        ],
        "plan_manifest": {"revision": 1},
    }
    got = rewind_gate_settlement(state, "s2")
    assert got["kind"] == "rewound"
    assert got["step_id"] == "s2"
    assert [s["status"] for s in got["steps"]] == ["completed", "pending"]
    assert got["manifest"]["revision"] == 2
    assert "第 2 步" in got["directive"] and "重做" in got["directive"]


def test_settlement_reports_a_miss_instead_of_silently_doing_nothing():
    """对不上：必须报 missed 并说明「没重开」，不能什么都不发生。

    这一档是必然会遇到的：前端 ``parsePlanSteps`` 会把没有 id 的老计划步骤兜底成
    ``legacy-N`` / ``idx-N``，这批 id 后端永远匹配不上。以前它返回空 dict，
    调用点 ``if patch:`` 直接跳过——计划没动、事件没写、模型也没被告诉。
    """
    from blank_api.services.agent.graph import rewind_gate_settlement

    state = {
        "plan_steps": [
            {"id": "s1", "step": "调研", "status": "completed"},
            {"id": "s2", "step": "实现", "status": "completed"},
        ]
    }
    got = rewind_gate_settlement(state, "legacy-0")
    assert got["kind"] == "missed"
    assert got["step_id"] == "legacy-0"
    # 已知 id 是事实来源：只列真的在计划里的，不回传我们自己编的兜底值。
    assert got["known"] == ["s1", "s2"]
    assert "没有重开" in got["directive"]
    # 事件负载要能回答"为什么没退"：请求的 id + 当时的计划 id。
    assert got["payload"] == {"step_id": "legacy-0", "known_step_ids": ["s1", "s2"]}


def test_settlement_treats_an_empty_step_id_as_nothing_to_rewind():
    """空 id 不算"打回失败"：调用点只在 rewind_request 非空时结算，
    真漏到这里也不能编出"退回某一步"的说法。"""
    from blank_api.services.agent.graph import rewind_gate_settlement

    got = rewind_gate_settlement(
        {"plan_steps": [{"id": "s1", "step": "调研", "status": "completed"}]}, ""
    )
    assert got["kind"] == "missed"
    assert got["step_id"] == ""
    assert got["payload"]["known_step_ids"] == ["s1"]


# ── 记账：两种结果都要同时进事件流与 hook 上下文 ──────────────────────────


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    async def add(self, event_type, node="", payload=None):
        self.events.append((event_type, node, dict(payload or {})))
        return len(self.events)


class _Session:
    def __init__(self) -> None:
        self.hook_context: list[dict] = []


async def test_records_a_rewind_as_an_event_and_a_context_line():
    """对得上：只落 plan.rewound，不落 missed——两条路不能同时出现。"""
    from blank_api.services.agent.graph import rewind_gate_settlement, settlement_records

    recorder, session = _Recorder(), _Session()
    state = {
        "plan_steps": [
            {"id": "s1", "step": "调研", "status": "completed"},
            {"id": "s2", "step": "实现", "status": "completed"},
        ],
        "plan_manifest": {"revision": 3},
    }
    applied = await settlement_records(
        rewind_gate_settlement(state, "s2"),
        session=session,
        recorder=recorder,
        node="execute_action",
    )

    assert [item[0] for item in recorder.events] == ["plan.rewound"]
    assert recorder.events[0][2]["step_id"] == "s2"
    assert recorder.events[0][2]["index"] == 1
    assert session.hook_context[0]["event"] == "plan.rewound"
    # 返回值就是执行节点要写回状态的那一段：空的话计划等于没重开。
    assert [s["status"] for s in applied["steps"]] == ["completed", "pending"]
    assert applied["manifest"]["revision"] == 4


async def test_records_a_miss_as_an_event_and_tells_the_model():
    """对不上：事件与 hook 上下文都要落，且返回值必须是空（计划不动）。

    只落事件的话模型会以为计划已经重开，于是跳过前面步骤直接往下做；
    只落上下文则界面上永远解释不清"我明明点了第 3 步"。
    """
    from blank_api.services.agent.graph import rewind_gate_settlement, settlement_records

    recorder, session = _Recorder(), _Session()
    state = {"plan_steps": [{"id": "s1", "step": "调研", "status": "completed"}]}
    applied = await settlement_records(
        rewind_gate_settlement(state, "idx-4"),
        session=session,
        recorder=recorder,
        node="execute_action",
    )

    assert [item[0] for item in recorder.events] == ["plan.rewind_missed"]
    assert recorder.events[0][2] == {"step_id": "idx-4", "known_step_ids": ["s1"]}
    assert session.hook_context[0]["event"] == "plan.rewind_missed"
    assert "没有重开" in session.hook_context[0]["text"]
    # 计划不动：这条返回值会被执行节点当作 patch 用。
    assert applied == {}


# ── 行为判据：打回到底有没有重开计划 ──────────────────────────────────────
# 之前只有源码字符串断言，抓不到「算出来了但被后面的初始化覆盖」。


def test_rewind_produces_a_state_patch_that_reopens_the_steps():
    from blank_api.services.agent.graph import rewind_plan_for_gate

    state = {
        "plan_steps": [
            {"id": "s1", "step": "调研", "status": "completed", "evidence": "读了 3 个文件"},
            {"id": "s2", "step": "实现", "status": "completed", "evidence": "改了 a.py"},
            {"id": "s3", "step": "测试", "status": "completed", "evidence": "跑了 pytest"},
        ],
        "plan_manifest": {"plan_id": "p1", "revision": 2},
    }
    patch = rewind_plan_for_gate(state, "s2")

    assert patch["index"] == 1
    assert [s["status"] for s in patch["steps"]] == ["completed", "pending", "pending"]
    assert patch["steps"][1]["evidence"] == ""
    assert patch["manifest"]["revision"] == 3
    assert "第 2 步" in patch["directive"]


def test_rewind_patch_is_empty_when_the_step_is_unknown():
    from blank_api.services.agent.graph import rewind_plan_for_gate

    state = {"plan_steps": [{"id": "s1", "step": "调研", "status": "completed"}]}
    assert rewind_plan_for_gate(state, "nope") == {}
    assert rewind_plan_for_gate(state, "") == {}
