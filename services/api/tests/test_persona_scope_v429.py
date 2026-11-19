"""学人的三处接线：跨项目作用域、追问、记忆分流、强制开关。

这些都属于"字段建了没人读"的高发区——今天已经栽过四次，所以每条都真的走一遍。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from blank_api.services.agent.persona import PersonaStore  # noqa: E402


def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("BLANK_PERSONA_DIR", str(tmp_path / "persona"))
    return PersonaStore(tmp_path / "persona")


# ── 跨项目 ────────────────────────────────────────────────────────────────
def test_a_global_rule_reaches_every_project(tmp_path, monkeypatch):
    """跨项目偏好（"他从不用 tab 缩进"）在每个仓库都该生效。复制到各项目库
    会变成 N 份副本，改一处要改 N 处，漏改的后果是"某个仓库里突然不像他了"。"""
    store = _store(tmp_path, monkeypatch)
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()

    store.record_assertion(str(a), "做数据库改动必须先问我", scope="global")

    assert "做数据库改动必须先问我" in store.persona_block(str(a))
    assert "做数据库改动必须先问我" in store.persona_block(str(b))


def test_a_project_rule_does_not_leak_to_another_project(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()

    store.record_assertion(str(a), "做数据库改动必须先问我")

    assert "做数据库改动必须先问我" in store.persona_block(str(a))
    assert "做数据库改动必须先问我" not in store.persona_block(str(b))


def test_a_global_rule_is_not_a_copy_in_the_project_store(tmp_path, monkeypatch):
    """全局条目只该有一份。第一版先写项目库再改 label，会留下副本。"""
    store = _store(tmp_path, monkeypatch)
    a = tmp_path / "a"
    a.mkdir()

    store.record_assertion(str(a), "做数据库改动必须先问我", scope="global")

    assert store.load(str(a)) == []
    assert len(store.load_global()) == 1


# ── 追问 ──────────────────────────────────────────────────────────────────
def test_a_follow_up_correction_is_learnable(tmp_path, monkeypatch):
    """第一轮的任务描述写的是"这次做什么"，不能学；追问不一样，它天然是纠正。"""
    store = _store(tmp_path, monkeypatch)
    a = tmp_path / "a"
    a.mkdir()

    learned = store.learn_from_text(
        str(a), "下次先跑单个测试再跑全量，不要一上来就跑全量", task_id="t2", source="followup"
    )

    assert learned
    assert any("先跑单个测试" in item.rule for _action, item in learned)


def test_a_task_spec_is_still_not_a_rule(tmp_path, monkeypatch):
    """追问里也可能有具体任务句（"把按钮改成蓝色"），那不该被学成偏好。"""
    store = _store(tmp_path, monkeypatch)
    a = tmp_path / "a"
    a.mkdir()

    assert store.learn_from_text(str(a), "把按钮改成蓝色", task_id="t2", source="followup") == []


# ── memory_write 分流 ─────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_user_memory_becomes_a_pending_requirement(tmp_path, monkeypatch):
    """`user` / `feedback` 两类是"关于他"的，散文形态没法逐条核对。
    但只记**候选**：模型不能替用户把一条要求变成"他一贯如此"。"""
    from blank_api.services.tools.memory_tools import _memory_write

    store = _store(tmp_path, monkeypatch)
    context = SimpleNamespace(root=tmp_path, task_id="t1", memory=None)

    outcome = await _memory_write(
        context, {"type": "user", "content": "他要求改完必须跑回归，不要只看类型检查"}
    )

    assert outcome.status == "ok"
    entries = store.load(str(tmp_path))
    assert entries, "关于他的那条应该进使用者模型"
    assert all(item.status == "candidate" for item in entries), "模型不能自己把它置为生效"
    assert "待确认" in str(outcome.output if hasattr(outcome, "output") else outcome)


# ── 强制开关 ──────────────────────────────────────────────────────────────
def test_standard_enforcement_is_off_by_default():
    """默认不拦：条款是从他的话里展开的，理解可能偏。一上来就拦，
    会把"理解偏了"变成"任务被判不合格"。"""
    from blank_api.services.agent.runtime import _standard_enforced

    assert _standard_enforced() is False


def test_the_unmet_note_says_how_to_turn_it_off(monkeypatch):
    """拦的时候必须给出退出方式，否则用户只能删配置。"""
    from blank_api.services import settings as settings_module
    from blank_api.services.agent import runtime as runtime_module

    monkeypatch.setattr(
        settings_module.SettingsStore, "read", lambda self: {"features": {"standard_enforce": True}}
    )

    assert runtime_module._standard_enforced() is True
    note = runtime_module.STANDARD_UNMET_NOTE.format(items="改完必须验证")
    assert "改完必须验证" in note
    assert "standard_enforce" in note
