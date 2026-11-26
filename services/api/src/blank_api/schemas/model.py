from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

from blank_api.models.model_provider import PROVIDER_KINDS


def _http_base_url(value: str) -> str:
    """供应商地址只允许 http(s)，且不能把账号密码塞进 URL。

    adapter 会把它直接交给 httpx；file:/data:/带凭据的地址要么起不来，要么
    把凭据写进请求行与日志。这里在入口拒掉，比等到最贵的那次调用再报错便宜。
    """
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("base_url 只允许 http(s) 地址")
    if parsed.username or parsed.password:
        raise ValueError("base_url 不允许包含账号密码")
    return value.rstrip("/")


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
    reasoning_effort: str = Field(default="", max_length=20)
    verbosity: str = Field(default="", max_length=20)
    total_params_b: float = Field(default=0.0, ge=0.0, le=100_000.0)
    active_params_b: float = Field(default=0.0, ge=0.0, le=100_000.0)
    enabled: bool = True

    @field_validator("base_url")
    @classmethod
    def _valid_base_url(cls, value: str) -> str:
        return _http_base_url(value)


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
    reasoning_effort: str | None = Field(default=None, max_length=20)
    verbosity: str | None = Field(default=None, max_length=20)
    total_params_b: float | None = Field(default=None, ge=0.0, le=100_000.0)
    active_params_b: float | None = Field(default=None, ge=0.0, le=100_000.0)
    enabled: bool | None = None

    @field_validator("base_url")
    @classmethod
    def _valid_base_url(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("base_url 不能为空")
        return _http_base_url(value)


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
    reasoning_effort: str = ""
    verbosity: str = ""
    total_params_b: float = 0.0
    active_params_b: float = 0.0
    enabled: bool
    api_key_hint: str
    # 设置页锁定的默认供应商（settings.model.default_provider）。
    # 前端据此显示"默认模型"实际会是哪一家，而不是按列表第一项猜。
    is_default: bool = False


class ProviderDefaultIn(BaseModel):
    # 空串 = 清掉显式默认，回到「有 Key 的第一家」。
    provider_id: str = ""


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
