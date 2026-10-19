from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from blank_api.db import Base

PROVIDER_KINDS = ("openai_compatible", "openai_responses", "anthropic", "ollama")

PROVIDER_PRESETS: dict[str, dict] = {
    "openai": {"kind": "openai_compatible", "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini", "supports_tools": True},
    # Responses 与 chat/completions 是两套协议：新模型走这条才能用上推理档位与提示词缓存
    "openai_responses": {"kind": "openai_responses", "base_url": "https://api.openai.com/v1", "model": "gpt-4.1", "supports_tools": True},
    "deepseek": {"kind": "openai_compatible", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat", "supports_tools": True},
    "qwen": {"kind": "openai_compatible", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus", "supports_tools": True},
    "anthropic": {"kind": "anthropic", "base_url": "https://api.anthropic.com", "model": "claude-3-5-sonnet-latest", "supports_tools": True},
    "ollama": {"kind": "ollama", "base_url": "http://localhost:11434", "model": "qwen2.5-coder:7b", "supports_tools": True},
}


class ModelProvider(Base):
    __tablename__ = "model_providers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    api_key_encrypted: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    default_model: Mapped[str] = mapped_column(String(200), nullable=False)
    temperature: Mapped[float] = mapped_column(Float, nullable=False, default=0.2)
    max_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=2048)
    context_window: Mapped[int] = mapped_column(Integer, nullable=False, default=128000)
    supports_tools: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    supports_vision: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 输出档位：只有 Responses / 推理系列认这两个字段，其余供应商留空即可
    reasoning_effort: Mapped[str] = mapped_column(String(20), nullable=False, default="", server_default="")
    verbosity: Mapped[str] = mapped_column(String(20), nullable=False, default="", server_default="")
    # 参数量（单位：十亿）。稠密模型两者相等，MoE 模型差一个数量级。
    # 这两个数字决定的是两件不同的事，混在一起算就会得出"这模型很便宜所以哪都能跑"的错结论：
    #   total   → 权重占多少显存（决定能不能塞进这台机器）
    #   active  → 每个 token 走多少算力（决定跑多快、多贵）
    # 0 表示未填，路由时按"未知"处理，不会拿它去比较成本。
    total_params_b: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default="0")
    active_params_b: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default="0")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ModelUsage(Base):
    __tablename__ = "model_usage"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    provider_id: Mapped[str] = mapped_column(
        ForeignKey("model_providers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    trace_id: Mapped[str] = mapped_column(String(120), nullable=False, default="", index=True)
    node: Mapped[str] = mapped_column(String(60), nullable=False, default="chat")
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 缓存与推理的细分计量：不拆出来就看不出"便宜是因为命中缓存"还是"少调了一次"
    cached_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    cache_write_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    reasoning_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ok")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
