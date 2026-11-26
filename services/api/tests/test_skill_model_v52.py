"""Skill 级 model 覆盖（#287–#290）：判据。

上游锚点：Skill frontmatter 的 ``model`` 让 skill 工具调用后，本轮后续步骤的主模型
改用这一份。上游 ``SkillTool`` 返回 ``{kind:"model", mainLoopModel:sr}``，调用方把它
写进会话状态，下一次 ``call_model`` 拿它覆盖适配器的配置模型。

钉的不是「字段存在」——那用 ``hasattr`` 也能过。每条判据都指向一个**会走形的具体行为**：
frontmatter 解析丢了字段、目录不携带、handler 不写状态、适配器不覆盖，对应那条就红。
改前（没接 model 时）这些用例要么 AttributeError、要么断言失败。

覆盖五块：解析 / 目录携带 / handler 写状态 / 适配器 _payload 覆盖 / 适配器方法签名。
"""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from blank_api.services.skills_defs import (
    SkillDefinition,
    _definition_from_dir,
    discover_skills,
    load_skill,
)
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.skills import SkillCatalog, skill_tools

from tests.test_skills_disk_v45 import _write_skill


# --------------------------------------------------------------------------- #
# 1. 解析
# --------------------------------------------------------------------------- #


def test_model_frontmatter_is_parsed_into_definition(tmp_path: Path) -> None:
    """``model: fast-model`` 解析进 ``SkillDefinition.model``。

    解析里丢了这个字段，skill 声明了模型也不会生效——调用方拿到的永远是空串。
    """
    skill_dir = tmp_path / "fast-deploy"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: fast-deploy\ndescription: 快速上线\nmodel: gpt-5-turbo\n---\n先跑测试再上线。\n",
        encoding="utf-8",
    )
    definition, errors, warnings = _definition_from_dir(skill_dir, "project")
    assert definition is not None
    assert definition.model == "gpt-5-turbo"
    assert not errors


def test_model_whitespace_is_stripped(tmp_path: Path) -> None:
    """``model:  "  gpt-5  "`` 首尾空白要去掉。

    不 strip，供应商收到的是带空格的模型名，要么被拒、要么匹配不到任何模型。
    """
    skill_dir = tmp_path / "padded"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        '---\nname: padded\ndescription: d\nmodel: "  gpt-5  "\n---\nbody\n',
        encoding="utf-8",
    )
    definition, _, _ = _definition_from_dir(skill_dir, "project")
    assert definition is not None
    assert definition.model == "gpt-5"


def test_model_non_string_is_dropped_with_warning(tmp_path: Path) -> None:
    """``model: 123`` 不是字符串——告警丢弃，不猜一个值填入。

    不告警直接丢，用户不知道写了什么；把 123 当模型名传出去，报错来自供应商，
    离用户的书写位置太远。
    """
    skill_dir = tmp_path / "bad-model"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: bad-model\ndescription: d\nmodel: 123\n---\nbody\n",
        encoding="utf-8",
    )
    definition, _, warnings = _definition_from_dir(skill_dir, "project")
    assert definition is not None
    assert definition.model == ""
    assert any("model" in w and "不是非空字符串" in w for w in warnings)


def test_model_empty_string_is_dropped_without_warning(tmp_path: Path) -> None:
    """``model:`` 为空串——静默丢弃，不告警。

    空串就是"没声明"，跟没写这个字段一样；对一个空值发告警是噪声。
    """
    skill_dir = tmp_path / "empty-model"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: empty-model\ndescription: d\nmodel:\n---\nbody\n",
        encoding="utf-8",
    )
    definition, _, warnings = _definition_from_dir(skill_dir, "project")
    assert definition is not None
    assert definition.model == ""
    assert not any("model" in w for w in warnings)


def test_model_absent_defaults_to_empty(tmp_path: Path) -> None:
    """不写 ``model`` 字段——定义里 model 是空串，不改变当前模型。

    默认值写错（比如默认成一个具体模型名），每个没声明 model 的 skill 都会
    悄悄切换模型，这是最隐蔽的走形。
    """
    skill_dir = tmp_path / "no-model"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: no-model\ndescription: d\n---\nbody\n",
        encoding="utf-8",
    )
    definition, _, _ = _definition_from_dir(skill_dir, "project")
    assert definition is not None
    assert definition.model == ""


def test_model_absent_from_unsupported_fields(tmp_path: Path) -> None:
    """``model`` 不在 ``UNSUPPORTED_FIELDS`` 里——它被识别并生效，不是被忽略的字段。

    如果 model 还在 UNSUPPORTED_FIELDS 里，解析时会被记进 ignored 但不生效，
    前面的解析用例全红。
    """
    from blank_api.services.skills_defs import UNSUPPORTED_FIELDS

    assert "model" not in UNSUPPORTED_FIELDS


