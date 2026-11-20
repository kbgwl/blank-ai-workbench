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


def test_graph_wires_the_skip_reason_into_the_delivery():
    """全链路：state 里的跳过原因要真的走到交付文本里，不是只测纯函数。"""
    from blank_api.services.agent import graph

    state = {
        "validation": {"status": "skipped", "skipped_reason": "本机没有可用测试命令"},
        "tool_results": [{"ok": True, "data": {"path": "a.py", "diff": "@@ -1 +1 @@\n-a\n+b"}}],
    }
    text = graph._delivery_text("结论\n- 改好了", state, None)
    assert "未跑校验：本机没有可用测试命令" in text
    assert "验证：" not in text


# ── 校验与「最后一次改动」的先后 ───────────────────────────────────────────

_MUTATION = {"tool": "apply_patch", "ok": True, "status": "ok", "data": {"path": "b.py", "change_type": "modified"}}
_TEST_OK = {"tool": "run_tests", "ok": True, "status": "ok", "data": {"command": "pytest -q", "exit_code": 0}}


def test_validation_after_the_last_change_still_counts():
    from blank_api.services.agent import graph

    state = {
        "tool_results": [_MUTATION, _TEST_OK],
        "validation": {"status": "ok", "command": "pytest -q", "exit_code": 0, "covered_results": 2},
    }
    text = graph._delivery_text("结论\n- 改好了", state, None)
    assert "验证：pytest -q（退出码 0）" in text
    assert "未跑校验：" not in text


def test_changes_after_validation_invalidate_it():
    """先跑测试再编辑：那条校验不能代表最终状态。"""
    from blank_api.services.agent import graph

    state = {
        "tool_results": [_MUTATION, _TEST_OK, _MUTATION],
        "validation": {"status": "ok", "command": "pytest -q", "exit_code": 0, "covered_results": 2},
    }
    text = graph._delivery_text("结论\n- 改好了", state, None)
    assert "未跑校验：校验之后又改了代码，需要重跑" in text
    assert "验证：" not in text

    assert graph._delivery_facts("结论", state).verify_stale is True


def test_records_without_a_stamp_are_not_retroactively_invalidated():
    """老记录没有戳：不能因为缺字段就把历史交付全改写成交付未验证。"""
    from blank_api.services.agent import graph

    state = {
        "tool_results": [_MUTATION, _TEST_OK, _MUTATION],
        "validation": {"status": "ok", "command": "pytest -q", "exit_code": 0},
    }
    assert graph._changes_after_validation(dict(state["validation"]), state) is False


def test_verify_reports_which_files_it_covers():
    """序号答「过没过期」，文件集答「验的是什么」——两个都要有。"""
    from blank_api.services.agent import graph

    state = {
        "tool_results": [_MUTATION, _TEST_OK],
        "validation": {
            "status": "ok",
            "command": "pytest -q",
            "exit_code": 0,
            "covered_results": 2,
            "covered_files": ["a.py", "b.py"],
        },
    }
    payload = delivery.as_dict(graph._delivery_facts("结论", state))
    assert payload["verify"]["covers"] == ["a.py", "b.py"]

    # 过期时不给「覆盖了哪些文件」——那会让人以为验过当前状态。
    stale_state = {
        "tool_results": [_MUTATION, _TEST_OK, _MUTATION],
        "validation": state["validation"],
    }
    stale = delivery.as_dict(graph._delivery_facts("结论", stale_state))
    assert stale["verify"]["covers"] == []
    assert stale["verify"]["stale"] is True


# ── 两类缺口必须正交：stale ≠ missing ─────────────────────────────────────


def test_stale_is_not_counted_as_missing():
    """「跑了但作废」不能被统计成「从没跑过」——两类处置不同（重跑 vs 催跑）。"""
    facts = delivery.DeliveryFacts(completed="改好了", changed_files=1, verify_stale=True)
    payload = delivery.as_dict(facts)
    assert payload["verify"]["stale"] is True
    assert payload["verify"]["missing"] is False


# ── 覆盖判据：计数 + 内容指纹（shell 写与过滤后的下标）────────────────────


