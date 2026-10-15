"""Unified model layer: business code never talks to a vendor SDK directly."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field

import httpx


class ModelError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass
class ChatMessage:
    role: str  # system | user | assistant | tool
    content: str
    tool_call_id: str | None = None
    tool_calls: list[dict] = field(default_factory=list)


@dataclass
class ToolSpecLite:
    name: str
    description: str
    input_schema: dict


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class ModelResponse:
    text: str
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    finish_reason: str | None = None


@dataclass
class AdapterConfig:
    name: str
    kind: str
    base_url: str
    api_key: str
    model: str
    temperature: float = 0.2
    max_tokens: int = 2048
    supports_tools: bool = False
    supports_vision: bool = False
    timeout_seconds: float = 60.0
    client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient


class ModelAdapter(ABC):
    def __init__(self, config: AdapterConfig) -> None:
        self.config = config

    # ---- capability surface -------------------------------------------------
    @property
    def capabilities(self) -> dict:
        return {
            "provider": self.config.name,
            "kind": self.config.kind,
            "model": self.config.model,
            "streaming": True,
            "tools": self.config.supports_tools,
            "vision": self.config.supports_vision,
            "structured_output": True,
        }

    def require_tools(self, tools: Sequence[ToolSpecLite]) -> None:
        if tools and not self.config.supports_tools:
            raise ModelError(
                code="model_tools_unsupported",
                message=(
                    f"模型 {self.config.model} 未声明支持工具调用，无法在 Agent 流程中使用。"
                    "请在设置中勾选工具能力或改用支持工具的模型。"
                ),
            )

    # ---- transport helpers --------------------------------------------------
    def _client(self) -> httpx.AsyncClient:
        factory = self.config.client_factory
        if isinstance(factory, httpx.AsyncClient):
            return factory
        return factory(timeout=httpx.Timeout(self.config.timeout_seconds))

    async def _request_json(self, method: str, url: str, **kwargs) -> dict:
        try:
            async with self._client() as client:
                resp = await client.request(method, url, **kwargs)
        except httpx.TimeoutException:
            raise ModelError("model_timeout", f"模型请求超时：{self.config.name}", retryable=True) from None
        except httpx.HTTPError as exc:
            raise ModelError("model_unreachable", f"模型服务不可达：{self.config.name}（{exc.__class__.__name__}）", retryable=True) from None
        if resp.status_code >= 400:
            raise ModelError(
                "model_http_error",
                f"模型返回 {resp.status_code}：{resp.text[:200]}",
                retryable=resp.status_code >= 500,
            )
        return resp.json()

    @abstractmethod
    async def invoke(self, messages: Sequence[ChatMessage], **kwargs) -> ModelResponse: ...

    @abstractmethod
    def stream(self, messages: Sequence[ChatMessage], **kwargs) -> AsyncIterator[str]: ...

    async def structured_output(self, schema: dict, messages: Sequence[ChatMessage], **kwargs) -> dict:
        instruction = (
            "只输出符合以下 JSON Schema 的 JSON，不要输出解释或代码块标记：\n"
            + json.dumps(schema, ensure_ascii=False)
        )
        prompted = [ChatMessage(role="system", content=instruction), *messages]
        result = await self.invoke(prompted, **kwargs)
        return _parse_json_object(result.text)

    async def bind_tools(self, tools: Sequence[ToolSpecLite], messages: Sequence[ChatMessage], **kwargs) -> ModelResponse:
        self.require_tools(tools)
        return await self.invoke(messages, tools=tools, **kwargs)


def _parse_json_object(text: str) -> dict:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        stripped = stripped[4:] if stripped.lower().startswith("json") else stripped
    start, end = stripped.find("{"), stripped.rfind("}")
    if start == -1 or end == -1:
        raise ModelError("model_output_not_json", "模型未按要求返回 JSON")
    try:
        parsed = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ModelError("model_output_not_json", f"模型返回的 JSON 无法解析：{exc}") from None
    if not isinstance(parsed, dict):
        raise ModelError("model_output_not_json", "模型返回的 JSON 不是对象")
    return parsed


def sse_lines(body: AsyncIterator[bytes]) -> AsyncIterator[str]:
    return body


def data_payload(line: str) -> str | None:
    """Extract the payload of an SSE `data:` line."""
    if not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    return None if payload in {"", "[DONE]"} else payload
