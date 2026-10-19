"""Every capability switch in one table.

Codex ships ~140 of these behind a ``Feature`` enum. We are not copying the count
— we are copying the *shape*, because the shape is what prevents rot:

* **One registry.** "Is X on?" is answered here, not by an ``if`` scattered in
  whichever module happened to need it. A switch that only exists inside a
  function is a switch nobody can audit or turn off.
* **A stage per flag.** ``stable`` is on by default, ``beta`` is off until proven,
  ``deprecated`` stays off and is on its way out. Upgrading a build must not
  silently change what the agent does, so the default is part of the flag, not
  of the call site.
* **Resolution is ordered and reported.** Env var beats the settings file beats
  the legacy location beats the default, and ``describe()`` says which layer
  won. "It's off but I turned it on" is a support ticket; "on, overridden by
  BLANK_FEATURE_TOOL_SEARCH" is an answer.
* **Unknown names raise.** A typo'd flag that silently does nothing is worse
  than a crash at startup.

Adding a flag: append a ``FeatureFlag`` here, wire exactly one call site to
``is_enabled(name)``, and add the name to the wave doc. Do not add one that no
line of code reads — a registry full of dead switches trains people to ignore
the whole page.

Precedence, highest first:

1. ``BLANK_FEATURE_<NAME>`` environment variable
2. ``features.<name>`` in ``data/settings.json`` (the settings page writes here)
3. ``legacy`` path, for switches that predate this registry
4. ``FeatureFlag.default``

Env wins over the file on purpose: it is the escape hatch an operator reaches
for when a build misbehaves on a customer machine, and it should not require
editing their saved preferences to take effect.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Iterable

from blank_api.services.settings import SettingsStore, flag_value

STABLE = "stable"
BETA = "beta"
DEPRECATED = "deprecated"
STAGES = (STABLE, BETA, DEPRECATED)

# 生效来源，按优先级排列，describe() 原样报给前端
SRC_ENV = "env"
SRC_SETTINGS = "settings"
SRC_LEGACY = "legacy"
SRC_DEFAULT = "default"


@dataclass(frozen=True)
class FeatureFlag:
    """A single switch. ``legacy`` is the path it used to live at, if any."""

    name: str
    stage: str
    default: bool
    summary: str
    legacy: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "stage": self.stage,
            "default": self.default,
            "summary": self.summary,
            "legacy": list(self.legacy),
        }


_REGISTRY: tuple[FeatureFlag, ...] = (
    FeatureFlag(
        name="tool_search",
        stage=STABLE,
        default=True,
        summary="工具按需检索：默认只下发核心工具，其余靠 tool_search 拉进来。关掉后每轮全量下发。",
        # 老字段，设置页历史版本就在写这里
        legacy=("tools", "search"),
    ),
    FeatureFlag(
        name="handoff_notes",
        stage=STABLE,
        default=True,
        summary="上下文压缩后的交接笔记：把已完成/未完事项重新注入，位置在对话尾部以免打断前缀缓存。",
    ),
    FeatureFlag(
        name="prompt_cache",
        stage=STABLE,
        default=True,
        summary="提示词缓存：Anthropic 打 4 个 ephemeral 断点，OpenAI 下发 prompt_cache_key，命中部分按折扣计价。",
    ),
    FeatureFlag(
        name="parallel_read_tools",
        stage=BETA,
        default=False,
        summary="只读工具并行执行：同一轮里多个无副作用的工具并发跑，写操作与审批仍严格串行。",
    ),
    FeatureFlag(
        name="model_routing",
        stage=BETA,
        default=False,
        summary=(
            "按需选模型：用纯函数给每一步打分，在本次任务已选定的供应商里挑「够用且最便宜」的那个；"
            "同节点工具失败会自动提档重试。默认关闭，关着时行为与单模型完全一致。"
        ),
    ),
    FeatureFlag(
        name="tiered_tools",
        stage=BETA,
        default=False,
        summary=(
            "工具表跟着档位走：路由判为轻量档的那一步只下发一条最小回路"
            "（看/改/规划/检索出口），其余工具靠 tool_search 拉；标准与重型仍是全量。"
            "默认关闭——关着时工具表恒为全量，与不裁剪完全一致。"
        ),
    ),
)

FEATURES: dict[str, FeatureFlag] = {flag.name: flag for flag in _REGISTRY}


class UnknownFeature(KeyError):
    """Raised for a name that is not in the registry — a typo, not a feature."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(name)


