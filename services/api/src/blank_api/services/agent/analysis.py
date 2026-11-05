# -*- coding: utf-8 -*-
"""逆向实验室的独立分析工作区。

为什么要一个工作区之外的地方：逆向场景的产出是"对别人东西的分析笔记"，
它不该出现在被分析仓库的 diff 里。评审看到一次逆向任务，改动列表应该是空的——
分析报告不在那个仓库里，它在另一个目录。所以这里的根目录默认落在用户主目录下，
而不是 ``<workspace>/.blank/``：后者仍然算工作区里的新增文件，一次 ``git status``
就会把它当成改动。

目录按 task 分：同一个人在一天里分析三个样本，三份笔记不该混在一个文件夹里；
按 task 分也让"这次留下了什么"是查得到的，而不是靠文件名猜。
"""

from __future__ import annotations

import os
from pathlib import Path


def analysis_root(task_id: str) -> Path:
    """本次逆向任务的分析目录（不存在就建）。

    ``BLANK_ANALYSIS_DIR`` 可以改根位置（沙箱机、CI、或用户想放 D 盘）。
    拿不到可写目录时不抛错：返回一个"看起来对"的路径，写入会在工具层报错，
    比让整个任务起不来更容易解释。
    """
    base = os.environ.get("BLANK_ANALYSIS_DIR") or str(Path.home() / ".blank" / "analysis")
    safe = "".join(ch for ch in str(task_id or "") if ch.isalnum() or ch in "-_") or "task"
    path = Path(base) / safe
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        # 建不出来也把路径给出去：调用方随后写入会拿到具体的系统错误，
        # 那比"分析区不可用"这种笼统说法更容易定位。
        pass
    return path
