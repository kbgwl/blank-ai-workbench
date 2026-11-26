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

from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote

# 每条 finding 的机器码。封闭集合，改一处就要一起改：
# 界面按码选文案、`failure_reason()` 按码选句子、验收按码断言。
# 之前这三处都在拿**中文子串**反推（`"零改动" in item`），同一件事换个说法就
# 判不出来了——「目标要求改动…零改动（zero-change success）」和「…磁盘零改动（零交付）」
# 是两种完全不同的失败，靠子串只能挑到先出现的那个。码是判据，文本是给人看的。
FINDING_CODES = (
    "no_files_changed",       # 本轮没有任何文件改动（信息，不拦）
    "zero_change",            # 有写意图但磁盘零改动（硬违规）
    "expected_files_missed",  # 预期文件没全命中
    "tests_not_executed",     # 测试没有可采信的执行结果
    "tests_failed",           # 测试跑了但没通过
    "exit_code_nonzero",      # 退出码非 0
    "tool_refused",           # 工具调用被拒/未获批
    "approval_denied",        # 审批被驳回
    "false_claim",            # 交付文本假称测试通过（硬违规）
    "delivery_incomplete",    # 交付文本自述未完成（硬违规）
    "test_config_broken",     # 本轮把测试配置改坏（硬违规）
    "no_delivery_text",       # 没有最终交付文本
    "zero_delivery",          # 有验收标准、计划要求改，磁盘零改动（硬违规）
    "goal_unverifiable",      # 有验收标准但没有交付文本可核对
    "budget_exhausted",       # 预算耗尽
    "done_check_failed",      # done_when 里的可执行判据没过
)
# 会改判任务失败的码。verifier 自己用它划分"硬违规 / 只是记一笔"，
# 终态码取 detail 时也按它挑——一张 verdict 的 findings 里，第一个码常常是
# `no_files_changed` 这种信息码，直接取 codes[0] 会说错原因。
BLOCKING_CODES = frozenset(
    {"zero_change", "false_claim", "delivery_incomplete", "test_config_broken", "zero_delivery"}
)

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
    "delivery_matches_plan",
    "delivery_evidence",
)

# 交付正文里出现这些词，就等于声称「测试通过」。
_PASS_CLAIMS = ("测试通过", "测试已通过", "全部通过", "tests passed", "all tests pass", "验证通过")
# 工具结果里这些状态说明执行被拦过，可能越权或没真的落盘。
_REFUSED_STATUSES = frozenset({"denied", "blocked", "awaiting_approval", "refused"})
# 只列「成功就等于声称改了文件」的工具。run_shell 是命令执行，ls/echo/git status
# 都不落盘，把它算进来会把每一条只读命令都误判成零改动伪成功。
_WRITE_TOOLS = frozenset({"apply_patch", "edit_file", "write_file", "notebook_edit", "delete_file", "create_file"})
# 只有它留下的断言能回答「用户打开时看见什么」。别的工具的结果里没有 DOM。
_BROWSER_TOOLS = frozenset({"browser_check"})

