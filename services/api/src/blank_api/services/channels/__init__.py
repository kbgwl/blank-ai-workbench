"""消息通道：把「客户自己的 IM」接进来。

对外只有四个动作：查看目录（``catalog``）、存取配置（``store``）、
把提醒发出去（``delivery``）、把收到的消息变成任务（``inbound``）。
每个厂商的协议差异全部关在各自的适配器里，其余模块只认 ``Adapter``。
"""

from __future__ import annotations

from blank_api.services.channels.base import Adapter, Check, Field, Inbound, Send
from blank_api.services.channels.dingtalk import DingTalkAdapter
from blank_api.services.channels.feishu import FeishuAdapter
from blank_api.services.channels.webhook import WebhookAdapter
from blank_api.services.channels.wecom import WeComAdapter

# 顺序即界面顺序：客户最常配的两个在前，通用兜底最后。
ADAPTERS: dict[str, Adapter] = {
    adapter.kind: adapter
    for adapter in (
        WeComAdapter(),
        DingTalkAdapter(),
        FeishuAdapter(),
        WebhookAdapter(),
    )
}


def adapter_for(kind: str) -> Adapter | None:
    return ADAPTERS.get(kind)


def catalog() -> list[dict]:
    return [adapter.schema() for adapter in ADAPTERS.values()]


__all__ = ["ADAPTERS", "Adapter", "Check", "Field", "Inbound", "Send", "adapter_for", "catalog"]