def test_discover_skills_carries_model(tmp_path: Path) -> None:
    """``discover_skills`` 发现的 skill 带上 model。

    发现层丢了字段，目录层再正确也没用——数据在源头就没了。
    """
    _write_skill(
        tmp_path,
        "deploy",
        "---\nname: deploy\ndescription: 上线\nmodel: gpt-5\n---\n先跑全量。\n",
    )
    env = {"BLANK_USER_SKILLS_DIR": str(tmp_path.parent / "no-user")}
    discovery = discover_skills(tmp_path, env=env)
    assert "deploy" in discovery.entries
    assert discovery.entries["deploy"].model == "gpt-5"


def test_as_entry_includes_model() -> None:
    """``as_entry()`` 的字典里有 ``model`` 键——目录层靠这个键取值。

    as_entry 不写这个键，SkillCatalog 拿不到，handler 自然也写不了状态。
    """
    definition = SkillDefinition(
        name="x", description="d", instructions="body", model="gpt-5"
    )
    entry = definition.as_entry()
    assert entry["model"] == "gpt-5"


def test_as_dict_includes_model_when_declared() -> None:
    """审计视图 ``as_dict()`` 声明了 model 才带上——没声明的不出现在审计里。

    审计里出现空串的 model，读审计的人分不清"没声明"和"声明了空串"。
    """
    with_model = SkillDefinition(name="x", description="d", instructions="b", model="gpt-5")
    assert with_model.as_dict()["model"] == "gpt-5"

    without = SkillDefinition(name="y", description="d", instructions="b")
    assert "model" not in without.as_dict()


# --------------------------------------------------------------------------- #
# 2. 目录携带
# --------------------------------------------------------------------------- #


def test_catalog_entry_carries_model() -> None:
    """``SkillCatalog`` 构造时把 entry 里的 ``model`` 带进内部字典。

    构造时不取这个键，handler 从 entry 里读出来永远是空串。
    """
    catalog = SkillCatalog(
        [
            {
                "name": "fast",
                "description": "d",
                "instructions": "body",
                "model": "gpt-5-turbo",
            }
        ]
    )
    entry = catalog.get("fast")
    assert entry is not None
    assert entry["model"] == "gpt-5-turbo"


def test_catalog_entry_model_defaults_to_empty() -> None:
    """没传 ``model`` 键的 entry——目录里 model 是空串。

    默认值写成别的（比如 None），handler 的 ``str(entry.get("model") or "")``
    会把 None 读成 "None"，传给适配器就是一个假模型名。
    """
    catalog = SkillCatalog([{"name": "plain", "description": "d", "instructions": "b"}])
    entry = catalog.get("plain")
    assert entry is not None
    assert entry["model"] == ""


# --------------------------------------------------------------------------- #
# 3. handler 写状态
# --------------------------------------------------------------------------- #


def _tool_context(catalog: SkillCatalog, turn_state: dict | None = None) -> ToolContext:
    return ToolContext(
        root=Path("."),
        task_id="task-model",
        session_factory=None,
        skills=catalog,
        turn_state=turn_state if turn_state is not None else {},
    )


def _skill_tool():
    return {tool.name: tool for tool in skill_tools()}["skill"]


async def test_skill_handler_writes_skill_model_to_turn_state() -> None:
    """skill 工具调用后，``turn_state["skill_model"]`` 被写成 entry 里的 model。

    handler 不写这个键，``call_model`` 读到的永远是空串，模型覆盖不生效。
    """
    catalog = SkillCatalog(
        [
            {
                "name": "deploy",
                "description": "d",
                "instructions": "先跑全量。",
                "model": "gpt-5-turbo",
            }
        ]
    )
    context = _tool_context(catalog)
    tool = _skill_tool()
    outcome = await tool.handler(context, {"skill": "deploy"})
    assert outcome.ok
    assert context.turn_state["skill_model"] == "gpt-5-turbo"


async def test_skill_handler_does_not_write_when_model_empty() -> None:
    """没声明 model 的 skill——handler 不写 ``skill_model``，不覆盖当前模型。

    空 model 也写进去（写成空串），``call_model`` 读到空串不覆盖——这本身没错，
    但写入空串会覆盖掉上一个 skill 设置的模型，导致模型被悄悄重置。不写才是对的。
    """
    catalog = SkillCatalog(
        [{"name": "plain", "description": "d", "instructions": "body"}]
    )
    context = _tool_context(catalog)
    tool = _skill_tool()
    outcome = await tool.handler(context, {"skill": "plain"})
    assert outcome.ok
    assert "skill_model" not in context.turn_state


