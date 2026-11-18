"""v398：工具结果的「看不全」必须说出来，而且当前这一步要看得够。

两条在上一版会同时出错，但错法不同：

1. **静默截断**。网关把回给模型的结果 `[:8000]` 一剪，既不告诉模型还剩多少，
   也不说怎么办。模型看到一段在句子中间断掉的 `git diff`，只能判断"命令没输出完"，
   于是重跑同一条命令——同样的输出、同样的钱、同样的结果。
2. **预算是按工具名一刀切**。`run_shell` 固定 1000+2500 字符，改三个文件的 diff
   就看不全；同时两千行 `pytest: N passed` 也照样占满同一份额度。前者是"该看的没看到"，
   后者是"不该看的花了钱"，一刀切两个都治不了。

这里钉住的是：截断要带字据、产物仍留全文、预算按内容形态分流。
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

from tests.test_agent_v04 import (  # noqa: F401
    env,
    create_project,
)

from blank_api.services.agent.payload import render
from blank_api.services.tools import build_default_tools
from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome
from blank_api.services.tools.gateway import MAX_RESULT_CHARS, ToolGateway, _truncation_note


def _context(root: Path, task_id: str) -> ToolContext:
    from blank_api.db import get_session_factory

    return ToolContext(root=root, task_id=task_id, session_factory=get_session_factory())


async def _big_result_tool(size: int) -> ToolDefinition:
    async def handler(context: ToolContext, arguments: dict) -> ToolOutcome:
        # 造一段"像 diff"的长输出：每行都不同，形态分散。
        body = "\n".join(f"+ line {index} of a fairly wide patch hunk {index * 7}" for index in range(size))
        return ToolOutcome(ok=True, output=body)

    return ToolDefinition(
        name="big_patch",
        description="返回一段很长的 diff",
        input_schema={"type": "object"},
        risk_level="low",
        handler=handler,
    )


# --------------------------------------------------------------------------- #
# 1. 截断必须带字据
# --------------------------------------------------------------------------- #
def test_truncation_note_says_how_much_is_missing_and_what_to_do():
    note = _truncation_note("run_shell", 40_000, 90_000)

    assert "90000" in note
    assert "40000" in note
    assert "还有 50000 未显示" in note
    # 要给出下一步，否则模型只能原样重跑（这正是它被加进来的原因）。
    assert "重定向" in note
    assert "不要因为没看到就重复执行" in note


def test_truncation_note_for_read_only_tools_points_at_narrowing():
    note = _truncation_note("search_code", 40_000, 41_000)

    assert "缩小范围重跑" in note


async def test_oversize_result_is_marked_and_artifact_keeps_the_full_text(env, workspace):  # noqa: F811
    project_id = await create_project(env, workspace)
    task_id = "task-v398-truncate"
    from blank_api.db import get_session_factory
    from blank_api.models.task import Task

    async with get_session_factory()() as session:
        session.add(Task(id=task_id, project_id=project_id, title="截断", goal="截断", max_steps=4))
        await session.commit()

    tool = await _big_result_tool(3_000)
    gateway = ToolGateway([tool])
    payload = await gateway.execute("big_patch", {}, _context(workspace, task_id), "act")

    text = payload["output"]
    assert len(text) <= MAX_RESULT_CHARS + len(_truncation_note("big_patch", 0, 0)) + 64
    assert text.startswith("+ line 0 of a fairly wide patch hunk 0")
    assert "结果已截断" in text
    assert payload["result_truncated"]["kept_chars"] == MAX_RESULT_CHARS
    assert payload["result_truncated"]["total_chars"] > MAX_RESULT_CHARS

    # 产物是「模型到底看没看全」唯一的反证材料：它必须留全文，不能被同一把剪刀剪短。
    artifact = Path(payload["artifact_path"] and _artifact_file(payload["artifact_path"]))
    assert artifact.is_file()
    stored = json.loads(gzip.open(artifact, "rt", encoding="utf-8").read())
    assert len(stored["output"]) > MAX_RESULT_CHARS * 2


def _artifact_file(reference: str) -> Path:
    from blank_api.services.agent.artifacts import ArtifactStore

    task_id, _, name = reference.partition("/")
    located = ArtifactStore().locate(task_id, name.split(".")[0])
    assert located is not None, f"产物没落盘：{reference}"
    return located


async def test_short_result_is_untouched(env, workspace):  # noqa: F811
    project_id = await create_project(env, workspace)
    task_id = "task-v398-short"
    from blank_api.db import get_session_factory
    from blank_api.models.task import Task

    async with get_session_factory()() as session:
        session.add(Task(id=task_id, project_id=project_id, title="短", goal="短", max_steps=4))
        await session.commit()

    tool = await _big_result_tool(3)
    gateway = ToolGateway([tool])
    payload = await gateway.execute("big_patch", {}, _context(workspace, task_id), "act")

    assert "结果已截断" not in payload["output"]
    assert "result_truncated" not in payload


# --------------------------------------------------------------------------- #
# 2. 预算按内容形态分流
# --------------------------------------------------------------------------- #
def _render(tool: str, output: str) -> tuple[str, dict]:
    return render(tool, {"ok": True, "status": "ok", "output": output})


def test_wide_diff_keeps_the_whole_change():
    """一次改动要一次看全：靠重跑换来的「看懂」是双倍价钱。

    这里钉的是一个**真实的中间档**：约 1 万字符、几十处改动。旧预算（1000+2500）
    正好把它剪掉一半，那半个 diff 模型看不见，只能重跑同一条 git diff。
    """
    diff = "\n".join(
        f"@@ -{index},7 +{index},9 @@ def handler_{index}(request):"
        f"\n-    return old_value_{index}\n+    return new_value_{index}  # 改动理由 {index}"
        for index in range(90)
    )
    assert 9_000 < len(diff) < MAX_RESULT_CHARS

    rendered, stats = _render("run_shell", diff)

    assert stats["saved"] == 0, "补丁不该被当成重复日志削掉"
    assert "return new_value_89" in rendered
    # 同类变化仍是逐行给出的，不是被摘要成一句。
    assert "return new_value_0" in rendered


def test_repetitive_log_is_still_collapsed():
    """两千行在说同一件事时，把它们全塞进上下文才是真正的浪费。"""
    noisy = "\n".join(f"pytest: {index} passed" for index in range(2_000))

    rendered, stats = _render("run_shell", noisy)

    assert stats["saved"] > len(noisy) * 0.9
    assert "中段省略" in rendered


def test_read_file_note_survives_the_budget_switch():
    """读文件被裁剪时仍要给「共几行 / 现在到第几行 / 下一步 offset」。"""
    body = "\n".join(f"line {index}: VALUE_{index} = {index}" for index in range(5_000))
    rendered, _ = render(
        "read_file",
        {"ok": True, "status": "ok", "output": body, "data": {"total_lines": 5_000, "start_line": 1}},
    )

    assert "offset=" in rendered
