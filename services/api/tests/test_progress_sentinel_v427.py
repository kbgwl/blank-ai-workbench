"""进展哨兵：判「这一步有没有带来新信息」。

真机（任务 `36d86ed2`）里 26 步只有前 6 步在干活：
`js/gl.js` 被成功写入 6 次、同一处锚点改 3 次、同一条命令跑 9 次，
期间一次验证都没有——而每一次参数都不同，所以「同签名失败两次」那条断路器一条都没拦到。

判据盯的是三种**可核对的事实**：改过文件、验证通过、读到没见过的东西。
连续几步都没有这三种，或者同一份文件被反复重写却没验证，就提醒一次。

测试要钉住的边界比"能提醒"更重要：
* 正常节奏（改→验→改→验）**不许**提醒——提醒错一次就是在打断正确的工作；
* 提醒过要闭嘴（冷却），否则每步一句就变成噪音；
* 失败调用不算证据，但**读文件算**——"读代码定位"是正经进展，不能当空转。
"""

from __future__ import annotations

from blank_api.services.agent.progress import IDLE_LIMIT, NAG_COOLDOWN, REWRITE_LIMIT, ProgressSentinel


def _write(sentinel: ProgressSentinel, step: int, path: str = "js/gl.js") -> None:
    sentinel.record(step=step, tool="write_file", payload={"ok": True, "data": {"path": path}})


def _verify(sentinel: ProgressSentinel, step: int, tool: str = "browser_check") -> None:
    sentinel.record(step=step, tool=tool, payload={"ok": True, "data": {"asserts": []}})


def _read(sentinel: ProgressSentinel, step: int, text: str) -> None:
    sentinel.record(step=step, tool="read_file", payload={"ok": True, "output": text, "data": {"path": "a.js"}})


def _fail(sentinel: ProgressSentinel, step: int) -> None:
    sentinel.record(step=step, tool="run_shell", payload={"ok": False, "status": "failed", "output": "boom"})


def test_rewriting_the_same_file_without_verifying_gets_one_nudge():
    sentinel = ProgressSentinel()
    for step in (1, 2, 3):
        _write(sentinel, step)

    notes = sentinel.advise(4)

    assert len(notes) == 1
    assert "js/gl.js" in notes[0] and str(REWRITE_LIMIT) in notes[0]
    assert "browser_check" in notes[0], "提醒要给出下一步动作，只说「别这样」等于没说"


def test_the_nudge_does_not_repeat_every_step():
    sentinel = ProgressSentinel()
    for step in (1, 2, 3):
        _write(sentinel, step)
    assert sentinel.advise(4), "第一次该提醒"

    for step in range(5, 4 + NAG_COOLDOWN):
        repeat = [note for note in sentinel.advise(step) if "已经被成功写入" in note]
        assert repeat == [], "冷却期内不该反复念同一句"


def test_a_successful_verification_clears_the_debt():
    """改三次 + 验证通过一次 = 正常开发节奏，不是空转。"""
    sentinel = ProgressSentinel()
    for step in (1, 2, 3):
        _write(sentinel, step)
    _verify(sentinel, 4)

    assert sentinel.advise(5) == []
    assert sentinel.unverified == set()


def test_two_rewrites_are_still_normal_work():
    sentinel = ProgressSentinel()
    for step in (1, 2):
        _write(sentinel, step)

    assert sentinel.advise(3) == [], "改两次还是改-看-改的正常节奏，不该被念"


def test_four_steps_without_any_new_evidence_gets_a_nudge():
    sentinel = ProgressSentinel()
    sentinel.last_evidence = 1
    for step in range(2, 2 + IDLE_LIMIT):
        _fail(sentinel, step)

    notes = sentinel.advise(2 + IDLE_LIMIT - 1)

    assert any("没有产生任何新证据" in note for note in notes), notes


def test_reading_a_new_file_counts_as_progress():
    """「读代码定位」是正经进展：只看失败次数会把调试判成空转。"""
    sentinel = ProgressSentinel()
    sentinel.last_evidence = 1
    for index, step in enumerate(range(2, 2 + IDLE_LIMIT * 2)):
        _read(sentinel, step, f"内容片段 {index}")

    assert sentinel.advise(2 + IDLE_LIMIT * 2 - 1) == []


def test_reading_the_same_content_again_is_not_progress():
    sentinel = ProgressSentinel()
    sentinel.last_evidence = 1
    # 第一次读到这段内容算证据；之后反复读同一段就不再是了。
    _read(sentinel, 2, "同一段内容")
    for step in range(3, 3 + IDLE_LIMIT):
        _read(sentinel, step, "同一段内容")

    notes = sentinel.advise(3 + IDLE_LIMIT - 1)

    assert any("没有产生任何新证据" in note for note in notes), notes


def test_failed_calls_are_not_evidence():
    sentinel = ProgressSentinel()
    _fail(sentinel, 1)
    _fail(sentinel, 2)

    assert sentinel.last_evidence == 0


def test_a_failed_write_is_not_a_write():
    """写失败很常见（只读文件、路径不存在、内容被拒）。

    它既不该记进"重写次数"（否则同一份文件失败两次就触发重写提醒，而模型其实一次都没写进去），
    也不该算证据。这条断言必须用**会变更工作区**的工具来测：只用 run_shell 失败，
    去掉 ok 守卫也看不出差别——那样的断言拦不住真正的退化。
    """
    sentinel = ProgressSentinel()
    sentinel.record(step=1, tool="write_file", payload={"ok": False, "status": "failed", "data": {"path": "js/gl.js"}})
    sentinel.record(step=2, tool="edit_file", payload={"ok": False, "status": "failed", "data": {"path": "js/gl.js"}})

    assert sentinel.writes == {} and sentinel.unverified == set()
    assert sentinel.last_evidence == 0
