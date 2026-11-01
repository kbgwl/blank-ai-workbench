"""进程内指标：只记事实，不做判断。

为什么要自己写一个而不是接 Prometheus：这是**单机桌面产品**，指标的唯一读者是
设置页和排障脚本，而且必须能在断网、无外部依赖的情况下工作。这里给的是一张
可读的计数表，不是一套采集体系。

三条口径，写下来是为了以后不再争论：

* **计数器只增不减**，进程重启归零。跨重启的历史在事件表和用量表里，不在这里。
* **标签只允许有限取值**（错误码、供应商、结果），不做自由文本，否则这张表会长成
  日志——而日志不该被当成指标读。
* **不记任何用户数据**：不记 prompt、不记路径、不记参数正文。只记"发生了几次"。
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict

_LOCK = threading.Lock()
_COUNTERS: dict[str, float] = defaultdict(float)
_LABELED: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
_GAUGES: dict[str, float] = {}
_STARTED_AT = time.time()


def inc(name: str, value: float = 1.0, **labels: str) -> None:
    """计数器 +1。标签值一律转字符串并截断，避免把一整段报错变成标签。"""
    with _LOCK:
        if labels:
            key = (name, tuple(sorted((str(k), str(v)[:40]) for k, v in labels.items())))
            _LABELED[key] += float(value)
        else:
            _COUNTERS[name] += float(value)


def gauge(name: str, value: float) -> None:
    """瞬时值（连接数、队列长度）。下一次写入直接覆盖，不做累计。"""
    with _LOCK:
        _GAUGES[name] = float(value)


def counter(name: str) -> float:
    """某个指标的总次数：无标签的计数 + 该名字下所有标签分组之和。

    只返回无标签那一份会让"带着标签记的"看起来像没记过——`model.fallback` 就是
    按目标供应商打标签记的，读总数时应该把各组加起来。
    """
    with _LOCK:
        total = _COUNTERS.get(name, 0.0)
        total += sum(value for (key, _labels), value in _LABELED.items() if key == name)
        return total


def snapshot() -> dict:
    """导出当前读数。``by_label`` 只给有标签的计数——没有标签的那部分在 ``counters``。"""
    with _LOCK:
        counters = dict(_COUNTERS)
        gauges = dict(_GAUGES)
        labeled: dict[str, dict[str, float]] = defaultdict(dict)
        for (name, labels), value in _LABELED.items():
            key = ",".join(f"{k}={v}" for k, v in labels)
            labeled[name][key] = value
    return {
        "counters": counters,
        "counters_by_label": {name: dict(rows) for name, rows in labeled.items()},
        "gauges": gauges,
        "uptime_seconds": round(time.time() - _STARTED_AT, 1),
    }


def reset() -> None:
    """测试用：清空全部计数，避免用例之间互相污染。"""
    with _LOCK:
        _COUNTERS.clear()
        _LABELED.clear()
        _GAUGES.clear()


# —— 指标名集中在这里，避免各处手写字符串拼错（拼错不会报错，只会静默变成新指标）——
MODEL_TIMEOUT = "model.timeout"
MODEL_UNREACHABLE = "model.unreachable"
MODEL_RETRY = "model.retry"
MODEL_FALLBACK = "model.fallback"
MODEL_FALLBACK_FAILED = "model.fallback_failed"
WORKER_RECOVERED = "worker.recovered"
WORKER_ABANDONED = "worker.abandoned"
WORKER_RUN_FINISHED = "worker.run_finished"
SSE_CONNECTED = "sse.connected"
SSE_DISCONNECTED = "sse.disconnected"
SSE_POLL = "sse.db_polls"
SSE_WAKE = "sse.wakes"
ARTIFACT_WRITTEN = "artifact.written"
ARTIFACT_TRUNCATED = "artifact.truncated"
ARTIFACT_PURGED = "artifact.purged"
