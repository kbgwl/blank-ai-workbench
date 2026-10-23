import json
import time
import uuid
from collections.abc import Callable

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from blank_api.api.auth import require_token
from blank_api.api.errors import ApiError
from blank_api.deps import DbSession
from blank_api.models.model_provider import PROVIDER_PRESETS, PROVIDER_GONE, ModelProvider, ModelUsage
from blank_api.schemas.model import ChatIn, ProviderCreate, ProviderOut, ProviderUpdate
from blank_api.services.crypto import decrypt_secret, encrypt_secret, key_hint
from blank_api.services.models.base import ChatMessage, ModelError
from blank_api.services.models.pricing import estimate_cost
from blank_api.services.models.registry import KINDS, build_adapter

router = APIRouter(dependencies=[Depends(require_token)])

ClientFactory = Callable[..., httpx.AsyncClient]


def _factory_from(request: Request) -> ClientFactory | None:
    """Tests embed a mock transport here; production falls back to httpx.AsyncClient."""
    return getattr(request.app.state, "model_client_factory", None)


async def _get_provider(db: AsyncSession, provider_id: str) -> ModelProvider:
    provider = await db.get(ModelProvider, provider_id)
    if provider is None:
        raise ApiError(code="provider_not_found", message="模型供应商不存在", status_code=404)
    return provider


def _out(provider: ModelProvider) -> ProviderOut:
    return ProviderOut(
        id=provider.id,
        name=provider.name,
        kind=provider.kind,
        base_url=provider.base_url,
        model=provider.default_model,
        temperature=provider.temperature,
        max_tokens=provider.max_tokens,
        context_window=provider.context_window,
        supports_tools=provider.supports_tools,
        supports_vision=provider.supports_vision,
        reasoning_effort=getattr(provider, "reasoning_effort", "") or "",
        verbosity=getattr(provider, "verbosity", "") or "",
        total_params_b=float(getattr(provider, "total_params_b", 0) or 0),
        active_params_b=float(getattr(provider, "active_params_b", 0) or 0),
        enabled=provider.enabled,
        api_key_hint=key_hint(_plain_key(provider)),
    )


def _plain_key(provider: ModelProvider) -> str:
    if not provider.api_key_encrypted:
        return ""
    try:
        return decrypt_secret(provider.api_key_encrypted)
    except ApiError:
        return ""


async def _record_usage(
    db: AsyncSession, provider: ModelProvider, model: str, usage, latency_ms: int, status: str
) -> None:
    db.add(
        ModelUsage(
            id=str(uuid.uuid4()),
            provider_id=provider.id,
            model=model or provider.default_model,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            latency_ms=latency_ms,
            status=status,
        )
    )
    await db.commit()


@router.get("/providers/presets")
async def presets() -> dict:
    return {"presets": PROVIDER_PRESETS, "kinds": sorted({p["kind"] for p in PROVIDER_PRESETS.values()})}


@router.get("/providers", response_model=list[ProviderOut])
async def list_providers(db: DbSession) -> list[ProviderOut]:
    rows = (await db.execute(select(ModelProvider).order_by(ModelProvider.name))).scalars()
    return [_out(p) for p in rows]


@router.post("/providers", response_model=ProviderOut, status_code=201)
async def create_provider(payload: ProviderCreate, db: DbSession) -> ProviderOut:
    if payload.kind not in KINDS:
        raise ApiError(
            code="provider_kind_unsupported",
            message=f"不支持的供应商类型：{payload.kind}",
            status_code=422,
        )
    provider = ModelProvider(
        id=str(uuid.uuid4()),
        name=payload.name,
        kind=payload.kind,
        base_url=payload.base_url,
        api_key_encrypted=encrypt_secret(payload.api_key) if payload.api_key else "",
        default_model=payload.model,
        temperature=payload.temperature,
        max_tokens=payload.max_tokens,
        context_window=payload.context_window,
        supports_tools=payload.supports_tools,
        supports_vision=payload.supports_vision,
        reasoning_effort=payload.reasoning_effort,
        verbosity=payload.verbosity,
        total_params_b=payload.total_params_b,
        active_params_b=payload.active_params_b,
        enabled=payload.enabled,
    )
    db.add(provider)
    await db.commit()
    return _out(provider)


@router.get("/providers/{provider_id}", response_model=ProviderOut)
async def get_provider(provider_id: str, db: DbSession) -> ProviderOut:
    return _out(await _get_provider(db, provider_id))


@router.patch("/providers/{provider_id}", response_model=ProviderOut)
async def update_provider(provider_id: str, payload: ProviderUpdate, db: DbSession) -> ProviderOut:
    provider = await _get_provider(db, provider_id)
    data = payload.model_dump(exclude_unset=True)
    api_key = data.pop("api_key", None)
    for field, value in data.items():
        setattr(provider, "default_model" if field == "model" else field, value)
    if api_key:
        provider.api_key_encrypted = encrypt_secret(api_key)
    await db.commit()
    return _out(provider)


@router.delete("/providers/{provider_id}", status_code=204)
async def delete_provider(provider_id: str, db: DbSession) -> None:
    await db.delete(await _get_provider(db, provider_id))
    await db.commit()


