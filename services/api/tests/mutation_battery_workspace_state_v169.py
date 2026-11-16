"""玻璃题收场清理与前四条前端纪律的变异电池（#401）。

装置自检与 ``mutation_battery_empty_response_v395`` 同口径：
  1. 基线全绿；2. 锚点恰好出现一次；3. 写入后 sha256 必变（非 no-op）；
  4. 字节级还原并复核 sha256，还原后重新全绿。
每一格还要求「红项与变异同源」——只红别的测试等于判据没钉在实现上。
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

API = Path(__file__).resolve().parent.parent
PY = API / ".venv" / "Scripts" / "python.exe"
GRAPH = API / "src" / "blank_api" / "services" / "agent" / "graph.py"
RUNTIME = API / "src" / "blank_api" / "services" / "agent" / "runtime.py"
STATE = API / "src" / "blank_api" / "services" / "agent" / "workspace_state.py"
WORKSPACE_TESTS = "tests/test_workspace_state_externalize_v169.py"
PROMPT_TESTS = "tests/test_frontend_discipline_v169.py"

GLOBAL_SELECTOR_RULE = (
    '    "· 一条全局选择器能压掉一片功能：写 `body > *`、`*`、裸元素选择器这类大范围规则时，"\n'
    '    "先想它会不会盖掉组件自己的 position——`position:relative` 会把 fixed/sticky 元素"\n'
    '    "退化成普通流，粘顶导航、滚动进度条、回到顶部按钮会一起失效；z-index 与 display 同理。"\n'
    '    "这类规则要么收窄作用域，要么显式排除。\\n"\n'
)
HIDDEN_RULE = (
    '    "· 用 `hidden` 属性切换显隐的元素，样式里必须有 `[hidden]{display:none}`（需要压过"\n'
    '    "组件自身的 display 时加 `!important`），否则 `.item{display:flex}` 之类的规则会让"\n'
    '    "隐藏失效——命令面板过滤「输入了没反应」多半就是这个。\\n"\n'
)
RESPONSIVE_RULE = (
    '    "· 响应式按断点真验：窄屏（约 390px）下不许横向滚动、内容单列、正文字号不小于 14px；"\n'
    '    "徽标、脚注这类小字同样算，rem 先换算成 px 再判断。\\n"\n'
)
SELF_TEST_RULE = (
    '    "· 自测脚本先核实断言再信结论：报红时先确认脚本里的选择器、属性名、接口在实现里真的存在；"\n'
    '    "断言写错就改断言，不要为了一条错断言把本来正确的实现改坏；改不到通过就如实说明哪条没验过，"\n'
    '    "不要把半坏的状态交出去。\\n"\n'
)

# (label, path, old, new, 目标测试, 期望至少红的测试名片段)
MUTATIONS = [
    (
        "A_正常收尾不再外移状态",
        GRAPH,
        "    delivery_state = await _quarantine_scratch(state, session, node)\n"
        "    await _externalize_workspace_state(delivery_state, session, node)\n",
        "    delivery_state = await _quarantine_scratch(state, session, node)\n",
        [WORKSPACE_TESTS],
        ["test_summarize_result_externalizes_before_writing_delivery"],
    ),
    (
        "B_超时失败收尾不再外移状态",
        RUNTIME,
        '        if status in ("succeeded", "failed", "cancelled"):\n'
        "            await self._externalize_workspace_state(scope, status)\n",
        "        if False:  # 变异：终态兜底不再外移\n            pass\n",
        [WORKSPACE_TESTS],
        ["test_runtime_finish_externalizes_even_when_the_task_failed"],
    ),
    (
        "C_等待审批也清场",
        RUNTIME,
        '        if status in ("succeeded", "failed", "cancelled"):\n'
        "            await self._externalize_workspace_state(scope, status)\n",
        "        if True:  # 变异：非终态也清场\n            await self._externalize_workspace_state(scope, status)\n",
        [WORKSPACE_TESTS],
        ["test_waiting_approval_is_not_a_terminal_state"],
    ),
    (
        "D_整个备份目录一起收走",
        STATE,
        "    task_backups = backups / task\n",
        "    task_backups = backups  # 变异：别的任务备份也收走\n",
        [WORKSPACE_TESTS],
        ["test_other_tasks_backups_are_not_ours_to_move"],
    ),
    (
        "E_用户配置被当成临时目录",
        STATE,
        'SCRATCH_DIRS = (".blank/scratchpad", ".blank/context/scratchpad")',
        'SCRATCH_DIRS = (".blank",)  # 变异：整个 .blank 都收走',
        [WORKSPACE_TESTS],
        ["test_harness_state_leaves_the_delivery_dir_but_user_config_stays"],
    ),
    (
        "F_task_id 穿越防护被拔掉",
        STATE,
        "    task = _safe_component(task_id)\n",
        "    task = str(task_id)  # 变异：不再清洗路径分量\n",
        [WORKSPACE_TESTS],
        ["test_task_id_cannot_escape_the_state_root"],
    ),
    (
        "G_已有存档被覆盖",
        STATE,
        "    if not target.exists():\n        return target\n",
        "    if True:  # 变异：已存在也直接返回\n        return target\n",
        [WORKSPACE_TESTS],
        ["test_existing_archive_is_not_overwritten"],
    ),
    (
        "H_全局选择器纪律被删",
        GRAPH,
        GLOBAL_SELECTOR_RULE,
        '    ""\n',
        [PROMPT_TESTS],
        ["test_global_selector_must_not_clobber_positioning"],
    ),
    (
        "I_[hidden] 纪律被删",
        GRAPH,
        HIDDEN_RULE,
        '    ""\n',
        [PROMPT_TESTS],
        ["test_hidden_attribute_needs_display_guard"],
    ),
    (
        "J_窄屏真验纪律被删",
        GRAPH,
        RESPONSIVE_RULE,
        '    ""\n',
        [PROMPT_TESTS],
        ["test_responsive_rules_must_be_checked_at_the_breakpoint"],
    ),
    (
        "K_自测断言纪律被删",
        GRAPH,
        SELF_TEST_RULE,
        '    ""\n',
        [PROMPT_TESTS],
        ["test_self_test_selection_must_match_the_implementation"],
    ),
    (
        "L_输出样式不再替换编程纪律",
        GRAPH,
        '    body = _SYSTEM_HEAD + (CODING_INSTRUCTIONS if (not prompt or keep) else "") + _SYSTEM_TAIL\n',
        "    body = _SYSTEM_HEAD + CODING_INSTRUCTIONS + _SYSTEM_TAIL  # 变异：样式不再替换编程纪律\n",
        [PROMPT_TESTS],
        ["test_four_rules_follow_output_style_semantics"],
    ),
]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _run_tests(targets: list[str]) -> tuple[int, str]:
    result = subprocess.run(
        [str(PY), "-m", "pytest", *targets, "-v", "--tb=no", "-q"],
        cwd=str(API),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.returncode, result.stdout + result.stderr


def _red_names(output: str) -> list[str]:
    return [
        line.split("::")[-1].split()[0]
        for line in output.splitlines()
        if "FAILED" in line and "::" in line
    ]


def main() -> int:
    failures = 0

    baseline_targets = sorted({target for _, _, _, _, targets, _ in MUTATIONS for target in targets})
    rc, output = _run_tests(baseline_targets)
    if rc != 0:
        print(f"[FAIL] 基线不是全绿，变异读数作废：{_red_names(output)}")
        return 1
    print(f"[OK] 基线全绿（{len(baseline_targets)} 个目标）")

    for label, path, old, new, targets, expect in MUTATIONS:
        original_bytes = path.read_bytes()
        before = _sha(original_bytes)
        original = original_bytes.decode("utf-8")
        # runtime.py 是 CRLF，graph.py 是 LF：锚点按文件自身的换行符匹配。
        eol = "\r\n" if "\r\n" in original else "\n"
        old = old.replace("\n", eol)
        new = new.replace("\n", eol)
        anchor = original.count(old)
        if anchor != 1:
            print(f"[FAIL] {label}：锚点出现 {anchor} 次（要求恰好 1 次）")
            failures += 1
            continue
        mutated = original.replace(old, new, 1).encode("utf-8")
        if _sha(mutated) == before:
            print(f"[FAIL] {label}：变异是 no-op")
            failures += 1
            continue
        path.write_bytes(mutated)
        try:
            rc, output = _run_tests(targets)
            if rc == 0:
                print(f"[FAIL] {label}：变异后测试全绿，这条判据是装饰")
                failures += 1
                continue
            red = _red_names(output)
            if not any(fragment in name for name in red for fragment in expect):
                print(f"[FAIL] {label}：变异的红与期望不同源")
                print(f"  期望至少红: {expect}")
                print(f"  实际红: {red}")
                failures += 1
                continue
            print(f"[OK] {label}：如期红（{red}）")
        finally:
            path.write_bytes(original_bytes)
            if _sha(path.read_bytes()) != before:
                print(f"[FAIL] {label}：还原后 sha256 不一致")
                failures += 1

    rc, output = _run_tests(baseline_targets)
    if rc != 0:
        print(f"[FAIL] 还原后目标测试不绿，恢复不干净：{_red_names(output)}")
        failures += 1
    else:
        print("[OK] 还原后目标测试全绿（自检通过）")

    print(f"\n变异电池：{len(MUTATIONS) - failures}/{len(MUTATIONS)} 条变异如期红")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