async def test_skill_handler_preserves_prior_skill_model_when_not_declared() -> None:
    """上一个 skill 设了模型，当前 skill 没声明——已有的 ``skill_model`` 不被清掉。

    handler 无条件写 ``turn_state["skill_model"] = entry.get("model") or ""``，
    没声明的 skill 会把上一个 skill 的模型覆盖成空串，模型被悄悄切回默认。
    """
    catalog = SkillCatalog(
        [{"name": "plain", "description": "d", "instructions": "body"}]
    )
    context = _tool_context(catalog, turn_state={"skill_model": "gpt-5"})
    tool = _skill_tool()
    outcome = await tool.handler(context, {"skill": "plain"})
    assert outcome.ok
    assert context.turn_state["skill_model"] == "gpt-5"


async def test_skill_handler_overwrites_prior_skill_model_when_declared() -> None:
    """后调用的 skill 声明了不同模型——覆盖上一个 skill 设的模型。

    不覆盖，第一个 skill 设的模型永远留着，后面的 skill 声明被忽略。
    """
    catalog = SkillCatalog(
        [
            {
                "name": "new-model",
                "description": "d",
                "instructions": "body",
                "model": "claude-x",
            }
        ]
    )
    context = _tool_context(catalog, turn_state={"skill_model": "gpt-5"})
    tool = _skill_tool()
    outcome = await tool.handler(context, {"skill": "new-model"})
    assert outcome.ok
    assert context.turn_state["skill_model"] == "claude-x"


# --------------------------------------------------------------------------- #
# 4. 适配器 _payload 覆盖
# --------------------------------------------------------------------------- #


def _adapter_config(model: str = "config-model", **overrides):
    from blank_api.services.models.base import AdapterConfig

    return AdapterConfig(
        name="test",
        base_url="http://localhost",
        api_key="",
        model=model,
        **overrides,
    )


def _anthropic_adapter(model: str = "config-model"):
    from blank_api.services.models.anthropic import AnthropicAdapter

    return AnthropicAdapter(_adapter_config(model, kind="anthropic"))


def _openai_compat_adapter(model: str = "config-model"):
    from blank_api.services.models.openai_compat import OpenAICompatibleAdapter

    return OpenAICompatibleAdapter(_adapter_config(model, kind="openai"))


def _openai_responses_adapter(model: str = "config-model"):
    from blank_api.services.models.openai_responses import OpenAIResponsesAdapter

    return OpenAIResponsesAdapter(_adapter_config(model, kind="openai_responses"))


def _ollama_adapter(model: str = "config-model"):
    from blank_api.services.models.ollama import OllamaAdapter

    return OllamaAdapter(_adapter_config(model, kind="ollama"))


def test_anthropic_payload_uses_override_model() -> None:
    """Anthropic ``_payload``：传了 model 就用它，不传回落配置模型。

    不覆盖，``model`` 参数被忽略，skill 声明的模型永远发不出去。
    """
    adapter = _anthropic_adapter("config-model")
    overridden = adapter._payload([], None, False, model="skill-model")
    assert overridden["model"] == "skill-model"
    plain = adapter._payload([], None, False)
    assert plain["model"] == "config-model"


def test_openai_compat_payload_uses_override_model() -> None:
    """OpenAI 兼容 ``_payload``：同上。"""
    adapter = _openai_compat_adapter("config-model")
    overridden = adapter._payload([], None, False, model="skill-model")
    assert overridden["model"] == "skill-model"
    plain = adapter._payload([], None, False)
    assert plain["model"] == "config-model"


def test_openai_responses_payload_uses_override_model() -> None:
    """Responses ``_payload``：同上。"""
    adapter = _openai_responses_adapter("config-model")
    overridden = adapter._payload([], None, False, model="skill-model")
    assert overridden["model"] == "skill-model"
    plain = adapter._payload([], None, False)
    assert plain["model"] == "config-model"


def test_ollama_payload_uses_override_model() -> None:
    """Ollama ``_payload``：同上。"""
    adapter = _ollama_adapter("config-model")
    overridden = adapter._payload([], None, False, model="skill-model")
    assert overridden["model"] == "skill-model"
    plain = adapter._payload([], None, False)
    assert plain["model"] == "config-model"


# --------------------------------------------------------------------------- #
# 5. 适配器方法签名接受 model
# --------------------------------------------------------------------------- #


def test_anthropic_methods_accept_model_param() -> None:
    """Anthropic 的 ``invoke`` / ``stream`` / ``bind_tools_stream`` 签名里有 ``model``。

    签名里没有这个参数，调用方传 ``model=`` 会 TypeError，覆盖路径直接断。
    """
    import inspect

    from blank_api.services.models.anthropic import AnthropicAdapter

    adapter = _anthropic_adapter()
    for method_name in ("invoke", "stream", "bind_tools_stream"):
        method = getattr(adapter, method_name)
        params = inspect.signature(method).parameters
        assert "model" in params, f"AnthropicAdapter.{method_name} 没有 model 参数"


