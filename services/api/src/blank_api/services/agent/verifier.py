"""v165 §6 Verifier：独立于 Agent 判断的事实验证器。

「独立」在这里是硬约束：**不调用模型、不读 Agent 的自述、不查数据库**，
只吃调用方递过来的磁盘事实 / 工具结果 / 测试结论 / 审批记录 / 目标，
输出结构化 verdict。现有 ``review_result_node`` 是让同一个模型再审一遍自己的活，
那是「第二意见」，不是事实校验；两者并存，各写各的事件。

保守原则：只有**硬违规**才 ``blocking=True``（会改判任务失败）。
信息不足、没有测试配置、测试被驳回都只记 finding，不把一轮有效改动判成失败——
v0.9.1 实测里「已批准但显示失败」正是把环境问题和代码问题混为一谈造成的。
"""

from __future__ import annotations

from typing import Any, Iterable

CHECKS = (
    "goal_met",
    "files_changed",
    "expected_files",
    "no_extra_files",
    "tests_executed",
    "exit_code_zero",
    "diff_matches",
    "no_overreach",
    "no_false_claim",
    "delivery_says_done",
    "tests_still_runnable",
    "not_zero_change",
    "delivery_evidence",
)

# 交付正文里出现这些词，就等于声称「测试通过」。
_PASS_CLAIMS = ("测试通过", "测试已通过", "全部通过", "tests passed", "all tests pass", "验证通过")
# 工具结果里这些状态说明执行被拦过，可能越权或没真的落盘。
_REFUSED_STATUSES = frozenset({"denied", "blocked", "awaiting_approval", "refused"})
# 只列「成功就等于声称改了文件」的工具。run_shell 是命令执行，ls/echo/git status
# 都不落盘，把它算进来会把每一条只读命令都误判成零改动伪成功。
_WRITE_TOOLS = frozenset({"apply_patch", "write_file", "delete_file", "create_file"})

# 四行交付（agent/delivery.py::render）里「完成：」自己说没做完，「需要你：」把烂摊子
# 交回用户手工恢复。这是产品固定格式、不是模型自由散文，所以判据敢收紧。
# v0.11.2 §7 真机：正文写「完成：页面未修好…需要你：从 .blank_backups 恢复」，
# 任务状态却判 succeeded；旧 no_false_claim 只抓「假称测试通过」，抓不到这种真话。
_INCOMPLETE_MARKS = (
    "未修好", "没修好", "未修复", "没有修复", "未完成", "没完成",
    "无法完成", "未解决", "没解决", "改坏了",
)
_HANDOFF_MARKS = ("恢复", "重做", "重新落", "手工", "手动", "自行修复", "自己修")
# 校验通道把「本轮把测试配置改坏」误写成 skipped 时用；见 graph.run_validation_node。
_UNTESTABLE_STATUSES = frozenset({"skipped", "error"})


def _results(tool_results: Iterable[Any]) -> list[dict[str, Any]]:
    return [item for item in tool_results if isinstance(item, dict)]


def _changed(changed_paths: Iterable[Any]) -> list[str]:
    return [str(item).strip() for item in changed_paths if str(item).strip()]


def _successful_write_calls(rows: list[dict[str, Any]]) -> bool:
    """「零改动伪成功」只认一个事实：写工具真的成功返回了，磁盘却没变。

    不能拿目标文本里的「修改/新增」当写意图——验收条件可能明写「不修改任何文件」，
    也可能 Agent 调研后判断本来就不需要改。那属于「目标没完成」，交给验收与审查，
    不是 Verifier 该改判的「伪成功」。
    """
    return any(
        str(row.get("tool") or "") in _WRITE_TOOLS
        and str(row.get("status") or "") == "ok"
        and row.get("ok") is not False
        for row in rows
    )


def _false_claim(report: str, validation: dict[str, Any]) -> bool:
    if not report:
        return False
    low = report.lower()
    if not any(mark in low for mark in _PASS_CLAIMS):
        return False
    return validation.get("status") != "ok" or validation.get("passed") is not True


