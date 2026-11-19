"""主模型看不见图时，请一家能看的模型代看一次。

背景：用户把默认模型换成讯飞 spark-x2.6（窗口 200K、纯文本）。`browser_check`
的截图于是被摘掉，模型只剩控制台错误清单——"页面有没有报错"知道，"页面长什么样"
不知道。它会在结论里写"只在桌面视口验证过"，但那是猜的。

为什么不让用户换模型：换掉之后写代码、做推理的模型也一起变了，那是另一件事。
"看图"是**一次小任务**——把一张截图说清楚，不需要主模型那样的能力，也不该绑在它身上。

为什么不干脆不要图：截图是唯一能发现"布局塌了、元素叠在一起、白屏但有 DOM"的证据。
控制台一条错都没有的页面，可能整块内容都被 `overflow: hidden` 切掉了。

三条纪律：
1. 只在主模型**看不了**时才调用。能看就直接给，多一次调用就是多一份延迟与费用。
2. 失败不产出：拿不到描述就返回空串，让调用方退回"你看不到图"的老文案——
   绝不能编一段"看起来像图"的描述，那比不给更坏。
3. 每轮有上限。模型会反复截图，没有上限就是给它一个无限花钱的按钮。
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from blank_api.db import get_session_factory
from blank_api.models.model_provider import ModelProvider
from blank_api.services.agent.vision import supports_vision as vision_ok
from blank_api.services.models.base import ChatMessage
from blank_api.services.models.registry import build_adapter
from blank_api.services.settings import SettingsStore

log = logging.getLogger(__name__)

# 一轮最多代看几张。够它"看一眼页面、改完再看一眼"，不够它拿截图当玩具。
MAX_PROXY_CALLS = 8
# 代看是辅助动作：等太久不如直接说"没看到"。主循环在等它。
PROXY_TIMEOUT_SECONDS = 60.0

PROMPT = (
    "这是一个网页的截图。请用 3–6 句中文客观描述你看到的内容，重点依次是："
    "① 整体布局是否完整（有没有明显的空白大块、错位、元素重叠、被裁切）；"
    "② 主要区域各是什么（标题、面板、图表、列表等）；"
    "③ 图中可见的文字或数字，挑能看清的关键几处照抄；"
    "④ 有没有明显的渲染异常（乱码、黑块、占位符、错误提示）。"
    "只描述你确实看到的，看不清就说看不清，不要推测代码或功能。"
)


def _configured_provider_id() -> str:
    try:
        return str((SettingsStore().read().get("model") or {}).get("vision_provider") or "").strip()
    except Exception:  # noqa: BLE001 - 设置读不出来不该让看图这一步炸掉
        return ""


async def _pick_provider(exclude: str) -> ModelProvider | None:
    """挑一家能看图的供应商。

    优先设置里显式指定的 `model.vision_provider`；没配就按「有 key 的、声明支持视觉的」
    挑第一家。**排除主模型自己**——它刚被判定看不见，再问它一次只是浪费一轮。
    """
    configured = _configured_provider_id()
    async with get_session_factory()() as session:
        if configured:
            provider = await session.get(ModelProvider, configured)
            if provider is not None and provider.enabled and provider.id != exclude:
                if vision_ok(getattr(provider, "supports_vision", False), provider.default_model or ""):
                    return provider
            log.warning("配置的视觉供应商 %s 不可用（不存在/停用/看不了图），改为自动挑一家", configured)
        rows = (
            await session.execute(
                select(ModelProvider).where(ModelProvider.enabled == True)  # noqa: E712
            )
        ).scalars().all()
    for provider in rows:
        if provider.id == exclude:
            continue
        if not str(provider.api_key_encrypted or ""):
            continue
        if vision_ok(getattr(provider, "supports_vision", False), provider.default_model or ""):
            return provider
    return None


async def describe(image: dict, *, question: str = "", exclude_provider: str = "") -> tuple[str, str]:
    """请能看图的模型描述这张图。返回 (描述, 供应商名)；拿不到返回 ("", "")。"""
    data = str((image or {}).get("data") or "")
    if not data:
        return "", ""
    provider = await _pick_provider(exclude_provider)
    if provider is None:
        return "", ""
    media = str(image.get("media_type") or "image/jpeg")
    message = ChatMessage(
        role="user",
        content=(question or PROMPT),
        images=[{"media_type": media, "data": data}],
    )
    try:
        adapter = build_adapter(provider)
        result = await adapter.invoke([message])
    except Exception as error:  # noqa: BLE001 - 代看失败只能降级，不能把工具调用带崩
        log.warning("代看失败（%s）：%s", provider.name, str(error)[:200])
        return "", ""
    text = str(getattr(result, "text", "") or "").strip()
    if not text:
        return "", ""
    return text[:2000], provider.name


async def describe_for_session(image: dict, *, session, step: int, question: str = "") -> tuple[str, str]:
    """带节流的入口：主循环里每张图都走这里。

    节流状态放在会话上而不是模块级变量——两个任务并行时，模块级计数会互相吃掉名额。
    """
    if session is None:
        return "", ""
    # 只有**真的会话**才代看：它带着 provider_id 与代看账本（vision_proxy_calls）。
    # 单测/回放用的会话替身没有这两格，getattr 会给出默认值并让这里"看起来可以代看"，
    # 于是一次纯装配就顺手打了一次外部模型——那不是省事，是失控。
    if not hasattr(session, "vision_proxy_calls"):
        return "", ""
    used = int(getattr(session, "vision_proxy_calls", 0) or 0)
    if used >= MAX_PROXY_CALLS:
        return "", ""
    current = str(getattr(session, "provider_id", "") or "")
    text, name = await describe(image, question=question, exclude_provider=current)
    if text:
        session.vision_proxy_calls = used + 1
        log.info("代看 step=%s 由 %s 完成，%s 字", step, name, len(text))
    return text, name