# 四行交付（agent/delivery.py::render）里「完成：」自己说没做完，「需要你：」把烂摊子
# 交回用户手工恢复。这是产品固定格式、不是模型自由散文，所以判据敢收紧。
# v0.11.2 §7 真机：正文写「完成：页面未修好…需要你：从 .blank_backups 恢复」，
# 任务状态却判 succeeded；旧 no_false_claim 只抓「假称测试通过」，抓不到这种真话。
_INCOMPLETE_MARKS = (
    "未修好", "没修好", "未修复", "没有修复", "未完成", "没完成",
    "无法完成", "未解决", "没解决", "改坏了",
    # 真机 9eac5bed：交付写「完成：工作区仍为空，三个文件均未创建，功能尚未实现」，
    # 旧词表没有覆盖「未实现/仍为空」，整轮被判 succeeded 且零个文件。
    "未实现", "仍为空", "没有任何改动", "未做任何改动",
    # 真机 e08fc1db（2026-11-21 大文件写入验证）：交付写「完成：…正文约 6291 字，
    # 未达 9000 字门槛」，任务仍判 succeeded。数值门槛没达到也是没完成，不能只认
    # 叙事性的「未完成」。词表只收明确的“没达到”表述，不收裸「不足」——
    # 「不足已解决」这类句子会被误伤。
    "未达", "未达到", "未达标", "未满足", "不满足", "尚未达到",
    "还差", "少于", "字数不足", "长度不足", "内容不足", "未写够", "没写够",
    # 真机 7ce98397（2026-11-21 shopbench 复跑）：交付写「前端导航点击无反应，
    # 五个视图进不去」，任务仍判 succeeded。功能“不工作”也是没完成，不能只认
    # “未完成/未实现”这类叙事词。
    "无反应", "没反应", "进不去", "打不开", "无法打开", "不工作",
    "不显示", "未显示", "未生效", "没生效",
)
# 「需要你：」里要求用户**重做/恢复/修复工作**，才是没做完的证据。
# 清理验证残留、补装工具链这类收尾交接不算：真机 coding-012/028/035 测试退出码
# 都是 0、文件都改对了，只因写了「需要你：手动删除验证残留」被判失败——那些残留
# 是模型自证时留下的，删除被执行域不可逆闸门拦下，是闸门的正确行为，不是交付物。
_HANDOFF_MARKS = (
    "恢复", "重做", "重新落", "修复",
    # 同一条真机：交付写「需要你：补足正文至 9000 字以上」。这是把未完成的
    # 工作交回用户，不是清理残留；不收它，状态又会判 succeeded。
    "补足", "补全", "补齐", "补写", "补上", "继续补", "手动补",
    # 真机 7ce98397：「需要你：给 switchView 的 history.replaceState 包 try/catch，
    # 重跑浏览器点击确认视图切换」——把验证/修复工作交回用户，也是没做完。
    "重跑", "重新跑", "再跑一遍", "手动确认", "手工确认",
)
# 校验通道把「本轮把测试配置改坏」误写成 skipped 时用；见 graph.run_validation_node。
_UNTESTABLE_STATUSES = frozenset({"skipped", "error"})
# 这些 skipped 原因只说明「这次没跑」，不说明「本轮把配置改坏」：审批没过、
# 环境不可用、工具链缺失、只读/无改动。空字符串是升级前的旧结论——仍按
# 「动过测试文件才拦」处理，不能因为缺字段就回到旧误判。
_SKIPS_NEVER_A_BREAK = frozenset(
    {"readonly", "no_change", "awaiting_approval", "not_authorized",
     "env_unavailable", "toolchain_missing"}
)
# 测试文件/测试配置的判据只认文件名与目录名，不读内容：hidden 测试被移出后
# 工作区里本来就没有测试，只能靠「本轮动了哪个路径」这个事实。
_TEST_CONFIG_NAMES = frozenset(
    {"conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml",
     "package.json", "jest.config.js", "jest.config.ts", "vitest.config.js",
     "vitest.config.ts", "playwright.config.js", "playwright.config.ts",
     ".mocharc.json", ".mocharc.yml", "phpunit.xml"}
)
_TEST_DIR_NAMES = frozenset({"tests", "test", "__tests__", "spec", "specs"})


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


def _touches_test_artifact(path: str) -> bool:
    """这个路径是不是测试文件或测试配置（本轮动过它，才有资格说配置被改坏）。"""
    normalized = str(path or "").replace("\\", "/").strip()
    if not normalized:
        return False
    name = normalized.rsplit("/", 1)[-1].lower()
    if name.startswith("test_") or name.endswith(("_test.py", ".test.js", ".test.ts", ".spec.js", ".spec.ts")):
        return True
    if name in _TEST_CONFIG_NAMES:
        return True
    return any(part.lower() in _TEST_DIR_NAMES for part in normalized.split("/")[:-1])


