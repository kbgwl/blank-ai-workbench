"""工具表按模型档位裁剪（tiered_tools）。

要证明五件事，缺一件这个 beta 就不该转正：

1. 轻量档真的变短了 —— 每类能力只留一个代表，被拿掉的都是"专门化"或"便利"；
2. 变短不等于变弱 —— 被藏起来的工具一条 tool_search 就能拉回来，拉回来之后
   切档也不会再被夺走（否则模型会看到"上一步还有，这一步没了"）；
3. 切回来必须完整 —— lean → full 要恢复到裁剪前的集合，不能少一个；
4. 关掉就完全不变 —— 开关关闭或没有路由时，工具表恒为全量，且一条事件都不发；
5. 子代理不受影响 —— 它拿到的本来就是白名单，再裁一层只会制造两份矛盾的视图。
"""

from pathlib import Path

import pytest

from blank_api.db import get_session_factory
from blank_api.models.project import Project
from blank_api.models.task import AgentEvent, Task
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.graph import AgentSession, tool_profile
from blank_api.services.agent.router import HEAVY, LIGHT, STANDARD, Candidate, Router
from blank_api.services.models.base import (
    AdapterConfig,
    ModelAdapter,
    ModelResponse,
    Usage,
)
from blank_api.services.tools import build_default_tools
from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome
from blank_api.services.tools.gateway import ToolGateway
from blank_api.services.tools.registry import (
    CORE_TOOLS,
    DEFERRED_TOOLS,
    LEAN_TOOLS,
    PROFILE_FULL,
    PROFILE_LEAN,
    ToolRegistry,
)

TASK_ID = "task-tiered-tools"


def _gateway() -> ToolGateway:
    return ToolGateway(build_default_tools())


# ------------------------------------------------------------------ 档位映射


def test_only_the_light_tier_gets_a_shorter_menu():
    # 反过来的诱惑是"重型给更多"，但 full 已经是最全的一份；再"多给"只能是把
    # deferred 也全下发，schema 成本和选择难度一起抬上去，正好和精简目的相反。
    assert tool_profile(LIGHT) == PROFILE_LEAN
    assert tool_profile(STANDARD) == PROFILE_FULL
    assert tool_profile(HEAVY) == PROFILE_FULL
    # 没有路由（开关关着 / 池子里只有一个候选）时必须退回全量
    assert tool_profile("") == PROFILE_FULL


def test_lean_keeps_one_tool_per_job():
    gw = _gateway()
    gw.registry.set_profile(PROFILE_LEAN)
    names = set(gw.registry.visible_names())

    assert names == set(LEAN_TOOLS)
    # 一条完整的最小回路：看 → 改 → 规划提问 → 出口
    for needed in (
        "read_file",
        "list_files",
        "search_code",
        "apply_patch",
        "run_shell",
        "update_plan",
        "request_user_input",
        "tool_search",
    ):
        assert needed in names
    # 被拿掉的是「已保留工具的专门化」和「长会话便利」
    assert "run_tests" not in names
    assert "git" not in names
    assert "write_notes" not in names and "read_notes" not in names
    # 裁剪只发生在核心集合里：deferred 本来就是藏着的
    assert names <= CORE_TOOLS
    assert not (names & DEFERRED_TOOLS)


def test_lean_is_a_strict_subset_not_a_different_catalog():
    gw = _gateway()
    full = set(gw.registry.visible_names())
    gw.registry.set_profile(PROFILE_LEAN)
    lean = set(gw.registry.visible_names())

    # 裁剪不能引入新工具，也不能把 deferred 顺手放出来
    assert lean < full
    assert len(lean) < len(full)


def test_switching_back_restores_the_full_menu():
    gw = _gateway()
    before = gw.registry.visible_names()

    assert gw.registry.set_profile(PROFILE_LEAN) is True
    assert gw.registry.set_profile(PROFILE_FULL) is True
    # 顺序也要一样：工具顺序进 prompt，变了就是缓存前缀失效
    assert gw.registry.visible_names() == before


def test_set_profile_reports_whether_anything_changed():
    gw = _gateway()
    # 已经是 full，再设 full 不该被当成"变了"——否则每步都会多一条事件
    assert gw.registry.set_profile(PROFILE_FULL) is False
    assert gw.registry.set_profile(PROFILE_LEAN) is True
    assert gw.registry.set_profile(PROFILE_LEAN) is False


