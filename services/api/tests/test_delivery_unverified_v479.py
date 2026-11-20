"""v479：改了东西却没跑校验，交付里必须说出来，且不许伪造「验证：」行。

背景（better-harness 评测 2026-11-20）：真机会话里 46 个 episode 跑过校验，
但「校验落在最后一次改动之后」被确认的是 0 个，还有 1 个改完完全没跑。
旧口径下这种交付是**静默**的——「验证」行整行隐藏，等于把「没验证」谎报成
「无需验证」。这里锁住新口径：缺就明写，且不侵占验证行的前缀。
"""

from __future__ import annotations

from blank_api.services.agent import delivery


def _facts(**overrides):
    base = dict(completed="改好了", changed_files=1, files=("a.py",), diff_count=1)
    base.update(overrides)
    return delivery.DeliveryFacts(**base)


def test_changed_without_check_is_disclosed():
    text = delivery.render(_facts())
    assert "未跑校验：" in text
    # 不变量：没有真实退出码时，「验证：」前缀一个都不许出现。
    assert "验证：" not in text


def test_changed_with_real_check_keeps_the_verify_line():
    text = delivery.render(_facts(verify_command="pytest -q", verify_exit=1))
    assert "验证：pytest -q（退出码 1）" in text
    assert "未跑校验：" not in text


def test_no_change_no_check_says_nothing():
    text = delivery.render(_facts(changed_files=0, files=(), diff_count=0))
    assert "未跑校验：" not in text
    assert "验证：" not in text


def test_command_without_exit_code_counts_as_unverified():
    """只有命令、没有退出码 = 说跑了其实没跑。这是最容易伪装成"已验证"的形态。"""
    text = delivery.render(_facts(verify_command="pytest -q", verify_exit=None))
    assert "未跑校验：" in text
    assert "验证：" not in text


def test_structured_flag_matches_the_text():
    missing = delivery.as_dict(_facts())
    assert missing["verify"]["missing"] is True

    ran = delivery.as_dict(_facts(verify_command="pytest -q", verify_exit=0))
    assert ran["verify"]["missing"] is False

    untouched = delivery.as_dict(_facts(changed_files=0, files=(), diff_count=0))
    assert untouched["verify"]["missing"] is False


def test_skip_reason_becomes_the_reason_text():
    """跳过校验的具体原因要写进交付，不能只说"没跑"。"""
    text = delivery.render(_facts(verify_note="本机没有可用测试命令"))
    assert "未跑校验：本机没有可用测试命令" in text


def test_no_duplicate_note_when_delivery_already_says_it():
    """交付行已说过，收尾注记不许再补一句——同一件事出现两次会被读成两件事。"""
    from blank_api.services.agent import graph

    report = delivery.render(_facts())
    state = {"validation": {"status": "skipped", "skipped_reason": "本机没有可用测试命令"}}
    assert graph._validation_note(report, state) == report