def _broke_test_config(
    rows: list[dict[str, Any]], validation: dict[str, Any], paths: list[str]
) -> bool:
    """本轮本来能跑测试，改完却认不出测试命令 —— 配置被这一轮改坏了。

    真·环境没配（第一条测试就报「未识别到测试命令」）不算，那是 v0.9.1 明确
    不许改判成代码失败的情形。

    ``error``（命令还在但跑不起来了）沿用老判据：曾有一次 ok 的 run_tests +
    有文件改动。``skipped`` 本身不是证据——隐藏测试被移出工作区时 pytest 必然
    exit 5，本来就没什么可收集（2026-11-16 严格评测 coding-008 t2 实测：只改了
    fib.py，却被打成「配置被改坏」）；只有本轮确实动过测试文件/测试配置才算。
    审批没过、环境不可用这类 skipped 更不是证据，一律不拦。
    """
    status = str(validation.get("status") or "")
    if status not in _UNTESTABLE_STATUSES or not paths:
        return False
    if status == "skipped":
        if str(validation.get("skip_code") or "") in _SKIPS_NEVER_A_BREAK:
            return False
        return any(_touches_test_artifact(path) for path in paths)
    return any(
        str(row.get("tool") or "") == "run_tests"
        and str(row.get("status") or "") == "ok"
        for row in rows
    )


def _ran_command(rows: list[dict[str, Any]], wanted: str) -> tuple[str, str]:
    """本轮有没有跑过这条命令，结果如何。返回 (status, 证据)。

    只认**退出码为 0 且真的跑过**的那次：跑过但非零是 failed，没跑是 unknown。
    这两种绝不能混——"没跑"是没拿到结论，"跑了没过"是拿到了结论。
    """
    target = " ".join(str(wanted or "").split())
    if not target:
        return "unknown", "判据里没有写命令"
    for row in rows:
        data = row.get("data") if isinstance(row.get("data"), dict) else {}
        text = " ".join(str(data.get("command") or "").split())
        if not text or text != target:
            continue
        code = data.get("exit_code")
        if not isinstance(code, int) or isinstance(code, bool):
            return "unknown", f"跑过 {text}，但退出码未知"
        return ("passed" if code == 0 else "failed"), f"{text} 退出码 {code}"
    return "unknown", f"本轮没跑过 {target}"


def _same_page(url: str, wanted: str) -> bool:
    """回执里的 url 是 ``file:///D:/...`` 的绝对地址，判据里写的是工作区相对路径。

    只比尾部并让边界对齐到 ``/``：一次任务里通常只有一张 index.html，尾部比对足够，
    又不会因为盘符、大小写、百分号编码不同而假阴性。
    """
    left = unquote(str(url or "")).replace("\\", "/").rstrip("/").lower()
    right = unquote(str(wanted or "")).replace("\\", "/").rstrip("/").lower()
    while right.startswith("./"):
        right = right[2:]
    if not left or not right:
        return False
    if left == right:
        return True
    return left.endswith("/" + right) or right.endswith("/" + left)


def _page_asserted(
    rows: list[dict[str, Any]], page: str, selector: str, minimum: Any
) -> tuple[str, str]:
    """本轮 browser_check 有没有在指定页面上断言过这个选择器，结果如何。

    只读回执，不自己起浏览器——verifier 是纯函数，这条纪律不能破。
    三种结论严格区分：跑过且断言不成立 = failed；没跑过 = unknown；
    跑过这张页面但压根没断言这个选择器 = unknown（它验了别的，不等于验了这条）。
    """
    want = str(page or "").strip()
    want_selector = str(selector or "").strip()
    if not want or not want_selector:
        return "unknown", "判据里没写页面或选择器"
    try:
        need = int(minimum)
    except (TypeError, ValueError):
        need = 1
    seen_page = False
    for row in rows:
        if str(row.get("tool") or "") not in _BROWSER_TOOLS:
            continue
        data = row.get("data") if isinstance(row.get("data"), dict) else {}
        if not _same_page(str(data.get("url") or ""), want):
            continue
        seen_page = True
        for item in data.get("asserts") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("selector") or "").strip() != want_selector:
                continue
            count = item.get("count")
            if item.get("ok") is True:
                return "passed", f"{want} 上 {want_selector} 命中 {count} 个（要 ≥{need}）"
            return "failed", f"{want} 上 {want_selector} 只有 {count} 个（要 ≥{need}）"
    if seen_page:
        return "unknown", f"检查过 {want}，但没有断言 {want_selector}"
    return "unknown", f"本轮没打开过 {want}"