def test_unknown_profile_is_rejected_loudly():
    gw = _gateway()
    with pytest.raises(ValueError):
        gw.registry.set_profile("tiny")


# ------------------------------------------------- 裁剪是展示，不是能力边界


def test_search_pulls_a_trimmed_tool_back():
    gw = _gateway()
    gw.registry.set_profile(PROFILE_LEAN)
    assert "run_tests" not in gw.registry.visible_names()

    matched, added = gw.registry.search("跑测试")

    assert [tool.name for tool in matched][:1] == ["run_tests"]
    assert "run_tests" in added
    assert "run_tests" in gw.registry.visible_names()


def test_activated_tool_survives_a_profile_switch():
    """弱模型刚把能力拿回来，下一步又被夺走——那是最难查的一类"工具丢失"。"""
    gw = _gateway()
    gw.registry.set_profile(PROFILE_LEAN)
    gw.registry.search("提交代码")
    assert "git" in gw.registry.visible_names()

    gw.registry.set_profile(PROFILE_FULL)
    gw.registry.set_profile(PROFILE_LEAN)

    assert "git" in gw.registry.visible_names()
    # 但没被激活的仍然藏着
    assert "run_tests" not in gw.registry.visible_names()


def test_hidden_tools_are_still_registered_and_executable():
    """藏起来只影响下发列表。真按名字调过来，照样执行、照样过审批。"""
    gw = _gateway()
    gw.registry.set_profile(PROFILE_LEAN)

    assert "run_tests" in gw.tools
    assert gw.get("run_tests") is not None


def test_subagent_registry_ignores_profiles():
    # 子代理只拿到一小撮白名单（defer=False），再按档位裁是重复劳动，
    # 而且会造出两份互相矛盾的"当前能用什么"。
    tools: list[ToolDefinition] = build_default_tools()
    registry = ToolRegistry(tools, activate_all=True)

    assert registry.set_profile(PROFILE_LEAN) is False
    assert registry.profile == PROFILE_FULL
    assert len(registry.visible_names()) == len(tools)


def test_as_dict_reports_the_current_profile():
    gw = _gateway()
    assert gw.registry.as_dict()["profile"] == PROFILE_FULL
    gw.registry.set_profile(PROFILE_LEAN)
    snapshot = gw.registry.as_dict()

    assert snapshot["profile"] == PROFILE_LEAN
    assert set(snapshot["visible"]) == set(LEAN_TOOLS)


# ------------------------------------------------------- 接进执行链路（真事件）


def _pool() -> list[Candidate]:
    return [
        Candidate("local", "本机 7B", LIGHT, 7.0),
        Candidate("cloud", "云端 671/37", STANDARD, 37.0),
        Candidate("giant", "巨型 405B", HEAVY, 405.0),
    ]


class CapturingAdapter(ModelAdapter):
    """记录每次下发的工具名，然后给个空回复（本用例不关心模型说了什么）。"""

    def __init__(self, name: str) -> None:
        super().__init__(
            AdapterConfig(
                name=name,
                kind="openai_compatible",
                base_url="",
                api_key="",
                model=name,
                supports_tools=True,
            )
        )
        self.seen: list[list[str]] = []

    async def invoke(self, messages, **kwargs):  # pragma: no cover - 不该被调到
        raise AssertionError("本用例只验证下发列表")

    async def stream(self, messages, **kwargs):  # pragma: no cover
        raise NotImplementedError

    async def bind_tools(self, tools, messages, **kwargs):
        self.seen.append([tool.name for tool in tools])
        return ModelResponse(text="好", usage=Usage(), model=self.config.name)


def _session(
    tmp_path: Path, *, router: Router | None, tiered_tools: bool = True
) -> AgentSession:
    tools = build_default_tools()
    primary = CapturingAdapter("云端 671/37")
    local = CapturingAdapter("本机 7B")
    session = AgentSession(
        adapter=primary,
        gateway=ToolGateway(tools),
        context=ToolContext(
            root=tmp_path,
            task_id=TASK_ID,
            session_factory=get_session_factory(),
            approval_policy=lambda *_: False,
        ),
        recorder=EventRecorder(get_session_factory(), TASK_ID),
        session_factory=get_session_factory(),
        provider_id="cloud",
        provider_name="云端 671/37",
        goal="工具裁剪",
        max_steps=10,
        router=router,
        routing_adapters={"local": local, "cloud": primary, "giant": CapturingAdapter("巨型 405B")},
        tiered_tools=tiered_tools,
    )
    session._probe = {  # type: ignore[attr-defined]
        "primary": primary,
        "local": local,
    }
    return session