@router.post("/providers/{provider_id}/test")
async def test_provider(provider_id: str, request: Request, db: DbSession) -> dict:
    provider = await _get_provider(db, provider_id)
    adapter = build_adapter(provider, _factory_from(request))
    started = time.perf_counter()
    try:
        result = await adapter.invoke([ChatMessage(role="user", content="ping")])
    except ModelError as exc:
        await _record_usage(db, provider, provider.default_model, _zero_usage(), int((time.perf_counter() - started) * 1000), "error")
        return {"ok": False, "code": exc.code, "message": exc.message, "capabilities": adapter.capabilities}
    latency = int((time.perf_counter() - started) * 1000)
    await _record_usage(db, provider, result.model, result.usage, latency, "ok")
    return {
        "ok": True,
        "model": result.model,
        "latency_ms": latency,
        "usage": {
            "prompt_tokens": result.usage.prompt_tokens,
            "completion_tokens": result.usage.completion_tokens,
        },
        "capabilities": adapter.capabilities,
    }


def _zero_usage():
    from blank_api.services.models.base import Usage

    return Usage()


@router.post("/providers/{provider_id}/chat")
async def chat(provider_id: str, payload: ChatIn, request: Request, db: DbSession) -> dict:
    provider = await _get_provider(db, provider_id)
    adapter = build_adapter(provider, _factory_from(request))
    started = time.perf_counter()
    try:
        result = await adapter.invoke([ChatMessage(role="user", content=payload.message)])
    except ModelError as exc:
        await _record_usage(db, provider, provider.default_model, _zero_usage(), 0, "error")
        raise ApiError(code=exc.code, message=exc.message, status_code=502) from None
    await _record_usage(
        db, provider, result.model, result.usage, int((time.perf_counter() - started) * 1000), "ok"
    )
    return {
        "text": result.text,
        "model": result.model,
        "usage": {
            "prompt_tokens": result.usage.prompt_tokens,
            "completion_tokens": result.usage.completion_tokens,
        },
    }


@router.post("/providers/{provider_id}/chat/stream")
async def chat_stream(
    provider_id: str, payload: ChatIn, request: Request, db: DbSession
) -> StreamingResponse:
    provider = await _get_provider(db, provider_id)
    adapter = build_adapter(provider, _factory_from(request))

    async def events():
        collected: list[str] = []
        try:
            async for chunk in adapter.stream([ChatMessage(role="user", content=payload.message)]):
                collected.append(chunk)
                yield f"event: delta\ndata: {json.dumps({'text': chunk}, ensure_ascii=False)}\n\n"
        except ModelError as exc:
            yield f"event: error\ndata: {exc.message}\n\n"
            await _record_usage(db, provider, provider.default_model, _zero_usage(), 0, "error")
            return
        text = "".join(collected)
        await _record_usage(db, provider, provider.default_model, _estimate(text), 0, "ok")
        yield "event: done\ndata: completed\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


def _estimate(text: str):
    from blank_api.services.models.base import Usage

    return Usage(prompt_tokens=0, completion_tokens=max(1, len(text) // 4))


@router.get("/usage/summary")
async def usage_summary(db: DbSession) -> dict:
    rows = (
        await db.execute(
            select(
                ModelProvider.name,
                ModelUsage.model,
                ModelUsage.status,
                ModelUsage.prompt_tokens,
                ModelUsage.completion_tokens,
                ModelUsage.latency_ms,
                ModelUsage.cached_tokens,
            )
            # 左连接而不是内连接：供应商行删掉以后它名下的钱还在账上（见 model_provider.py 的说明）。
            # 这条路径是成本页与总览页的合计来源，内连接会让界面上直接少算。
            .select_from(ModelUsage)
            .outerjoin(ModelProvider, ModelProvider.id == ModelUsage.provider_id)
        )
    ).all()
    totals: dict[str, dict] = {}
    for name, model, status, prompt, completion, latency, cached in rows:
        provider = name or PROVIDER_GONE
        bucket = totals.setdefault(
            f"{provider}/{model}",
            {"provider": provider, "model": model, "calls": 0, "failed": 0, "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "total_latency_ms": 0},
        )
        bucket["calls"] += 1
        bucket["failed"] += 1 if status != "ok" else 0
        bucket["prompt_tokens"] += prompt or 0
        bucket["completion_tokens"] += completion or 0
        bucket["cached_tokens"] += cached or 0
        bucket["total_latency_ms"] += latency or 0
    priced: list[dict] = []
    currency = "USD"
    grand_cost = 0.0
    for bucket in totals.values():
        cost = estimate_cost(
            bucket["model"], bucket["prompt_tokens"], bucket["completion_tokens"], bucket["cached_tokens"]
        )
        currency = cost.currency if cost.priced else currency
        bucket["cost"] = cost.as_dict()
        bucket["cache_hit_rate"] = (
            round(bucket["cached_tokens"] / bucket["prompt_tokens"], 3) if bucket["prompt_tokens"] else 0
        )
        bucket["success_rate"] = round(
            (bucket["calls"] - bucket["failed"]) / bucket["calls"], 3
        ) if bucket["calls"] else 0
        bucket["avg_latency_ms"] = round(bucket["total_latency_ms"] / bucket["calls"]) if bucket["calls"] else 0
        grand_cost += cost.amount
        priced.append(bucket)
    succeeded = sum(1 for row in rows if row[2] == "ok")
    return {
        "providers": priced,
        "total_calls": len(rows),
        "success_rate": round(succeeded / len(rows), 3) if rows else 0,
        "avg_latency_ms": round(
            sum((row[5] or 0) for row in rows) / len(rows)
        ) if rows else 0,
        "total_cost": {"amount": round(grand_cost, 6), "currency": currency},
    }
