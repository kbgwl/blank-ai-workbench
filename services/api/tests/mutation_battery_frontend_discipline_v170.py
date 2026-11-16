"""事故台题五条前端纪律的变异电池（#402）。

装置自检与 ``mutation_battery_workspace_state_v169`` 同口径：
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
PROMPT_TESTS = "tests/test_frontend_discipline_v170.py"

FOCUS_RULE = (
    '    "· 重渲染列表先想焦点与键盘：整块重建（`innerHTML=\'\'` 再 append）会把焦点扔回 body，"\n'
    '    "滚动或筛选触发的重渲染甚至让键盘用户按 Enter 打不开详情。要么只更新变化的节点保留原连接，"\n'
    '    "要么重建后按 roving tabindex 把焦点、选中项、滚动位置恢复回去；鼠标点击与键盘操作"\n'
    '    "必须走同一条打开路径。\\n"\n'
)
WINDOW_RULE = (
    '    "· 长列表做窗口化（只渲染可见行）时，上下都要有占位元素撑起真实总高度：滚动条长度要"\n'
    '    "对得上总行数，能滚到真实最后一条。只留尾部 spacer 会让比例缩短、滚不到底，"\n'
    '    "用户会以为数据丢了。\\n"\n'
)
DERIVED_RULE = (
    '    "· 改了数据之后，所有派生视图一起重算：当前筛选结果、结果计数、空状态、分页与排序。"\n'
    '    "批量操作最容易漏——「只看未解决」的视图里还留着刚标记解决的记录，计数也不对；"\n'
    '    "同一份状态只保留一个来源，别让视图与数据各说各话。\\n"\n'
)
PERSIST_RULE = (
    '    "· 视图状态（筛选、搜索、排序、分页）每次变化都要落盘：写 URL hash 或 localStorage，"\n'
    '    "并且恢复时优先级一致。只在启动时写一次等于没写——刷新就回到默认视图，用户以为筛选丢了。\\n"\n'
)
BOOT_RULE = (
    '    "· 启动路径要能扛空数据：首次渲染前判断结果集是否为空（取 `view[0]` 前先判空），"\n'
    '    "空数据与空筛选结果都只是空状态，不是异常。事件绑定不要挂在可能抛异常的首屏渲染之后——"\n'
    '    "一个初始化异常会让整页按钮、输入、快捷键全部失联；先绑定再渲染，或两段各自兜底。\\n"\n'
)
ID_RULE = (
    '    "· 改 HTML 结构、id、class 时同步检索 JS/CSS 里的引用（`getElementById`、`querySelector`、"\n'
    '    "样式选择器），确认没有指向已改名的节点；改名后打开一次页面看控制台，"\n'
    '    "第一条 TypeError 往往就是某处还在取旧 id。\\n"\n'
)

# (label, old, new, 期望至少红的测试名片段)
MUTATIONS = [
    ("A_焦点恢复纪律被删", FOCUS_RULE, '    ""\n', ["test_rerender_must_restore_focus_and_keyboard"]),
    ("B_窗口化占位纪律被删", WINDOW_RULE, '    ""\n', ["test_windowed_list_needs_leading_and_trailing_spacers"]),
    ("C_派生视图重算纪律被删", DERIVED_RULE, '    ""\n', ["test_batch_actions_recompute_derived_views"]),
    ("D_视图状态落盘纪律被删", PERSIST_RULE, '    ""\n', ["test_view_state_is_persisted_on_every_change"]),
    ("E_空数据启动纪律被删", BOOT_RULE, '    ""\n', ["test_bootstrap_survives_empty_result_set"]),
    ("F_id 引用同步纪律被删", ID_RULE, '    ""\n', ["test_renamed_ids_must_be_found_in_js_and_css"]),
    (
        "G_输出样式不再替换编程纪律",
        '    body = _SYSTEM_HEAD + (CODING_INSTRUCTIONS if (not prompt or keep) else "") + _SYSTEM_TAIL\n',
        "    body = _SYSTEM_HEAD + CODING_INSTRUCTIONS + _SYSTEM_TAIL  # 变异：样式不再替换编程纪律\n",
        ["test_six_rules_follow_output_style_semantics"],
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
    baseline_targets = [PROMPT_TESTS]
    rc, output = _run_tests(baseline_targets)
    if rc != 0:
        print(f"[FAIL] 基线不是全绿，变异读数作废：{_red_names(output)}")
        return 1
    print("[OK] 基线全绿（1 个目标）")

    for label, old, new, expect in MUTATIONS:
        original_bytes = GRAPH.read_bytes()
        before = _sha(original_bytes)
        original = original_bytes.decode("utf-8")
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
        GRAPH.write_bytes(mutated)
        try:
            rc, output = _run_tests(baseline_targets)
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
            GRAPH.write_bytes(original_bytes)
            if _sha(GRAPH.read_bytes()) != before:
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