def test_a_shell_write_after_validation_invalidates_by_fingerprint(tmp_path):
    """shell 里的 sed -i / 格式化器不走文件工具结果，只能靠内容指纹发现。"""
    from blank_api.services.agent import graph

    target = tmp_path / "a.py"
    target.write_text("x = 1\n", encoding="utf-8")
    fingerprint = graph._fingerprints(str(tmp_path), ["a.py"])
    validation = {"status": "ok", "covered_mutations": 1, "covered_fingerprint": fingerprint}
    state = {"tool_results": [_MUTATION]}

    assert graph._changes_after_validation(validation, state, str(tmp_path)) is False

    target.write_text("x = 2\n", encoding="utf-8")  # 模拟 shell 改写
    assert graph._changes_after_validation(validation, state, str(tmp_path)) is True


def test_covering_count_survives_filtered_scratch_entries():
    """收尾前的 scratch 隔离会删条目：按计数判，下标前移不会造成误判。"""
    from blank_api.services.agent import graph

    validation = {"status": "ok", "covered_mutations": 1}
    filtered = [_MUTATION, _TEST_OK]  # 一条 scratch 写已被删掉，不计入两侧
    assert graph._changes_after_validation(validation, {"tool_results": filtered}) is False

    assert graph._changes_after_validation(
        validation, {"tool_results": [_MUTATION, _TEST_OK, _MUTATION]}
    ) is True


def test_legacy_index_stamp_still_works():
    """已落库的记录只有下标戳，交付时不能因此判成"没过期"。"""
    from blank_api.services.agent import graph

    legacy = {"status": "ok", "covered_results": 2}
    assert graph._changes_after_validation(legacy, {"tool_results": [_MUTATION, _TEST_OK]}) is False
    assert graph._changes_after_validation(
        legacy, {"tool_results": [_MUTATION, _TEST_OK, _MUTATION]}
    ) is True


def test_scratchpad_writes_do_not_count_on_either_side():
    """scratch 写两侧都不计：否则盖戳时算、判定时被删，计数下降会掩盖后续写入。"""
    from blank_api.services.agent import graph

    scratch = {
        "tool": "apply_patch",
        "ok": True,
        "status": "ok",
        "data": {"path": ".blank/scratchpad/selftest.py", "change_type": "created"},
    }
    assert graph._mutation_count([scratch]) == 0
    assert graph._mutation_count([_MUTATION, scratch]) == 1


def test_a_moved_scratch_script_is_not_a_late_change(tmp_path):
    """收尾把自测脚本移走，不是「校验之后又改了代码」。

    复评（第四轮）抓到的误报形态：盖戳时 _selftest.js 还在工作区根，收尾时被
    _quarantine_scratch 挪进 .blank/scratchpad，交付再取指纹拿到 missing，
    于是一次正常交付被标成「未跑校验」。
    """
    from blank_api.services.agent import graph

    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "_selftest.js").write_text("console.log(1)\n", encoding="utf-8")
    fingerprint = graph._fingerprints(str(tmp_path), ["a.py", "_selftest.js"])
    validation = {"status": "ok", "covered_mutations": 1, "covered_fingerprint": fingerprint}
    state = {"tool_results": [_MUTATION]}

    assert graph._changes_after_validation(validation, state, str(tmp_path)) is False

    (tmp_path / "_selftest.js").unlink()  # 被移出工作区（隔离/外移）
    assert graph._changes_after_validation(validation, state, str(tmp_path)) is False


def test_scratchpad_paths_never_enter_the_fingerprint(tmp_path):
    """两路判据同一口径：计数排除 scratch，指纹也要排除。"""
    from blank_api.services.agent import graph

    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / ".blank").mkdir()
    (tmp_path / ".blank" / "scratchpad").mkdir()
    (tmp_path / ".blank" / "scratchpad" / "selftest.py").write_text("print(1)\n", encoding="utf-8")

    fingerprint = graph._fingerprints(
        str(tmp_path), ["a.py", ".blank/scratchpad/selftest.py"]
    )
    assert set(fingerprint) == {"a.py"}
