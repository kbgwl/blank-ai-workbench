from pydantic import BaseModel, Field

from blank_api.models.model_provider import PROVIDER_KINDS


class ProviderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: str = Field(default="openai_compatible")
    base_url: str = Field(min_length=1, max_length=500)
    api_key: str = ""
    model: str = Field(min_length=1, max_length=200)
    temperature: float = 0.2
    max_tokens: int = 2048
    context_window: int = 128000
    supports_tools: bool = False
    supports_vision: bool = False
    enabled: bool = True


class ProviderUpdate(BaseModel):
    kind: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    context_window: int | None = None
    supports_tools: bool | None = None
    supports_vision: bool | None = None
    enabled: bool | None = None


class ProviderOut(BaseModel):
    id: str
    name: str
    kind: str
    base_url: str
    model: str
    temperature: float
    max_tokens: int
    context_window: int
    supports_tools: bool
    supports_vision: bool
    enabled: bool
    api_key_hint: str


class ChatIn(BaseModel):
    message: str
    stream: bool = False


class UsageOut(BaseModel):
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    status: str


_ = PROVIDER_KINDS
