"""标准 → Skill：把"他做事的方式"沉淀成可加载的程序。

判据分两类：
1. **产出是否合法**——frontmatter 必须能被我们自己的加载器解析（冒号/引号踩过）；
2. **接线是否真的通**——收尾有没有把提案落库（字段建了没人读是这个项目的常见病）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from blank_api.db import get_session_factory  # noqa: E402
from blank_api.services.agent import skillsmith  # noqa: E402
from blank_api.services.agent.persona import PersonaStore  # noqa: E402
from blank_api.services.skills_defs import PROJECT_SKILLS_DIR, discover_skills  # noqa: E402
from tests.test_guardian_trail_v51 import seeded  # noqa: E402, F401 - 建表夹具

_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _live(tmp_path, phrases=("做事要严谨",), times=2):
    store = PersonaStore(tmp_path / "persona")
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    for phrase in phrases:
        for index in range(times):
            store.learn_from_text(str(ws), phrase, task_id=f"{phrase}{index}", scope_ref=str(ws))
    return store, ws


# ── 产出合法 ──────────────────────────────────────────────────────────────
def test_the_generated_frontmatter_parses_with_our_own_loader(tmp_path):
    """description 里常带中文冒号，YAML 会把 `: ` 当键值分隔符——
    整块 frontmatter 解析失败，技能被加载成"没有描述"，而且不报错。"""
    store, ws = _live(tmp_path)
    drafts = skillsmith.drafts_for(store, str(ws))
    assert drafts

    root = Path(tmp_path) / "proj"
    empty = Path(tmp_path) / "empty"
    for item in drafts:
        target = root / PROJECT_SKILLS_DIR / item["name"] / "SKILL.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(item["skill_md"], encoding="utf-8")

    found = discover_skills(
        root, env={"BLANK_USER_SKILLS_DIR": str(empty), "BLANK_BUILTIN_SKILLS_DIR": str(empty)}
    )

    assert set(found.entries) == {item["name"] for item in drafts}
    for skill in found.entries.values():
        assert skill.description, "描述被 YAML 吃掉了"
        assert "严谨" in skill.description


def test_skill_names_follow_the_required_shape(tmp_path):
    store, ws = _live(tmp_path, ("做事要严谨", "交付要简洁"))
    for item in skillsmith.drafts_for(store, str(ws)):
        assert _NAME.match(item["name"]), item["name"]


def test_the_description_fits_the_proposal_validator(tmp_path):
    """`store_proposals` 会拒掉尖括号和超长描述——先在这里挡住，
    否则提案会在收尾时静默失败。"""
    store, ws = _live(tmp_path, ("做事要严谨", "交付要简洁"))
    for item in skillsmith.drafts_for(store, str(ws)):
        assert len(item["description"]) <= 200
        assert "<" not in item["description"] and ">" not in item["description"]


def test_the_skill_carries_his_actual_words(tmp_path):
    """没有原话的标准不该出提案：没有他的话，就只剩我们自己发明的规矩。"""
    store, ws = _live(tmp_path)
    item = skillsmith.drafts_for(store, str(ws))[0]

    assert "做事要严谨" in item["skill_md"]
    assert "不是我们从行为里猜的" in item["skill_md"]


def test_no_standard_means_no_skill(tmp_path):
    store = PersonaStore(tmp_path / "persona")
    assert skillsmith.drafts_for(store, str(tmp_path)) == []


def test_the_body_says_how_it_is_checked(tmp_path):
    """写了判据却没人核，就是又一次"字段建了没人读"。"""
    store, ws = _live(tmp_path)
    body = skillsmith.drafts_for(store, str(ws))[0]["skill_md"]

    assert "standard.conformance" in body
    assert "change_is_verified" in body


# ── 接线 ──────────────────────────────────────────────────────────────────
class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def add(self, kind, node, payload):
        self.events.append((kind, payload))


@pytest.mark.asyncio
async def test_staging_is_idempotent(seeded, tmp_path, monkeypatch):  # noqa: F811
    """同一个标准不该每跑一个任务就提一次。"""
    monkeypatch.setenv("BLANK_PERSONA_DIR", str(tmp_path / "persona"))
    store, ws = _live(tmp_path)
    drafts = skillsmith.drafts_for(store, str(ws))

    first = await skillsmith.stage(get_session_factory(), drafts)
    second = await skillsmith.stage(get_session_factory(), drafts)

    assert len(first) == len(drafts)
    assert second == [], "第二次不该再提一遍"


@pytest.mark.asyncio
async def test_the_runtime_actually_stages_a_proposal(seeded, tmp_path, monkeypatch):  # noqa: F811
    """真走一遍收尾，断言提案被提交——不是只测那个纯函数。"""
    monkeypatch.setenv("BLANK_PERSONA_DIR", str(tmp_path / "persona"))
    monkeypatch.setenv("BLANK_EXPERIENCE_DIR", str(tmp_path / "experience"))

    from blank_api.services.agent.runtime import AgentRuntime

    store, ws = _live(tmp_path)
    recorder = _Recorder()
    scope = SimpleNamespace(
        task_id="task-1",
        store=get_session_factory(),
        session=SimpleNamespace(goal="改个页面", context=SimpleNamespace(root=ws)),
        recorder=recorder,
    )
    result = {
        "tool_results": [
            {
                "ok": True,
                "tool": "write_file",
                "data": {"path": "a.py", "change_type": "created", "diff": "+x"},
            }
        ],
        "validation": {"exit_code": 0},
        "summary": "改了 a.py，`pytest` 退出码 0。",
    }

    await AgentRuntime.__dict__["_consolidate"](None, scope, result, title="t")

    kinds = [kind for kind, _ in recorder.events]
    assert "skill.crystallized" in kinds, kinds
    payload = next(payload for kind, payload in recorder.events if kind == "skill.crystallized")
    assert "work-rigorously" in payload["names"]
