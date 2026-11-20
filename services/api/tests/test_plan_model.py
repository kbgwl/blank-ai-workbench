

# ── 打回指定步骤（移植自 LoopForge 的人工闸门语义）─────────────────────────


def test_rewind_reopens_the_target_and_everything_after_it():
    from blank_api.services.plan import rewind_to_step

    steps = [
        {"id": "s1", "step": "调研", "status": "completed", "evidence": "读了 3 个文件"},
        {"id": "s2", "step": "实现", "status": "completed", "evidence": "改了 a.py"},
        {"id": "s3", "step": "测试", "status": "in_progress", "evidence": ""},
    ]
    rewound, index = rewind_to_step(steps, "s2")
    assert index == 1
    assert [s["status"] for s in rewound] == ["completed", "pending", "pending"]
    # 旧产物对应的证据要清掉：留着会让下一步以为输入已经就绪
    assert rewound[1]["evidence"] == ""


def test_rewind_keeps_earlier_steps_untouched():
    from blank_api.services.plan import rewind_to_step

    steps = [
        {"id": "s1", "step": "调研", "status": "completed", "evidence": "读完了"},
        {"id": "s2", "step": "实现", "status": "completed", "evidence": "改完了"},
    ]
    rewound, _ = rewind_to_step(steps, "s2")
    assert rewound[0] == steps[0]


def test_rewind_with_unknown_step_changes_nothing():
    from blank_api.services.plan import rewind_to_step

    steps = [{"id": "s1", "step": "调研", "status": "completed", "evidence": "读了"}]
    rewound, index = rewind_to_step(steps, "step-does-not-exist")
    assert index == -1
    assert rewound == steps


def test_rewind_with_empty_id_is_a_noop():
    from blank_api.services.plan import rewind_to_step

    steps = [{"id": "s1", "step": "调研", "status": "completed"}]
    rewound, index = rewind_to_step(steps, "")
    assert index == -1
    assert rewound == steps
