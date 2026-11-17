"""运维控制台题六条前端纪律的变异电池。

与 v171 同口径：基线全绿、锚点恰好一次、变异非 no-op、字节级还原并复核 sha256。
每格把红集合精确比对到那一条 rule 判据；元测试（输出样式语义）是同一单点缺陷的
第二个观察面，单独断言必红并从精确集合里排除。
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

API = Path(__file__).resolve().parent.parent
PY = API / ".venv" / "Scripts" / "python.exe"
GRAPH = API / "src" / "blank_api" / "services" / "agent" / "graph.py"
PROMPT_TESTS = ["tests/test_frontend_discipline_v172.py"]
STYLE_TEST = "test_six_rules_follow_output_style_semantics"

HEIGHT_RULE = (
    '    "用户会以为数据丢了。滚动容器自己必须有确定高度（固定值，或视口内 `max-height` 加 "\n'
    '    "`overflow:auto`），窗口计算拿的是这个高度；让 spacer 把容器撑高，`clientHeight` 会变成"\n'
    '    "全部内容高度，5000 行一次性全渲染，帧率直接塌。验收看 DOM 行数远小于总行数、"\n'
    '    "滚到底能看到最后一条。\\n"\n'
)
HEIGHT_OLD = '    "用户会以为数据丢了。\\n"\n'
SCROLL_RULE = (
    '    "· 滚动的对象要找准：赋值 `scrollTop` 之前先确认该元素的 `scrollHeight` 大于 "\n'
    '    "`clientHeight`；内层容器不滚就说明真实滚动体是 document，`scrollTop` 写上去不会变化，"\n'
    '    "滚动监听也要挂在同一个滚动体上。先量再滚，滚完读回 `scrollTop` 与底部距离确认生效。\\n"\n'
)
SEED_RULE = (
    '    "· 演示/种子数据也要守题面的不变量：题面要求编号按时间递增，生成时时间步就必须恒定或"\n'
    '    "递增——每条随机 30–209 秒会让 id 顺序和时间顺序对不上，一点排序就露出首尾颠倒。"\n'
    '    "造完数据先按排序字段核对正序首尾两条，再交付。\\n"\n'
)
ROVING_RULE = (
    '"必须走同一条打开路径。roving tabindex 的初始值不能是 null：列表里至少有一行 "\n'
    '    "`tabindex=0`，否则 Tab 根本进不来，挂在列表上的 keydown（`if (!focusId) return`）"\n'
    '    "也就永远不会执行；focusin/pointerdown 都要登记当前行，焦点行被重渲染移除"\n'
    '    "（`focusout` 的 relatedTarget 为 null）时按 id 把焦点补回去。\\n"\n'
)
ROVING_OLD = '"必须走同一条打开路径。\\n"\n'
URL_RULE = (
    '    "并且恢复时优先级一致。只在启动时写一次等于没写——刷新就回到默认视图，用户以为筛选丢了。"\n'
    '    "URL 明确压过本地存档：hash 里没有某个键时也按默认值来，不能让上一次存的值悄悄赢；"\n'
    '    "要保留「默认」这个选择，就把默认值也写进 hash。\\n"\n'
)
URL_OLD = '    "并且恢复时优先级一致。只在启动时写一次等于没写——刷新就回到默认视图，用户以为筛选丢了。\\n"\n'
FALLBACK_RULE = (
    '    "· 降级/兜底层默认隐藏，显隐只走有 CSS 规则支撑的开关（`[hidden]`、"\n'
    '    "`.is-hidden{display:none}` 或内联样式）：给元素加一个样式表里不存在的类名，它照样以 "\n'
    '    "`display:block` 全屏叠在页面上。正常态与降级态各取一次 `display` 或各截一次图，"\n'
    '    "确认只有一层可见。\\n"\n'
)

MUTATIONS = [
    ("A_虚拟化容器定高纪律被删", HEIGHT_RULE, HEIGHT_OLD, ["test_virtualized_list_container_must_have_its_own_height"]),
    ("B_真实滚动体纪律被删", SCROLL_RULE, '    ""\n', ["test_scroll_must_target_the_real_scroller"]),
    ("C_种子数据单调纪律被删", SEED_RULE, '    ""\n', ["test_seed_data_must_keep_the_required_order"]),
    ("D_roving tabindex 初始可聚焦纪律被删", ROVING_RULE, ROVING_OLD, ["test_roving_tabindex_starts_from_a_tabbable_row"]),
    ("E_URL 压过存档纪律被删", URL_RULE, URL_OLD, ["test_url_state_outranks_the_local_snapshot"]),
    ("F_兜底层隐藏纪律被删", FALLBACK_RULE, '    ""\n', ["test_fallback_layer_is_hidden_by_a_real_rule"]),
]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _run() -> tuple[int, list[str]]:
    result = subprocess.run(
        [str(PY), "-m", "pytest", *PROMPT_TESTS, "-v", "--tb=no", "-q"],
        cwd=str(API),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    red = [
        line.split("::")[-1].split()[0]
        for line in (result.stdout + result.stderr).splitlines()
        if "FAILED" in line and "::" in line
    ]
    return result.returncode, red


def main() -> int:
    failures = 0
    original = GRAPH.read_bytes()
    before = _sha(original)
    text = original.decode("utf-8")

    rc, red = _run()
    if rc != 0:
        print(f"[FAIL] 基线不是全绿，读数作废：{red}")
        return 1
    print("[OK] 基线全绿")

    for label, old, new, expected in MUTATIONS:
        anchor = text.count(old)
        if anchor != 1:
            print(f"[FAIL] {label}：锚点出现 {anchor} 次（要求 1 次）")
            failures += 1
            continue
        mutated = text.replace(old, new, 1).encode("utf-8")
        if _sha(mutated) == before:
            print(f"[FAIL] {label}：变异是 no-op")
            failures += 1
            continue
        GRAPH.write_bytes(mutated)
        try:
            rc, red = _run()
        finally:
            GRAPH.write_bytes(original)
            if _sha(GRAPH.read_bytes()) != before:
                print(f"[FAIL] {label}：还原后 sha256 不一致")
                failures += 1
                continue
        style_red = STYLE_TEST in red
        rule_red = [name for name in red if name != STYLE_TEST]
        if not style_red:
            print(f"[FAIL] {label}：元测试没红，说明新纪律词条可能还有别处副本")
        if rule_red != expected:
            print(f"[FAIL] {label}：红集合不同源\n  期望 {expected}\n  实际 {rule_red}")
            failures += 1
            continue
        if not style_red:
            failures += 1
            continue
        print(f"[OK] {label}：如期只红 {rule_red}（元测试同源红）")

    rc, red = _run()
    if rc != 0:
        print(f"[FAIL] 还原后不绿：{red}")
        failures += 1
    else:
        print("[OK] 还原后全绿（自检通过）")

    total = len(MUTATIONS)
    print(f"\n变异电池：{total - failures}/{total} 条如期红")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
