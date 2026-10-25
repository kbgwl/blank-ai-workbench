from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, UniqueConstraint, false, func, text, true
from sqlalchemy.orm import Mapped, mapped_column

from blank_api.db import Base

PROVIDER_KINDS = ("openai_compatible", "openai_responses", "anthropic", "ollama")

# 账行上的 provider_id 找不到对应配置行时念出来的名字。两个读模型（observability、usage）
# 共用这一个说法，界面上才不会出现"None"或者干脆凭空少一笔钱。
PROVIDER_GONE = "供应商已删除"

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

    # 与迁移 0003 同名：列上的 unique=True 会被 PG 自动命名成 model_providers_name_key，
    # 两条建库路径（alembic / create_all）于是给出两个名字，按名字 DROP 的脚本会一个中一个空。
    __table_args__ = (UniqueConstraint("name", name="uq_model_providers_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    api_key_encrypted: Mapped[str] = mapped_column(
        String(1000), nullable=False, default="", server_default=""
    )
    default_model: Mapped[str] = mapped_column(String(200), nullable=False)
    temperature: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.2, server_default="0.2"
    )
    max_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2048, server_default="2048"
    )
    context_window: Mapped[int] = mapped_column(
        Integer, nullable=False, default=128000, server_default="128000"
    )
    supports_tools: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    supports_vision: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
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
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ModelUsage(Base):
    __tablename__ = "model_usage"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # 账本行对配置表不许有引用完整性。外键写在 SQLite 上从不生效（连接层没开
    # `PRAGMA foreign_keys`），在 MySQL/PostgreSQL 上会照声明执行：以前这一列带
    # `ON DELETE CASCADE`，真 MySQL 8.4 实测删一家供应商 = 它名下 3 笔 600 tokens 直接归零。
    # Codex 的 `token_usage_record` 同样不引用供应商配置表——账是历史事实，不跟配置一起消失。
    # 于是"供应商被删"是一条正常状态而不是异常状态：provider_id 悬空，读模型用左连接
    # 把它念成「供应商已删除」（见 api/routes/observability.py、services/agent/evidence.py）。
    provider_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    trace_id: Mapped[str] = mapped_column(
        String(120), nullable=False, default="", server_default="", index=True
    )
    node: Mapped[str] = mapped_column(
        String(60), nullable=False, default="chat", server_default="chat"
    )
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    completion_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # 缓存与推理的细分计量：不拆出来就看不出"便宜是因为命中缓存"还是"少调了一次"
    cached_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    cache_write_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    reasoning_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # 供应商那一笔请求的 id。空串=这家没给（Ollama 一类），不许拿我们自己的 id 顶上。
    response_id: Mapped[str] = mapped_column(String(120), nullable=False, default="", server_default="", index=True)
    # 回合归属：这一笔钱是谁引起的、属于哪一轮。对标 Codex 的
    # `token_usage_record{turn_id, root_turn_id}` 与 `turn_attribution{parent_turn_id, initiating_agent_path}`。
    # 以前只有 task_id + trace_id：能问"这个任务花了多少"，问不出"这一轮花了多少"
    # （同一任务重跑三次会并成一堆），也问不出"这条子分支是被哪条线程派下去的"。
    # 空串一律表示"这条行上线前就存在，当时没记"，不许读成第 0 轮或根线程为空。
    run_id: Mapped[str] = mapped_column(String(120), nullable=False, default="", server_default="", index=True)
    # 发起轮（origin）：引起这一整棵任务树的那一轮。同一轮里每个发言成员都有自己的
    # task_id 和自己的 run_id，所以「这几个人合起来花了多少」用 run_id 问不出来——
    # 它是每个成员各一个值，而轮只有一个。这一列把整轮的钱收在一个键上。
    # 顶层运行的发起轮就是它自己；子代理跟着父记录器，不用自己填。
    # 空串=这一行上线前就存在，当时没记（不许读成"没有发起轮"）。
    origin_run_id: Mapped[str] = mapped_column(
        String(120), nullable=False, default="", server_default="", index=True
    )
    root_trace_id: Mapped[str] = mapped_column(String(120), nullable=False, default="", server_default="")
    parent_trace_id: Mapped[str] = mapped_column(String(120), nullable=False, default="", server_default="")
    agent_path: Mapped[str] = mapped_column(String(200), nullable=False, default="", server_default="")
    # 这一笔属于哪一次并行编排（fan_out）。空串=不是并行派出去的（单发子代理、主线程、摘要），
    # 或者这行上线前就存在——"没说"与"第 0 批"是两回事，读出来必须是 null。
    parallel_group: Mapped[str] = mapped_column(
        String(24), nullable=False, default="", server_default="", index=True
    )
    # 这一笔账跑在哪个沙箱档位。空串=这一列上线前的老行（当时没记），不许读成 workspace。
    sandbox_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default="", server_default=""
    )
    latency_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="ok", server_default="ok"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