def test_openai_compat_methods_accept_model_param() -> None:
    """OpenAI 兼容的三个方法签名里有 ``model``。"""
    import inspect

    from blank_api.services.models.openai_compat import OpenAICompatibleAdapter

    adapter = _openai_compat_adapter()
    for method_name in ("invoke", "stream", "bind_tools_stream"):
        method = getattr(adapter, method_name)
        params = inspect.signature(method).parameters
        assert "model" in params, f"OpenAICompatibleAdapter.{method_name} 没有 model 参数"


def test_openai_responses_methods_accept_model_param() -> None:
    """Responses 的三个方法签名里有 ``model``。"""
    import inspect

    from blank_api.services.models.openai_responses import OpenAIResponsesAdapter

    adapter = _openai_responses_adapter()
    for method_name in ("invoke", "stream", "bind_tools_stream"):
        method = getattr(adapter, method_name)
        params = inspect.signature(method).parameters
        assert "model" in params, f"OpenAIResponsesAdapter.{method_name} 没有 model 参数"


def test_ollama_methods_accept_model_param() -> None:
    """Ollama 的三个方法签名里有 ``model``。"""
    import inspect

    from blank_api.services.models.ollama import OllamaAdapter

    adapter = _ollama_adapter()
    for method_name in ("invoke", "stream", "bind_tools_stream"):
        method = getattr(adapter, method_name)
        params = inspect.signature(method).parameters
        assert "model" in params, f"OllamaAdapter.{method_name} 没有 model 参数"


def test_invoke_fallback_model_uses_override_when_provider_omits_model() -> None:
    """非流式响应里供应商没回 model 时，fallback 用传入的 model 而不是配置模型。

    ``_parse_completion`` 的 fallback 链写错（不认传入的 model），供应商没回 model
    字段时，响应里记录的是配置模型而不是 skill 覆盖的模型——用量账记错对象。
    """
    from blank_api.services.models.openai_compat import OpenAICompatibleAdapter

    adapter = _openai_compat_adapter("config-model")
    # 供应商响应里没有 model 字段
    data = {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}], "id": "x"}
    response = adapter._parse_completion(data, model="skill-model")
    assert response.model == "skill-model"

    # 供应商响应里带了 model 字段——以供应商的为准
    data2 = {
        "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
        "id": "x",
        "model": "provider-model",
    }
    response2 = adapter._parse_completion(data2, model="skill-model")
    assert response2.model == "provider-model"


# --------------------------------------------------------------------------- #
# 6. graph 透传：call_model 读 skill_model 并传给 _call_adapter
# --------------------------------------------------------------------------- #


def test_call_model_reads_skill_model_from_turn_state() -> None:
    """``call_model`` 从 ``turn_state["skill_model"]`` 读模型并透传给 ``_call_adapter``。

    读取的键名写错（比如读 ``turn_state["model"]``），skill 设的模型永远传不到适配器。
    """
    from blank_api.services.agent.graph import AgentSession

    source = inspect.getsource(AgentSession.call_model)
    assert "skill_model" in source
    assert "model=str(getattr(self.context, \"turn_state\", {}).get(\"skill_model\", \"\") or \"\")" in source


def test_call_adapter_passes_model_to_bind_tools_stream() -> None:
    """``_call_adapter`` 把 model/on_tool_call 透传给 ``bind_tools_stream``。

    透传断在这里，适配器拿到的 model 永远是空串。只查 ``"model=model" in source``
    不够——``_call_non_stream`` 的调用里也有这串，去掉 ``bind_tools_stream`` 的
    透传后源里仍能找到，判据不会红。这里只看 ``bind_tools_stream(...)`` 这一次调用
    的参数列表：``on_delta`` / ``on_tool_call`` / ``model`` 缺一即红。
    """
    from blank_api.services.agent.graph import AgentSession

    source = inspect.getsource(AgentSession._call_adapter)
    start = source.index("bind_tools_stream(")
    call = source[start : source.index(")", start)]
    assert "on_delta=relay" in call
    assert "on_tool_call=_on_tool_call" in call
    assert "model=model" in call


def test_call_non_stream_passes_model_to_bind_tools() -> None:
    """``_call_non_stream`` 把 model 透传给 ``adapter.bind_tools``。

    非流式退路断了，关掉流式开关时模型覆盖不生效。
    """
    from blank_api.services.agent.graph import AgentSession

    source = inspect.getsource(AgentSession._call_non_stream)
    assert "model=model" in source
