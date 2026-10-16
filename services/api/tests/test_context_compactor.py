"""模型参与的五要素上下文压缩。

历史超预算时必须由模型把滑出窗口的旧消息折成摘要，而不是简单丢弃；
同一批消息不得重复折叠；摘要模型挂掉时必须退回确定性截断，不能把任务本身拖垮。
"""

from blank_api.services.agent.context import ContextCompactor, compact_messages, render_summary


class FakeSummarizer:
    def __init__(self) -> None:
        self.folds: list[list[str]] = []
        self.should_fail = False

    async def __call__(self, folded, previous: str) -> str:
        if self.should_fail:
            raise RuntimeError("summarizer unavailable")
        self.folds.append([str(item.get("content") or "") for item in folded])
        return render_summary(
            {
                "goal": "目标",
                "constraints": ["只能改工作区内文件"],
                "done": [f"已折叠第 {len(self.folds)} 批历史"],
                "failures": ["上次测试失败：断言不符"],
                "open": ["仍未补齐的验收条件"],
            }
        )


def history_messages(count: int, size: int = 400) -> list[dict]:
    return [
        {"role": "system", "content": "系统规则"},
        {"role": "user", "content": "任务目标"},
        *({"role": "tool", "content": f"结果 {index} " + "x" * size} for index in range(count)),
    ]


async def test_under_budget_keeps_history_and_skips_the_model():
    summarize = FakeSummarizer()
    compactor = ContextCompactor(max_chars=10_000)

    built = await compactor.build(history_messages(3), summarize)

    assert summarize.folds == []
    assert compactor.covered == 0
    assert [item["content"] for item in built[:2]] == ["系统规则", "任务目标"]
    assert len(built) == 5


async def test_over_budget_folds_history_into_five_section_summary():
    summarize = FakeSummarizer()
    compactor = ContextCompactor(max_chars=3_000)

    built = await compactor.build(history_messages(20), summarize)

    assert summarize.folds, "超出预算必须触发一次模型摘要"
    assert compactor.covered > 0
    assert built[0]["content"] == "系统规则"
    assert built[1]["content"] == "任务目标"
    summary = built[2]["content"]
    assert "执行历史摘要" in summary
    assert "失败与原因" in summary
    assert "未完成" in summary or "未补齐" in summary


async def test_summary_window_is_bounded():
    summarize = FakeSummarizer()
    compactor = ContextCompactor(max_chars=4_000)

    built = await compactor.build(history_messages(30), summarize)

    assert sum(len(str(item["content"])) for item in built) <= 4_000


async def test_same_messages_are_not_folded_twice():
    summarize = FakeSummarizer()
    compactor = ContextCompactor(max_chars=3_000)

    await compactor.build(history_messages(20), summarize)
    folds_after_first = len(summarize.folds)

    await compactor.build(history_messages(20), summarize)
    assert len(summarize.folds) == folds_after_first, "同一批消息不应重复折叠"

    await compactor.build(history_messages(40), summarize)
    assert len(summarize.folds) > folds_after_first, "窗口再次滑动后才折叠新增部分"


async def test_summarizer_failure_falls_back_to_deterministic_truncation():
    summarize = FakeSummarizer()
    summarize.should_fail = True
    compactor = ContextCompactor(max_chars=3_000)

    built = await compactor.build(history_messages(20), summarize)

    assert compactor.covered == 0
    assert compactor.fold_failures == 1
    assert any("上下文已压缩" in str(item["content"]) for item in built)


def test_render_summary_omits_empty_sections():
    text = render_summary({"goal": "目标", "constraints": [], "done": [], "failures": [], "open": []})
    assert "目标" in text
    assert "约束" not in text


def test_deterministic_fallback_still_preserves_goal_and_tail():
    compacted = compact_messages(history_messages(20), max_chars=500)
    contents = [item["content"] for item in compacted]
    assert contents[0] == "系统规则"
    assert "任务目标" in contents
    assert any("上下文已压缩" in item for item in contents)
