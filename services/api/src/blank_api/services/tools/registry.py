"""Tool registry: which tools the model can currently see.

Both Codex and Claude Code stopped sending every tool on every turn. With 40+
tools the schema list itself costs thousands of tokens per call and measurably
degrades tool choice, so they keep a small always-on core and let the model pull
the rest in by describing what it wants (Codex: ``tool_search``; Claude:
deferred tool loading).

The policy lives here, in one table, not spread across fifteen tool modules:

* ``CORE_TOOLS`` — what the agent needs to do anything at all.
* ``DEFERRED_TOOLS`` — capabilities that only matter some of the time (version
  control, the network, memory search, sub-agents). Visible after a search hit.

* ``PROFILE_LEAN`` — a shorter menu for models the router judged weak. Same
  principle as the core/deferred split, one level down: fewer near-duplicate
  entries means fewer wrong picks, and the schema payload shrinks on *every*
  turn.

Three invariants:

* **Deferral is presentation only.** A deferred tool is still registered, still
  risk-scored and still goes through the same approval path. Hiding a tool must
  never become a way to bypass policy.
* **Activation is per session.** Loading ``web_search`` in one task does not
  leak into another task's tool list.
* **Trimming never takes capability away.** Anything the lean profile hides is
  one ``tool_search`` away, and what was activated stays activated across a
  profile switch — a model that just pulled ``run_tests`` in must not lose it
  on the next step.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from blank_api.services.tools.base import ToolDefinition

# 核心工具：没有它们连"看、改、跑、问、记计划"都做不了
CORE_TOOLS = frozenset(
    {
        "tool_search",
        "update_plan",
        "request_user_input",
        "read_file",
        "list_files",
        "search_code",
        "apply_patch",
        "run_shell",
        "run_tests",
        "write_notes",
        "read_notes",
    }
)

# 按需加载：用得着才出现，省掉每轮都重复的 schema 成本
DEFERRED_TOOLS = frozenset(
    {
        "git",
        "web_search",
        "web_fetch",
        "memory_search",
        "spawn_agent",
        "spawn_agents",
    }
)

# 搜索别名：模型说"跑测试""提交""查资料"时也要命中，不能只认英文名
ALIASES: dict[str, tuple[str, ...]] = {
    "git": ("git", "提交", "版本", "分支", "commit", "diff", "log", "stash"),
    "run_tests": ("测试", "用例", "test", "pytest", "验证"),
    "web_search": ("联网", "上网", "搜索", "查资料", "资料", "网页", "search", "web", "internet"),
    "web_fetch": ("抓取", "上网", "网页", "链接", "抓", "fetch", "url", "http"),
    "memory_search": ("记忆", "历史", "上次", "以前", "memory", "recall"),
    "write_notes": ("笔记", "交接", "记录", "notes"),
    "read_notes": ("笔记", "交接", "notes"),
    "spawn_agent": ("子代理", "子任务", "委派", "并行", "subagent", "delegate"),
    "spawn_agents": ("子代理", "并行", "多路", "同时", "fanout", "parallel", "subagent"),
    "apply_patch": ("修改", "编辑", "patch", "写文件", "替换"),
    "run_shell": ("命令", "shell", "执行", "终端", "bash"),
    "search_code": ("搜索代码", "查找", "grep", "search"),
    "request_user_input": ("提问", "询问", "确认", "ask"),
    "update_plan": ("计划", "步骤", "plan", "todo"),
}

MAX_SEARCH_RESULTS = 6
# 低于这个分数说明只是描述里蹭到了同一个词（最多 +18），不该为它增加每一轮的 schema 成本。
# 别名命中（+25 起）才算真的想要这个能力。
MIN_SCORE = 24
# 检索工具自己不参与检索：它本来就在可见列表里，把它当结果返回纯属噪音
EXCLUDE_FROM_SEARCH = frozenset({"tool_search"})

# 同一轮里可以真正并发执行的工具：无副作用、不写盘、不改会话状态。
#
# 为什么单独一张表，而不是复用 permissions.READ_ONLY_TOOLS：那张表的语义是
# "计划模式允许用什么"，里面含 update_plan（改计划）、tool_search（改可见工具集）、
# request_user_input（等用户）—— 这三个都不是只读，并发跑会互相踩状态。
#
# git 也刻意排除：它的只读子命令确实只读，但同一张表里也挂着 commit / push，
# 按工具名放行等于把写操作混进了并行批次。
PARALLEL_SAFE_TOOLS = frozenset(
    {
        "read_file",
        "list_files",
        "search_code",
        "read_notes",
        "memory_search",
        "web_search",
        "web_fetch",
    }
)

# ---- 档位视图 ---------------------------------------------------------------
#
# 路由已经知道「这一步大概多难、该花多少」，工具表却一直是同一份。Codex 三代
# 协议之间的差异本质上就是这件事：给弱模型的菜单越短，它选错的概率越低；而省下
# 的 schema token 是每一轮都在付的，不是只付一次。
#
# 削减规则不是「随手少给几个」，而是「每类能力留一个代表」：
#
#   看    read_file / list_files / search_code
#   改    apply_patch / run_shell
#   规划  update_plan / request_user_input
#   出口  tool_search
#
# 被拿掉的都是「已保留工具的专门化」或「长会话便利」：
#   run_tests、git  —— run_shell 的专门化。模型说"跑测试""提交"时，
#                      tool_search 的别名表会直接把它们拉回来。
#   write_notes / read_notes —— 服务于跨窗口交接；撑不到交接那一步的模型，
#                      写出来的笔记也没有信息量。
#
# 新加的工具默认只在 full 档出现，要进 lean 必须显式登记。这是有意的：lean 是
# 一份白名单，"忘了登记"应该表现为「弱模型看不到」，而不是菜单被悄悄扩写。
PROFILE_FULL = "full"
PROFILE_LEAN = "lean"
PROFILES = (PROFILE_FULL, PROFILE_LEAN)

LEAN_TOOLS = frozenset(
    {
        "tool_search",
        "update_plan",
        "request_user_input",
        "read_file",
        "list_files",
        "search_code",
        "apply_patch",
        "run_shell",
    }
)

_TOKEN_SPLIT = re.compile(r"[^0-9A-Za-z_\u4e00-\u9fff]+")


def _terms(text: str) -> list[str]:
    return [item.lower() for item in _TOKEN_SPLIT.split(text or "") if item]


class ToolRegistry:
    """Session-scoped view over the full tool set."""

    def __init__(
        self,
        tools: Sequence[ToolDefinition],
        *,
        core: Iterable[str] | None = None,
        activate_all: bool = False,
    ) -> None:
        self._tools: dict[str, ToolDefinition] = {tool.name: tool for tool in tools}
        self._order: list[str] = [tool.name for tool in tools]
        names = set(self._tools)
        if activate_all:
            self._core: set[str] = set(names)
        else:
            requested = set(core) if core is not None else set(CORE_TOOLS)
            # 核心集合里可能有本次没有注册的名字（例如子代理只拿到一部分工具）
            self._core = {name for name in requested if name in names}
            # 未在 DEFERRED_TOOLS 里声明的工具默认常驻，避免"新增工具却忘了登记"变成隐形 bug
            self._core |= {name for name in names if name not in DEFERRED_TOOLS}
        # 切档时要回到的基准。没有它，从 lean 切回 full 就再也找不回原本的核心集合。
        self._base_core: set[str] = set(self._core)
        # 子代理拿到的本来就是一小撮白名单，再按档位裁一层只是重复劳动，
        # 而且会把它和主会话的工具视图搞成两个互相矛盾的版本。
        self._fixed = activate_all
        self.profile = PROFILE_FULL
        self._active: set[str] = set()

    # -- 档位 ---------------------------------------------------------------
    def set_profile(self, profile: str) -> bool:
        """切换工具档位视图，返回「可见集合是否真的变了」。

        只动下发列表，不动 ``_active``：已经用 tool_search 拉进来的工具切档后
        仍然可见——否则弱模型刚把能力拿回来，下一步又被夺走，表现就像工具丢失。

        返回布尔值是因为调用方要拿它决定发不发事件：每一步都发一条
        ``tools.scoped`` 只会把活动流刷满，而真正有信息量的只有"变了"的那一次。
        """
        if profile not in PROFILES:
            raise ValueError(f"未知的工具档位：{profile}")
        if self._fixed or profile == self.profile:
            return False
        before = set(self._core)
        self.profile = profile
        if profile == PROFILE_LEAN:
            self._core = {name for name in self._base_core if name in LEAN_TOOLS}
        else:
            self._core = set(self._base_core)
        return self._core != before

    # -- 查询 ---------------------------------------------------------------
    def all(self) -> list[ToolDefinition]:
        return [self._tools[name] for name in self._order]

    def visible(self) -> list[ToolDefinition]:
        return [self._tools[name] for name in self._order if name in self._core or name in self._active]

    def visible_names(self) -> list[str]:
        return [tool.name for tool in self.visible()]

    def deferred_names(self) -> list[str]:
        return [name for name in self._order if name not in self._core and name not in self._active]

    def is_active(self, name: str) -> bool:
        return name in self._core or name in self._active

    def activate(self, names: Iterable[str]) -> list[str]:
        """把工具加进本次会话的可见列表，返回真正新增的那些。"""
        added: list[str] = []
        for name in names:
            if name in self._tools and name not in self._core and name not in self._active:
                self._active.add(name)
                added.append(name)
        return added

    # -- 检索 ---------------------------------------------------------------
    def _score(self, tool: ToolDefinition, terms: list[str], raw: str) -> int:
        name = tool.name.lower()
        description = (tool.description or "").lower()
        aliases = [item.lower() for item in ALIASES.get(tool.name, ())]
        score = 0
        for term in terms:
            if term == name:
                score += 100
            elif term in name:
                score += 40
            if any(term == alias for alias in aliases):
                score += 60
            elif any(term in alias for alias in aliases):
                score += 25
            elif any(alias in term for alias in aliases if len(alias) >= 2):
                # 中文没有词边界："查资料" 里包含 "资料"，"跑测试" 里包含 "测试"
                score += 40
            if term in description:
                score += 8
        # 整串命中（"run tests"）额外加权
        compact = raw.strip().lower()
        if compact and compact in name:
            score += 30
        if compact and compact in description:
            score += 10
        return score

    def search(self, query: str, limit: int = MAX_SEARCH_RESULTS) -> tuple[list[ToolDefinition], list[str]]:
        """Return matching tools and activate them.

        No match on a non-empty query returns an empty list on purpose: the model
        should be told "nothing matched" rather than handed a random catalog and
        left to guess.

        Results far behind the best hit are dropped: a tool that only shares one
        character with the query is noise, and adding it to the tool list costs
        every subsequent turn.
        """
        raw = (query or "").strip()
        bound = max(1, min(MAX_SEARCH_RESULTS, int(limit or MAX_SEARCH_RESULTS)))
        if not raw:
            # 空查询 = "都有什么能力可加载"，只列尚未加载的
            pending = [self._tools[name] for name in self.deferred_names()]
            return pending[:bound], self.activate(tool.name for tool in pending[:bound])
        terms = _terms(raw)
        scored: list[tuple[int, int, ToolDefinition]] = []
        for index, name in enumerate(self._order):
            if name in EXCLUDE_FROM_SEARCH:
                continue
            tool = self._tools[name]
            score = self._score(tool, terms, raw)
            if score > 0:
                scored.append((score, -index, tool))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        if not scored:
            return [], []
        threshold = max(MIN_SCORE, int(scored[0][0] * 0.3))
        matched = [tool for score, _, tool in scored[:bound] if score >= threshold]
        added = self.activate(tool.name for tool in matched)
        return matched, added

    def as_dict(self) -> dict:
        return {
            "profile": self.profile,
            "visible": self.visible_names(),
            "deferred": self.deferred_names(),
            "active": sorted(self._active),
            "total": len(self._tools),
        }


def describe(tool: ToolDefinition) -> dict:
    return {
        "name": tool.name,
        "description": tool.description,
        "risk_level": tool.risk_level,
        "input_schema": tool.input_schema,
    }