def run_done_checks(
    done_when: list[str], checks: list[Any], rows: list[dict[str, Any]], root: Any
) -> list[dict[str, Any]]:
    """逐条核对完成判据。没有可执行检查的条目记 `unverifiable`，不算失败。

    这是"什么算完成"第一次由**事实**回答，而不是由模型的交付文本回答。
    但第一版刻意**不设 blocking**：判据是模型自己写的，写歪了不该让一个好交付判死；
    先把"哪条过了、哪条没过"摆出来，跑几天看准了再考虑升级成拦。
    """
    out: list[dict[str, Any]] = []
    base = Path(root) if root else None
    for index, text in enumerate(done_when):
        check = checks[index] if index < len(checks) and isinstance(checks[index], dict) else {}
        kind = str(check.get("type") or "")
        if not kind:
            out.append({"text": text, "status": "unverifiable", "evidence": "这条没有可执行判据"})
            continue
        if kind == "command":
            status, evidence = _ran_command(rows, str(check.get("cmd") or ""))
            out.append({"text": text, "status": status, "evidence": evidence})
            continue
        if kind == "page":
            # 不依赖 root：它看的是 browser_check 的回执，不是磁盘。
            status, evidence = _page_asserted(
                rows,
                str(check.get("path") or ""),
                str(check.get("selector") or ""),
                check.get("min", 1),
            )
            out.append({"text": text, "status": status, "evidence": evidence})
            continue
        if base is None:
            out.append({"text": text, "status": "unknown", "evidence": "拿不到工作区根目录"})
            continue
        rel = str(check.get("path") or "").strip().lstrip("./")
        target = base / rel
        try:
            if not target.exists():
                out.append({"text": text, "status": "failed", "evidence": f"{rel} 不存在"})
                continue
            if kind == "path_exists":
                out.append({"text": text, "status": "passed", "evidence": f"{rel} 存在"})
                continue
            # file_min_chars：按 Unicode 字符数，不按字节。中文一个字算一个字，
            # 与用户说的「9000 字」同一口径。
            if kind == "file_min_chars":
                try:
                    need = int(check.get("min") or 0)
                except (TypeError, ValueError):
                    out.append(
                        {"text": text, "status": "unknown", "evidence": "判据里的 min 不是数字"}
                    )
                    continue
                body = target.read_text(encoding="utf-8", errors="strict")
                count = len(body)
                out.append(
                    {
                        "text": text,
                        "status": "passed" if count >= need else "failed",
                        "evidence": f"{rel} 共 {count} 字符（要 ≥{need}）",
                    }
                )
                continue
            # file_contains：只在文本文件上做，读不动就 unknown（二进制/编码问题不是"没写对"）
            body = target.read_text(encoding="utf-8", errors="strict")
            needle = str(check.get("text") or "")
            if needle and needle in body:
                out.append({"text": text, "status": "passed", "evidence": f"{rel} 含「{needle}」"})
            else:
                out.append({"text": text, "status": "failed", "evidence": f"{rel} 不含「{needle}」"})
        except OSError as error:
            out.append({"text": text, "status": "unknown", "evidence": f"{rel} 读不了：{error.__class__.__name__}"})
        except UnicodeDecodeError:
            out.append({"text": text, "status": "unknown", "evidence": f"{rel} 不是文本文件"})
    return out


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
    # 工作区根目录：核对 path_exists / file_contains 要读磁盘。传 None 时这类判据记 unknown，
    # 不判失败——拿不到目录是"没验成"，不是"没做到"。
    root: Any = None,
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
    codes: list[str] = []

    def note(code: str, text: str) -> None:
        """记一条 finding，同时记它的码。

        两样都要：只有文本时界面得拿中文去猜（猜错就说错），只有码时用户
        读到的是一堆 `zero_delivery`。码给机器，文本给人。
        """
        codes.append(code)
        findings.append(text)

    blocking = False

    if readonly or direct_answer:
        # 纯问答/只读调研没有交付物可验，硬拉去判「零改动伪成功」是误伤。
        return {
            "verdict": "not_applicable",
            "passed": True,
            "blocking": False,
            "checks": checks,
            "findings": [],
            "codes": [],
            "evidence": {"reason": "readonly_or_answer", "tool_results": len(rows)},
        }

    status = str(val.get("status") or "")
    passed_flag = val.get("passed")
    exit_code = val.get("exit_code")

    # ① 文件是否真实修改
    checks["files_changed"] = bool(paths)
    if not paths:
        note("no_files_changed", "本轮没有任何文件改动")
    write_intent = _successful_write_calls(rows)
    checks["not_zero_change"] = not (write_intent and not paths)
    if write_intent and not paths:
        note("zero_change", "目标要求改动且执行链有写意图，但磁盘上零改动（zero-change success）")
        blocking = True

    # ② 预期文件 / 多余文件
    if expected:
        checks["expected_files"] = all(any(str(p).endswith(str(e)) or str(e) in str(p) for p in paths) for e in expected)
        if not checks["expected_files"]:
            note("expected_files_missed", f"预期改动的文件未全部命中：{expected[:5]}")
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
        note("tests_not_executed", f"测试没有可采信的执行结果（validation.status={status or 'none'}）")
    elif status != "ok":
        note("tests_failed", f"测试未通过（validation.status={status}）")
    if status == "ok":
        checks["exit_code_zero"] = exit_code in (None, 0) and passed_flag is not False
        if not checks["exit_code_zero"]:
            note("exit_code_nonzero", f"测试退出码非 0：{exit_code}")
    else:
        checks["exit_code_zero"] = None

    # ④ 审批 / 越权
    refused = [row for row in rows if str(row.get("status") or "") in _REFUSED_STATUSES]
    denied_approvals = [item for item in approvals if item.get("decision") is False]
    checks["no_overreach"] = not (refused or denied_approvals)
    if refused:
        note("tool_refused", f"{len(refused)} 个工具调用被拒绝或未获批")
    if denied_approvals:
        note("approval_denied", f"{len(denied_approvals)} 次审批被驳回")

    # ⑤ 交付文本有没有假称测试通过
    checks["no_false_claim"] = not _false_claim(report, val)
    if not checks["no_false_claim"]:
        note("false_claim", "交付文本声称测试通过，但验证结果不支持")
        blocking = True

    # ⑥ 交付文本自己说没做完 / 要求用户手工恢复（v0.11.2 §7 真机缺陷）
    # 这是比「假称通过」更硬的事实：模型都写「页面未修好」了，状态不能是 succeeded。
    checks["delivery_says_done"] = not _delivery_is_incomplete(report)
    if not checks["delivery_says_done"]:
        note("delivery_incomplete", "交付文本自述未完成或要求用户手工恢复")
        blocking = True

    # ⑦ 本轮把测试配置改坏：曾有测试跑通，最后却没有可采信的测试结果。
    # 与 graph 的校验通道同源，但这里是独立的最后一道闸——预算耗尽、修复回路
    # 没机会跑时，也不能让任务带着「测试没跑成」判成功。
    checks["tests_still_runnable"] = not _broke_test_config(rows, val, paths)
    if not checks["tests_still_runnable"]:
        if status == "skipped":
            note("test_config_broken", "本轮改动了测试文件或测试配置，之后一条测试也没收集到（配置被改坏）")
        else:
            note("test_config_broken", "本轮改动前有测试可用，改完后测试命令无法识别（配置被改坏）")
        blocking = True

    # ⑧ 最终交付证据
    checks["delivery_evidence"] = bool(str(report or "").strip())
    if not checks["delivery_evidence"]:
        note("no_delivery_text", "没有最终交付文本")

    # ⑨ 目标是否完成：没有硬性违规则认为已达成；有明确验收标准时还得有交付文本可核对。
    # 这一项只记 finding、不设 blocking——「目标没完成」和「伪成功」是两件事。
    done_when = [
        str(item).strip() for item in ((goal_spec or {}).get("done_when") or []) if str(item).strip()
    ]
    # ⑨a 零交付：有验收标准、计划里也确实要求改文件，磁盘却一个文件都没动。
    # 只有「计划要求改动」才算硬事实——纯问答与「调研后判断不需要改」不在此列，
    # 否则会把正常回答判成失败。
    plan_edits = any(
        str(item.get("kind") or "") in ("edit", "repair")
        for item in ((plan or {}).get("steps") or [])
        if isinstance(item, dict)
    )
    checks["delivery_matches_plan"] = not (not paths and bool(done_when) and plan_edits)
    if not checks["delivery_matches_plan"]:
        note("zero_delivery", "目标有验收标准、计划要求改动，但磁盘零改动（零交付）")
        blocking = True
    has_delivery = bool(str(report or "").strip())
    checks["goal_met"] = not blocking and not budget_exhausted and (not done_when or has_delivery)
    if done_when and not has_delivery:
        note("goal_unverifiable", "目标有验收标准但没有交付文本，无法核对")
    if budget_exhausted:
        note("budget_exhausted", "预算耗尽，目标只完成到当前轮")

    # ⑩ 逐条核对 done_when（有可执行判据的那几条）。
    # 这里只摆事实、不改 verdict：判据是模型写的，写歪了不该把好交付判死。
    done_results = run_done_checks(
        done_when, list((goal_spec or {}).get("done_checks") or []), rows, root
    )
    checked = [item for item in done_results if item["status"] in ("passed", "failed")]
    if checked:
        failed_items = [item for item in checked if item["status"] == "failed"]
        checks["done_when"] = not failed_items
        for item in failed_items:
            note("done_check_failed", f"判据没过：{item['text']}（{item['evidence']}）")

    verdict = "failed" if blocking else ("incomplete" if findings else "passed")
    return {
        "verdict": verdict,
        "passed": not blocking,
        "blocking": blocking,
        "checks": checks,
        "findings": findings[:12],
        # 与 findings 同序的机器码（不截断：12 条展示上限是排版问题，
        # 判据不该因为排版丢码）。老调用方不看这个字段，新调用方不该再读中文。
        "codes": codes,
        "evidence": {
            "done_when": done_results,
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
    """把 blocking 的 verdict 翻译成用户能看懂的一句话，不写过程。

    有 ``codes`` 就按码取句：码是判据，文本是可改的表述。升级前的 verdict
    只有 ``findings`` 文本，所以保留一条子串回退——但那只是给老数据用的，
    新产出不该再走它。
    """
    if not verdict or not verdict.get("blocking"):
        return ""
    findings = [str(item) for item in verdict.get("findings") or []]
    codes = [str(item) for item in verdict.get("codes") or [] if str(item)]
    if codes:
        for code, text in _BLOCKING_REASON_TEXT:
            if code in codes:
                return text
        return _BLOCKING_REASON_TEXT[-1][1]
    if any("零改动" in item for item in findings):
        return "目标要求改动，但工作区没有任何文件变化"
    if any("假称" in item or "声称测试" in item for item in findings):
        return "交付文本声称测试通过，实际验证结果不支持"
    if any("自述未完成" in item or "手工恢复" in item for item in findings):
        return "交付文本自述未完成、需要用户接手，任务不能判成功"
    if any("配置被改坏" in item or "测试命令无法识别" in item for item in findings):
        return "本轮改动把测试配置弄坏了，最后没能跑出可采信的测试结果"
    return findings[0] if findings else "独立事实验证未通过"


# 按码取句，顺序即优先级：一句话里同时有多个硬违规时，说最需要用户先处理的那个。
_BLOCKING_REASON_TEXT = (
    ("zero_change", "目标要求改动，但工作区没有任何文件变化"),
    ("false_claim", "交付文本声称测试通过，实际验证结果不支持"),
    ("delivery_incomplete", "交付文本自述未完成、需要用户接手，任务不能判成功"),
    ("test_config_broken", "本轮改动把测试配置弄坏了，最后没能跑出可采信的测试结果"),
    ("zero_delivery", "目标有验收标准、计划要求改动，但工作区没有任何文件变化"),
    ("", "独立事实验证未通过"),
)
