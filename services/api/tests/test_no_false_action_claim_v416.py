"""交付正文不许说没发生的事（卡 2 第二半：对账）。

真机形状（3622e0e7）：`rm -rf` 被独立审查拒了，工具返回 denied，
而模型在结论里写"已处理"，界面照抄这一句——人看到的和发生的不是一件事。

判据只有一条：**这一轮没有任何成功的写盘或命令执行**时，正文里的「已…」
必须被一句客观事实顶在前面；只要有任意一次成功动手，就一个字都不改。
"""

from blank_api.services.agent.graph import NO_ACTION_FACT, _enforce_no_false_claim


def _denied_shell() -> dict:
    return {
        "tool": "run_shell",
        "status": "denied",
        "ok": False,
        "output": "",
    }


def _ok_write() -> dict:
    return {"tool": "write_file", "status": "ok", "ok": True, "output": "written"}


def test_a_denied_action_is_not_reported_as_done() -> None:
    report = "结论\n- 已删除 build 目录，工作区已清理。"
    out = _enforce_no_false_claim(report, {"tool_results": [_denied_shell()]})
    assert out.startswith(NO_ACTION_FACT), "动作被拒却照抄「已删除」：界面会替它说谎"
    assert report in out, "原文要保留：改写字句等于替模型编第二份事实"


def test_a_successful_write_needs_no_guard() -> None:
    report = "结论\n- 已修改 3 个文件并跑了测试，全部通过。"
    assert _enforce_no_false_claim(report, {"tool_results": [_ok_write()]}) == report


def test_a_plain_answer_is_left_alone() -> None:
    """纯问答没有动作声称，不该被钉一句"你没动手"——那不是事实，是噪音。"""
    for report in ("你好。", "结论\n- 这个仓库用 FastAPI + Next.js。", ""):
        assert _enforce_no_false_claim(report, {"tool_results": []}) == report


def test_the_guard_does_not_stack() -> None:
    report = "结论\n- 已删除 build 目录。"
    once = _enforce_no_false_claim(report, {"tool_results": [_denied_shell()]})
    twice = _enforce_no_false_claim(once, {"tool_results": [_denied_shell()]})
    assert once == twice, "同一份正文被钉两句，读起来像两次失败"


def test_a_read_only_turn_is_not_an_action() -> None:
    """读文件成功不代表活干成了：只读工具不进"动手"这一列。"""
    rows = [{"tool": "read_file", "status": "ok", "ok": True}]
    report = "结论\n- 已修改配置。"
    out = _enforce_no_false_claim(report, {"tool_results": rows})
    assert out.startswith(NO_ACTION_FACT)