def _delivery_is_incomplete(report: str) -> bool:
    """交付正文自己说没完成，或要求用户手工接手恢复。

    只认四行格式的「完成：」「需要你：」两行：别的段落里顺口提一句「未修复的历史问题」
    不该把一轮有效改动判成失败。
    """
    completed = ""
    needs = ""
    for raw in str(report or "").splitlines():
        line = raw.strip()
        if line.startswith("完成："):
            completed = line[len("完成："):].strip()
        elif line.startswith("需要你："):
            needs = line[len("需要你："):].strip()
    if any(mark in completed for mark in _INCOMPLETE_MARKS):
        return True
    return bool(needs) and any(mark in needs for mark in _HANDOFF_MARKS)


def _broke_test_config(
    rows: list[dict[str, Any]], validation_status: str, paths: list[str]
) -> bool:
    """本轮本来能跑测试，改完却认不出测试命令 —— 配置被这一轮改坏了。

    真·环境没配（第一条测试就报「未识别到测试命令」）不算，那是 v0.9.1 明确
    不许改判成代码失败的情形。只有「已有一次 ok 的 run_tests + 有文件改动 +
    最后没有可采信的测试结果」三条同时成立才拦。
    """
    if validation_status not in _UNTESTABLE_STATUSES or not paths:
        return False
    return any(
        str(row.get("tool") or "") == "run_tests"
        and str(row.get("status") or "") == "ok"
        for row in rows
    )


