"""v483：人工闸门可以「打回到指定步骤」（移植自 LoopForge 的 human gate）。

语义：驳回时带上计划步骤 id，该步及其之后全部重开成 pending，并把「从这一步重做」
写进下一次模型调用。普通驳回（不带 id）行为不变——只否掉这次调用，计划不动。

这里锁三件事：接口能收这个字段、路由把它送进图里、图里有对应的重开与事件。
打回的纯函数语义在 `test_plan_model.py::test_rewind_*` 里。
"""

from __future__ import annotations

from pathlib import Path

from blank_api.schemas.task import TaskApprovalIn

_SRC = Path(__file__).resolve().parents[1] / "src" / "blank_api"


def test_approval_accepts_a_send_back_step():
    payload = TaskApprovalIn(approve=False, send_back_step_id="step-2")
    assert payload.approve is False
    assert payload.send_back_step_id == "step-2"


def test_send_back_is_optional_and_defaults_to_plain_reject():
    payload = TaskApprovalIn(approve=False)
    assert payload.send_back_step_id == ""
    # 老客户端不会带这个字段，反序列化不能因此报错。
    assert TaskApprovalIn.model_validate({"approve": True}).send_back_step_id == ""


def test_route_forwards_the_step_id_into_the_resume_decision():
    """路由必须把它交给图：只在 schema 上收字段，图里永远看不到。"""
    source = (_SRC / "api" / "routes" / "tasks.py").read_text(encoding="utf-8")
    assert 'payload.send_back_step_id' in source
    assert '"step_id": payload.send_back_step_id' in source


def test_graph_captures_the_request_and_rewinds_the_plan():
    """图层两件事都要在：审批门记请求、执行节点重开计划并留事件。"""
    source = (_SRC / "services" / "agent" / "graph.py").read_text(encoding="utf-8")
    assert 'session.rewind_request = step_id' in source, "审批门没有记下打回请求"
    assert "rewind_to_step(" in source, "执行节点没有调用重开逻辑"
    assert '"plan.rewound"' in source, "打回没有留事件，事后查不到"
    # 打回必须给模型一句可据以行动的指令，否则模型只会原样重试。
    assert "plan.rewound" in source and "从这一步重做" in source
