"""任务级实时预览通道（内存）。

为什么不写事件表：

* 一次 800 token 的回复会变成几十行 "model.delta"，把活动流和一页能看清的时间线
  一起淹掉，还让 ``after_seq`` 游标失去"可跳读"的意义；
* 它只有"此刻正在看这个任务的人"关心。刷新页面之后应该看到完整回复，而不是最后
  800 个字符——持久化的应该是结果，不是过程。

所以这条通道允许丢：丢了只损失实时感，不影响任何持久记录，也不会让任务失败。
"""

from __future__ import annotations

import asyncio
import time

# 单个订阅者最多攒这么多帧。超过就丢最旧的：每帧都是"到目前为止的尾巴"，
# 丢旧帧不会让界面拼错，只会让首帧出现得更晚一点。
QUEUE_SIZE = 64


class LiveFeed:
    """按 task_id 广播实时增量。单进程内存实现。"""

    _queues: dict[str, set[asyncio.Queue]] = {}

    @classmethod
    def publish(cls, task_id: str, payload: dict) -> None:
        """推一帧。永不阻塞、永不抛：实时预览绝不能拖慢模型调用。"""
        if not task_id:
            return
        for queue in list(cls._queues.get(task_id) or ()):  # 复制一份，避免边迭代边改
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover - 与 full() 之间没有 await
                    pass
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:  # pragma: no cover - 同上，防御性
                continue

    @classmethod
    def wake(cls, task_id: str) -> None:
        """叫醒正在等事件的订阅者：**持久事件刚写进库了，别等下一次轮询**。

        为什么需要它：SSE 循环原本每 0.5 秒查一次库，一个 200 步的任务就是几千次
        查询，而其中绝大多数是空的。有了这一声唤醒，循环可以退到 2 秒才查一次空库，
        真正有事件时又被立刻叫起来——**空转的代价降下来，时延一分不涨**。

        跨进程无效（订阅表在内存里），所以它只是加速器：轮询必须继续存在，
        否则 worker 在另一个进程里写的事件就永远到不了界面。
        """
        cls.publish(task_id, {"wake": True})

    @classmethod
    def subscribe(cls, task_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        cls._queues.setdefault(task_id, set()).add(queue)
        return queue

    @classmethod
    def unsubscribe(cls, task_id: str, queue: asyncio.Queue) -> None:
        holders = cls._queues.get(task_id)
        if not holders:
            return
        holders.discard(queue)
        if not holders:
            cls._queues.pop(task_id, None)

    @classmethod
    def subscriber_count(cls, task_id: str) -> int:
        return len(cls._queues.get(task_id) or ())

    @classmethod
    def total_subscribers(cls) -> int:
        """当前所有任务上的订阅者总数（= 活跃 SSE 连接数，减去别的订阅方）。"""
        return sum(len(queues) for queues in cls._queues.values())

    @classmethod
    def active_tasks(cls) -> int:
        return len([queues for queues in cls._queues.values() if queues])

    @classmethod
    def reset(cls) -> None:
        """测试用：把所有订阅清掉，避免用例之间互相串流。"""
        cls._queues.clear()


# 攒批参数：太小会把模型调用拖成"每一片都等一次广播"，太大就看不到字在动。
FLUSH_SECONDS = 0.25
FLUSH_CHARS = 160
# 每帧只带"到目前为止的尾巴"。带上全文会让单个事件随回复长度平方增长，
# 而界面显示区本来也只有几行。
TAIL_CHARS = 800


class OutputRelay:
    """模型增量的攒批器：把碎片变成少量"截止到目前"的帧。

    三个刻意的选择：

    * **发尾巴而不是增量**。订阅方不用拼接，丢一帧也不会拼错——这是这条通道可以
      允许丢帧的前提。
    * **按时间或字数攒批**。每片都发等于让模型等广播；一次调用可能有几百片。
    * **不写事件表**。见模块开头的理由。
    """

    def __init__(self, task_id: str, node: str) -> None:
        self.task_id = task_id
        self.node = node
        self.chunks = 0
        self.chars = 0
        self.first_delta_ms = 0
        self._text = ""
        self._started = time.monotonic()
        self._sent_at = 0.0
        self._sent_chars = 0
        self._closed = False

    async def __call__(self, piece: str) -> None:
        """适配器每收到一段文字就调一次（签名 async 是为了背压口径一致）。"""
        if not piece:
            return
        if self.chunks == 0:
            # 首字延迟是这个特性最直接的成效指标：用户感觉"卡不卡"全看这个数
            self.first_delta_ms = max(1, int((time.monotonic() - self._started) * 1000))
        self.chunks += 1
        self.chars += len(piece)
        self._text += piece
        now = time.monotonic()
        if self.chars - self._sent_chars < FLUSH_CHARS and now - self._sent_at < FLUSH_SECONDS:
            return
        self._flush(done=False)

    def close(self) -> None:
        """收尾：补一帧 done，让界面知道这批文字到此为止。"""
        if self._closed:
            return
        self._closed = True
        if self.chars:
            self._flush(done=True)

    def metrics(self) -> dict:
        return {
            "chunks": self.chunks,
            "chars": self.chars,
            "first_delta_ms": self.first_delta_ms,
        }

    def _flush(self, done: bool) -> None:
        tail = self._text[-TAIL_CHARS:]
        LiveFeed.publish(
            self.task_id,
            {
                "task_id": self.task_id,
                "node": self.node,
                "text": tail,
                "chars": self.chars,
                "truncated": len(self._text) > len(tail),
                "done": done,
            },
        )
        self._sent_chars = self.chars
        self._sent_at = time.monotonic()
