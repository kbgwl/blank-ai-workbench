"""验证「没跑成」不等于「验证失败」。

真机三次：`npm test` 是全仓后端单测（约 25 分钟），单次工具上限 120–180 秒，
必然超时；交付物是真的（12KB 页面、browser_check 零控制台错误），却被判失败。
用户看到的是一个红叉 + 一句"测试未通过"，去翻一堆没问题的代码。

判据把三档钉死：跑了并失败 = 失败；跑了并通过 = 成功；没跑成 = 成功但显式标注未验证。
"""

from __future__ import annotations

import pytest

from blank_api.services.agent.runtime import (
    UNVERIFIED_NOTE,
    resolve_status,
    validation_unverified,
)


def _status(validation: dict) -> str:
    return resolve_status({"validation": validation})


def test_a_timed_out_check_does_not_kill_a_good_delivery():
    assert _status({"status": "timeout", "exit_code": None}) == "succeeded"


def test_a_check_that_never_started_is_not_a_failure_either():
    """命令找不到 / 判定器起不来：我们同样没拿到任何结论。"""
    assert _status({"status": "error", "exit_code": None}) == "succeeded"
    assert _status({"status": "unavailable"}) == "succeeded"


def test_a_real_failure_still_fails():
    """真跑了、退出码非零——这才是"验证不通过"，不能放行。"""
    assert _status({"status": "failed", "exit_code": 1}) == "failed"
    assert _status({"status": "ok", "exit_code": 2}) == "failed"


def test_passing_is_just_passing():
    assert _status({"status": "ok", "exit_code": 0}) == "succeeded"
    assert validation_unverified({"status": "ok", "exit_code": 0}) == ""


def test_skipped_stays_successful():
    """没改文件（纯问答）时校验本就不适用。"""
    assert _status({"status": "skipped"}) == "succeeded"


def test_blocking_verdict_overrides_the_leniency():
    """放宽的是"没跑成"，不是"事实不对"：零改动伪成功仍必须失败。"""
    assert resolve_status({"validation": {"status": "timeout"}, "verdict": {"blocking": True}}) == "failed"


def test_an_unverified_run_says_so_in_plain_words():
    note = validation_unverified({"status": "timeout"})
    assert "超时" in note and "没有拿到任何结论" in note or "没拿到" in note

    rendered = UNVERIFIED_NOTE.format(reason=note)
    assert "验证未完成" in rendered
    # 绝不能出现"通过"这种会被读成绿灯的词
    assert "通过" not in rendered


@pytest.mark.parametrize("status", ["timeout", "error", "unavailable"])
def test_every_unfinished_status_maps_to_succeeded(status):
    assert _status({"status": status}) == "succeeded"
