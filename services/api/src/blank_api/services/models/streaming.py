"""把流式碎片拼回一条完整响应。

四家供应商的 SSE 事件名各不相同，但"这条响应能不能拿去执行工具"只有一条标准：
**参数拼完整了**。少一个引号，apply_patch 就会改错文件，而错误会以"改错地方"的
形式出现在几十行之后，届时已经看不出根因在解析层。

所以这里只做一件事，而且必须做对：按 index 把 name / arguments 的碎片累积起来，
最后一次性交给调用方。流式只影响"什么时候能看到文字"，不影响"什么时候能动工具"。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class _Pending:
    call_id: str = ""
    name: str = ""
    arguments: str = ""


@dataclass
class ToolCallAccumulator:
    """按 index 聚合工具调用碎片。"""

    _items: dict[int, _Pending] = field(default_factory=dict)
    _order: list[int] = field(default_factory=list)

    def add(
        self,
        index: int,
        *,
        call_id: str = "",
        name: str = "",
        arguments: str = "",
    ) -> None:
        """记一个碎片。同一个 index 会累加，不同 index 各成一条调用。"""
        slot = self._items.get(index)
        if slot is None:
            slot = _Pending()
            self._items[index] = slot
            self._order.append(index)
        if call_id:
            # 有的供应商只在第一片给 id，后面都是空串，不能用后到的空值覆盖
            slot.call_id = call_id
        if name:
            slot.name = name
        if arguments:
            slot.arguments += arguments

    def __len__(self) -> int:
        return len(self._order)

    def __bool__(self) -> bool:
        return bool(self._order)

    def complete(self, index: int) -> dict | None:
        """某条调用的参数已经拼成完整 JSON 时返回它。

        只给「推测性只读执行」用：模型还在吐后面的调用时，先把已经落定的
        read_file / glob / search_code 跑起来。参数拼不完整就返回 None，
        绝不让半截 JSON 进入执行层。
        """
        slot = self._items.get(index)
        if slot is None or not slot.name:
            return None
        raw = slot.arguments.strip()
        # 一个参数碎片都没到就还不算完整：名字先到、参数后到是常态，
        # 这时按 {} 执行会跑一次参数错误的调用。JSON 对象一定以 } 结尾。
        if not raw or not raw.endswith("}"):
            return None
        try:
            arguments = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(arguments, dict):
            return None
        return {
            "id": slot.call_id or f"call_{index}",
            "name": slot.name,
            "arguments": arguments,
        }

    def as_list(self) -> list[dict]:
        """转成调用链认识的 OpenAI 形状。

        缺 id 时补一个基于 index 的稳定值：有些网关（尤其自建的 OpenAI 兼容层）
        压根不回 id，而工具结果回填时必须能对上号，对不上就是一次工具调用丢失。
        """
        calls: list[dict] = []
        for index in self._order:
            slot = self._items[index]
            if not slot.name:
                # 没有名字的调用不可能执行，留着只会让下一层报"未注册的工具："
                continue
            calls.append(
                {
                    "id": slot.call_id or f"call_{index}",
                    "type": "function",
                    "function": {"name": slot.name, "arguments": slot.arguments or "{}"},
                }
            )
        return calls


def invalid_tool_arguments(calls: list[dict]) -> list[tuple[str, int]]:
    """参数不是完整 JSON 的工具调用（流在参数中途被截断时的形状）。

    这类调用绝不能派发，更不能把那条 assistant 消息写回上下文：供应商会在下一次
    请求里直接拒掉（真机 23f93669：write_file 的参数 31009 字被截成半截字符串，
    下一次整包请求 400 invalid_prompt）。
    """
    bad: list[tuple[str, int]] = []
    for call in calls:
        function = call.get("function") or {}
        raw = str(function.get("arguments") or "{}")
        try:
            json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            bad.append((str(function.get("name") or ""), len(raw)))
    return bad
