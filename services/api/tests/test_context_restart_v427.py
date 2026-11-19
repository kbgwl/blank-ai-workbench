"""上下文重置：长任务在原地打转时，拆掉会话、用交接单重建。

这条是所有借鉴里最大的一条，也是最容易搞坏的一条——所以它**默认关闭**，
判据先写死"什么时候值得重开"，以及"重开之后必须带什么"。

背景（真机任务 `36d86ed2`）：26 步、prompt 19K→83K，后 14 步没有任何新证据。
那不是窗口不够大，是上下文里全是模型自己刚写下的草稿。

判据分两层：

1. **该不该重开**：步数、上下文、无进展三条同时成立才算。少一条都不划算——
   有进展时重开会打断它；会话还小时重开等于白丢。
2. **重开带什么**：目标、改过的文件、**没验证过的文件**、最近一次失败、交接笔记。
   任何"顺带保留的历史"都会把腐烂一起搬过去，所以消息只有两条。
"""

from __future__ import annotations

from blank_api.services.agent.restart import (
    RESTART_IDLE_STEPS,
    RESTART_MIN_STEPS,
    RESTART_MIN_TOKENS,
    build_restart_messages,
    should_restart,
    summarize_changed,
)
from blank_api.services.features import FEATURES, is_enabled


def test_short_sessions_are_never_restarted():
    """才跑了十步就拆会话，等于把已有的进展扔掉重来。"""
    assert should_restart(steps=RESTART_MIN_STEPS - 1, tokens=RESTART_MIN_TOKENS * 2, idle_steps=99) == ""


def test_small_context_is_never_restarted():
    assert should_restart(steps=RESTART_MIN_STEPS * 2, tokens=RESTART_MIN_TOKENS - 1, idle_steps=99) == ""


def test_progress_blocks_the_restart():
    """有进展时重开是打断，不是救援。"""
    assert should_restart(steps=99, tokens=999_999, idle_steps=RESTART_IDLE_STEPS - 1) == ""


def test_all_three_conditions_together_give_a_reason():
    reason = should_restart(steps=RESTART_MIN_STEPS, tokens=RESTART_MIN_TOKENS, idle_steps=RESTART_IDLE_STEPS)

    assert reason, "三条都成立却不重开，这条机制等于没接上"


def test_the_handoff_carries_what_a_new_session_needs():
    messages = build_restart_messages(
        system="你是执行 Agent",
        goal="把 3D 页面做出来",
        changed={"js/gl.js": 6, "index.html": 1},
        unverified={"js/gl.js"},
        last_error="run_tests 超时",
        notes="上次卡在 WebGL 初始化",
    )

    assert [item["role"] for item in messages] == ["system", "user"]
    body = messages[1]["content"]
    for must in ("把 3D 页面做出来", "js/gl.js", "还没有验证过", "run_tests 超时", "WebGL 初始化"):
        assert must in body, f"交接单漏了「{must}」，新会话只能重新踩一遍"
    assert "不要复述" in body


def test_the_handoff_drops_history_instead_of_carrying_it():
    """核心不是"压缩得更好"，是**不带旧历史**。"""
    messages = build_restart_messages(system="s", goal="g", changed={}, unverified=set())

    assert len(messages) == 2
    assert all(len(str(item.get("content"))) < 3_000 for item in messages)


def test_the_changed_list_is_capped():
    writes = {f"f{index}.js": index for index in range(20)}

    assert len(summarize_changed(writes)) == 8
    assert list(summarize_changed(writes))[0] == "f19.js", "按改的次数排序，最该看的排最前"


def test_the_feature_is_registered_and_off_by_default():
    flag = FEATURES.get("context_restart")

    assert flag is not None, "开了却没登记，设置页里根本找不到这个开关"
    assert flag.default is False, "默认打开太激进：先拿同一批任务对比过再开"
    assert is_enabled("context_restart") is False