async def _events(kind: str) -> list[dict]:
    async with get_session_factory()() as session:
        rows = (await session.execute(AgentEvent.__table__.select())).fetchall()
    return [row.payload for row in rows if row.type == kind]


@pytest.fixture
async def seeded():
    from blank_api.db import Base, get_engine

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with get_session_factory()() as session:
        session.add(Project(id="project-tools", name="工具裁剪"))
        session.add(
            Task(id=TASK_ID, project_id="project-tools", title="裁剪", goal="裁剪", max_steps=10)
        )
        await session.commit()


async def test_light_step_sends_a_short_menu(seeded, tmp_path):
    session = _session(tmp_path, router=Router(pool=tuple(_pool())))
    # 第一步、空上下文 → 轻量档 → 选中本机 7B
    await session.call_model([{"role": "user", "content": "改一行"}], "execute_action", step=1)

    local = session._probe["local"]  # type: ignore[attr-defined]
    assert local.seen, "轻量档这一步应该走本机模型"
    assert set(local.seen[-1]) == set(LEAN_TOOLS)
    assert session.gateway.registry.profile == PROFILE_LEAN

    scoped = await _events("tools.scoped")
    assert len(scoped) == 1
    assert scoped[0]["profile"] == PROFILE_LEAN
    assert scoped[0]["tier"] == LIGHT
    # 被藏起来的名字要进事件：否则"工具没了"在界面上永远无法解释
    assert "run_tests" in scoped[0]["hidden"]
    assert len(scoped[0]["visible"]) == len(LEAN_TOOLS)


async def test_standard_step_restores_the_full_menu(seeded, tmp_path):
    session = _session(tmp_path, router=Router(pool=tuple(_pool())))

    await session.call_model([{"role": "user", "content": "改一行"}], "execute_action", step=1)
    # review_result 基础分 60 → 标准档 → 云端
    await session.call_model([{"role": "user", "content": "看看改得对不对"}], "review_result", step=2)

    primary = session._probe["primary"]  # type: ignore[attr-defined]
    assert len(primary.seen[-1]) > len(LEAN_TOOLS)
    assert "run_tests" in primary.seen[-1]
    assert session.gateway.registry.profile == PROFILE_FULL

    scoped = await _events("tools.scoped")
    # 只有"真的变了"的两次：收窄一次、放开一次。每次调用都记等于噪音。
    assert [item["profile"] for item in scoped] == [PROFILE_LEAN, PROFILE_FULL]


async def test_flag_off_keeps_every_tool_and_stays_quiet(seeded, tmp_path):
    session = _session(tmp_path, router=Router(pool=tuple(_pool())), tiered_tools=False)
    await session.call_model([{"role": "user", "content": "改一行"}], "execute_action", step=1)

    local = session._probe["local"]  # type: ignore[attr-defined]
    assert local.seen, "路由本身仍然生效：关的是裁剪，不是路由"
    assert "run_tests" in local.seen[-1]
    assert session.gateway.registry.profile == PROFILE_FULL
    assert await _events("tools.scoped") == []


async def test_no_router_never_touches_the_menu(seeded, tmp_path):
    session = _session(tmp_path, router=None, tiered_tools=True)
    await session.call_model([{"role": "user", "content": "改一行"}], "execute_action", step=1)

    primary = session._probe["primary"]  # type: ignore[attr-defined]
    assert "run_tests" in primary.seen[-1]
    assert await _events("tools.scoped") == []


async def test_escalation_widens_the_menu_again(seeded, tmp_path):
    """轻量档工具跑失败 → 提档 → 下一步既换模型，也换回全量菜单。"""
    session = _session(tmp_path, router=Router(pool=tuple(_pool())))
    await session.call_model([{"role": "user", "content": "改一行"}], "execute_action", step=1)
    assert session.gateway.registry.profile == PROFILE_LEAN

    session.router.note_failure("execute_action")  # type: ignore[union-attr]
    await session.call_model([{"role": "user", "content": "再试一次"}], "execute_action", step=2)

    # 提一档 → 标准档 → 云端，并且菜单回到全量
    primary = session._probe["primary"]  # type: ignore[attr-defined]
    assert "run_tests" in primary.seen[-1]
    assert session.gateway.registry.profile == PROFILE_FULL