def _env(name: str) -> str:
    return f"BLANK_FEATURE_{name.upper()}"


def _as_bool(value: Any) -> bool | None:
    """三态：True / False / None（没配或配坏了，交给下一层）。

    实现放在 settings 模块里，因为写盘时也要用同一套规则解析——两处各写一遍
    迟早会分叉：一边认 "on"，另一边不认，用户就会看到"存进去了但不生效"。
    """
    return flag_value(value)


def _dig(payload: dict[str, Any], path: Iterable[str]) -> Any:
    node: Any = payload
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _settings_payload() -> dict[str, Any]:
    """读一次设置文件。异常一律当"没配过"，开关不该成为启动失败的理由。"""
    try:
        return SettingsStore().read()
    except Exception:  # noqa: BLE001 - 读盘/解析异常都不值得让 agent 起不来
        return {}


def resolve(name: str, settings: dict[str, Any] | None = None) -> tuple[bool, str]:
    """返回 (是否启用, 生效来源)。来源是给人看的，别拿它做逻辑判断。"""
    if name not in FEATURES:
        raise UnknownFeature(name)
    flag = FEATURES[name]
    payload = settings if isinstance(settings, dict) else _settings_payload()

    from_env = _as_bool(os.environ.get(_env(name)))
    if from_env is not None:
        return from_env, SRC_ENV

    features = payload.get("features")
    if isinstance(features, dict):
        from_file = _as_bool(features.get(name))
        if from_file is not None:
            return from_file, SRC_SETTINGS

    if flag.legacy:
        from_legacy = _as_bool(_dig(payload, flag.legacy))
        if from_legacy is not None:
            return from_legacy, SRC_LEGACY

    return flag.default, SRC_DEFAULT


def is_enabled(name: str, settings: dict[str, Any] | None = None) -> bool:
    """业务代码唯一该调的入口。名字写错直接抛，不给静默失效的机会。"""
    return resolve(name, settings)[0]


def describe(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """前端自省用：注册表全量 + 生效值 + 来源 + 文件里没人认领的键。"""
    payload = settings if isinstance(settings, dict) else _settings_payload()
    items: list[dict[str, Any]] = []
    for flag in _REGISTRY:
        enabled, source = resolve(flag.name, payload)
        items.append({**flag.as_dict(), "enabled": enabled, "source": source})

    configured = payload.get("features")
    known = set(FEATURES)
    unknown = sorted(k for k in (configured or {}) if k not in known) if isinstance(configured, dict) else []

    return {
        "features": items,
        # 拼错的键不会生效，但得让人看见它躺在那儿
        "unknown": unknown,
        "stages": list(STAGES),
        "sources": [SRC_ENV, SRC_SETTINGS, SRC_LEGACY, SRC_DEFAULT],
        "env_prefix": "BLANK_FEATURE_",
    }


def apply_patch(patch: dict[str, Any]) -> dict[str, Any]:
    """写设置文件的 features 段。未知名字整批拒绝，不部分生效。

    部分生效是最难排查的一种：用户以为全开了，实际只开了一半，而界面上
    没有哪个值看起来是错的。要么全写下，要么一条都不写。
    """
    cleaned: dict[str, bool] = {}
    for raw_name, raw_value in (patch or {}).items():
        name = str(raw_name).strip()
        if name not in FEATURES:
            raise UnknownFeature(name)
        value = _as_bool(raw_value)
        if value is None:
            raise ValueError(f"{name} 需要布尔值，收到 {raw_value!r}")
        cleaned[name] = value

    if cleaned:
        store = SettingsStore()
        merged = dict(store.read().get("features") or {})
        merged.update(cleaned)
        store.update({"features": merged})
    return describe()