def verify(
    *,
    goal_spec: dict[str, Any] | None = None,
    plan: dict[str, Any] | None = None,
    tool_results: Iterable[Any] = (),
    validation: dict[str, Any] | None = None,
    changed_paths: Iterable[Any] = (),
    report: str = "",
    approval_events: Iterable[Any] = (),
    expected_paths: Iterable[Any] = (),
    readonly: bool = False,
    direct_answer: bool = False,
    budget_exhausted: bool = False,
) -> dict[str, Any]:
    """产出 v165 §6.2 顺序的最终 verdict。

    返回形状稳定，字段只会增不会减：证据页、事件 payload、指标脚本都读它。
    """
    val = dict(validation or {})
    rows = _results(tool_results)
    paths = _changed(changed_paths)
    expected = _changed(expected_paths)
    approvals = [item for item in approval_events if isinstance(item, dict)]

    checks: dict[str, bool | None] = {name: None for name in CHECKS}
    findings: list[str] = []
    blocking = False

    if readonly or direct_answer:
        # 纯问答/只读调研没有交付物可验，硬拉去判「零改动伪成功」是误伤。
        return {
            "verdict": "not_applicable",
            "passed": True,
            "blocking": False,
            "checks": checks,
            "findings": [],
            "evidence": {"reason": "readonly_or_answer", "tool_results": len(rows)},
        }

    status = str(val.get("status") or "")
    passed_flag = val.get("passed")
    exit_code = val.get("exit_code")

    # ① 文件是否真实修改
    checks["files_changed"] = bool(paths)
    if not paths:
        findings.append("本轮没有任何文件改动")
    write_intent = _successful_write_calls(rows)
    checks["not_zero_change"] = not (write_intent and not paths)
    if write_intent and not paths:
        findings.append("目标要求改动且执行链有写意图，但磁盘上零改动（zero-change success）")
        blocking = True

    # ② 预期文件 / 多余文件
    if expected:
        checks["expected_files"] = all(any(str(p).endswith(str(e)) or str(e) in str(p) for p in paths) for e in expected)
        if not checks["expected_files"]:
            findings.append(f"预期改动的文件未全部命中：{expected[:5]}")
    else:
        checks["expected_files"] = None
    if plan and plan.get("steps"):
        checks["diff_matches"] = bool(paths) or not any(
            item.get("kind") in ("edit", "repair") for item in plan["steps"]
        )
    else:
        checks["diff_matches"] = None
    checks["no_extra_files"] = True  # 没有预期清单时不做「多余文件」推断，避免误判
    if not paths and expected:
        checks["no_extra_files"] = None

    # ③ 测试是否真的执行、退出码是否为 0
    checks["tests_executed"] = status in ("ok", "failed", "error", "timeout")
    if status in ("skipped", "awaiting_approval", "denied", ""):
        findings.append(f"测试没有可采信的执行结果（validation.status={status or 'none'}）")
    elif status != "ok":
        findings.append(f"测试未通过（validation.status={status}）")
    if status == "ok":
        checks["exit_code_zero"] = exit_code in (None, 0) and passed_flag is not False
        if not checks["exit_code_zero"]:
            findings.append(f"测试退出码非 0：{exit_code}")
    else:
        checks["exit_code_zero"] = None

    # ④ 审批 / 越权
    refused = [row for row in rows if str(row.get("status") or "") in _REFUSED_STATUSES]
    denied_approvals = [item for item in approvals if item.get("decision") is False]
    checks["no_overreach"] = not (refused or denied_approvals)
    if refused:
        findings.append(f"{len(refused)} 个工具调用被拒绝或未获批")
    if denied_approvals:
        findings.append(f"{len(denied_approvals)} 次审批被驳回")

    # ⑤ 交付文本有没有假称测试通过
    checks["no_false_claim"] = not _false_claim(report, val)
    if not checks["no_false_claim"]:
        findings.append("交付文本声称测试通过，但验证结果不支持")
        blocking = True

    # ⑥ 交付文本自己说没做完 / 要求用户手工恢复（v0.11.2 §7 真机缺陷）
    # 这是比「假称通过」更硬的事实：模型都写「页面未修好」了，状态不能是 succeeded。
    checks["delivery_says_done"] = not _delivery_is_incomplete(report)
    if not checks["delivery_says_done"]:
        findings.append("交付文本自述未完成或要求用户手工恢复")
        blocking = True

    # ⑦ 本轮把测试配置改坏：曾有测试跑通，最后却没有可采信的测试结果。
    # 与 graph 的校验通道同源，但这里是独立的最后一道闸——预算耗尽、修复回路
    # 没机会跑时，也不能让任务带着「测试没跑成」判成功。
    checks["tests_still_runnable"] = not _broke_test_config(rows, status, paths)
    if not checks["tests_still_runnable"]:
        findings.append("本轮改动前有测试可用，改完后测试命令无法识别（配置被改坏）")
        blocking = True

    # ⑧ 最终交付证据
    checks["delivery_evidence"] = bool(str(report or "").strip())
    if not checks["delivery_evidence"]:
        findings.append("没有最终交付文本")

    # ⑨ 目标是否完成：没有硬性违规则认为已达成；有明确验收标准时还得有交付文本可核对。
    # 这一项只记 finding、不设 blocking——「目标没完成」和「伪成功」是两件事。
    done_when = [
        str(item).strip() for item in ((goal_spec or {}).get("done_when") or []) if str(item).strip()
    ]
    has_delivery = bool(str(report or "").strip())
    checks["goal_met"] = not blocking and not budget_exhausted and (not done_when or has_delivery)
    if done_when and not has_delivery:
        findings.append("目标有验收标准但没有交付文本，无法核对")
    if budget_exhausted:
        findings.append("预算耗尽，目标只完成到当前轮")

    verdict = "failed" if blocking else ("incomplete" if findings else "passed")
    return {
        "verdict": verdict,
        "passed": not blocking,
        "blocking": blocking,
        "checks": checks,
        "findings": findings[:12],
        "evidence": {
            "changed_paths": paths[:20],
            "changed_count": len(paths),
            "tool_results": len(rows),
            "write_intent": write_intent,
            "validation_status": status,
            "exit_code": exit_code,
            "approval_points": (plan or {}).get("approval_points", []),
            "plan_revision": (plan or {}).get("revision", 0),
            "report_chars": len(str(report or "")),
        },
    }


def failure_reason(verdict: dict[str, Any] | None) -> str:
    """把 blocking 的 verdict 翻译成用户能看懂的一句话，不写过程。"""
    if not verdict or not verdict.get("blocking"):
        return ""
    findings = [str(item) for item in verdict.get("findings") or []]
    if any("零改动" in item for item in findings):
        return "目标要求改动，但工作区没有任何文件变化"
    if any("假称" in item or "声称测试" in item for item in findings):
        return "交付文本声称测试通过，实际验证结果不支持"
    if any("自述未完成" in item or "手工恢复" in item for item in findings):
        return "交付文本自述未完成、需要用户接手，任务不能判成功"
    if any("配置被改坏" in item or "测试命令无法识别" in item for item in findings):
        return "本轮改动把测试配置弄坏了，最后没能跑出可采信的测试结果"
    return findings[0] if findings else "独立事实验证未通过"
