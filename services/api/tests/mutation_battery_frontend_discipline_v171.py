"""五条前端纪律的变异电池。

装置自检与 ``mutation_battery_frontend_discipline_v170`` 同口径：
  1. 基线全绿；2. 锚点恰好出现一次；3. 写入后 sha256 必变（非 no-op）；
  4. 字节级还原并复核 sha256，还原后重新全绿。

比 v170 更窄的一处：每格把红集合**精确比对**到那一条判据，不用「包含即可」。
「输出样式语义」那条是内容存在性元测试，删掉任何一条新纪律都会连带它红——
这是同一个单点缺陷的两个观察面，因此在 rule 变异里单独断言它必红、并从
精确集合里排除；另有一格只改组合逻辑，要求它红而所有 rule 判据绿。
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

API = Path(__file__).resolve().parent.parent
PY = API / ".venv" / "Scripts" / "python.exe"
GRAPH = API / "src" / "blank_api" / "services" / "agent" / "graph.py"
PROMPT_TESTS = [
    "tests/test_frontend_discipline_v169.py",
    "tests/test_frontend_discipline_v170.py",
    "tests/test_frontend_discipline_v171.py",
]
STYLE_TEST = "test_five_rules_follow_output_style_semantics"

SHADER_RULE = (
    '    "· 用 WebGL / Canvas / 音频这类运行时初始化，失败要留证据：着色器编译后读 "\n'
    '    "`getShaderInfoLog`；片元着色器不能直接引用顶点着色器里的 attribute（要自己声明，"\n'
    '    "或经 varying 传入）。初始化失败不要只把画布 `display:none` 了事——在控制台说明原因，"\n'
    '    "并留一层静态背景兜底；首屏空白、画布 0×0 往往就是着色器编译失败被静默吞掉。\\n"\n'
)
RAF_RULE = (
    '    "· `requestAnimationFrame` 常驻循环的启动门控不能在循环外只判一次：第一次因"\n'
    '    "「指针还没动」「还没进入视口」而 early-return 的循环，必须由那个状态变化的事件重新启动，"\n'
    '    "否则动画永远停在第一帧。更稳的是循环一直跑，在循环内部按状态决定这一帧画不画。\\n"\n'
)
OVERFLOW_TAIL = (
    '    "徽标、脚注这类小字同样算，rem 先换算成 px 再判断。横滚先量再猜：拿 "\n'
    '    "`document.documentElement.scrollWidth` 对视口宽，确认溢出后对可疑区块逐个 `display:none` "\n'
    '    "二分定位。Grid 的 auto 轨道会被最宽内容按 max-content 撑破——容器写 "\n'
    '    "`grid-template-columns: minmax(0, 1fr)`，子项补 `min-width:0`。\\n"\n'
)
OVERFLOW_OLD_TAIL = '    "徽标、脚注这类小字同样算，rem 先换算成 px 再判断。\\n"\n'
DECOR_RULE = (
    '    "· 装饰层里的文字也算正文：`aria-hidden` 的漂浮标签、水印、角标只要文字可见可读，"\n'
    '    "就同样受 14px 下限约束，别因为它是装饰就写 0.78rem 糊过去。\\n"\n'
)
ENTRY_RULE = (
    '    "· 交付网页前按入口文件核对引用：`<script src>`、`<link href>`、图片与字体路径逐个确认"\n'
    '    "目标文件真在交付目录里；用静态服务器打开一次看控制台，缺文件的 404 会让整页脚本失联。"\n'
    '    "页面能打开不等于功能在——只交 index.html 和样式表、把 app.js 落下，就是最典型的残件。\\n"\n'
)

# (label, old, new, 期望精确红的 rule 判据（不含元测试）)
MUTATIONS = [
    ("A_WebGL 失败留证纪律被删", SHADER_RULE, '    ""\n', ["test_shader_failure_must_not_silently_hide_canvas"]),
    ("B_rAF 门控重启纪律被删", RAF_RULE, '    ""\n', ["test_raf_loop_must_restart_when_gate_changes"]),
    (
        "C_横滚实测与 grid 修法被删（保留旧 390px 句子）",
        OVERFLOW_TAIL,
        OVERFLOW_OLD_TAIL,
        ["test_mobile_overflow_is_measured_and_grid_tracks_are_bounded"],
    ),
    ("D_装饰层字号纪律被删", DECOR_RULE, '    ""\n', ["test_decorative_layer_text_still_counts_toward_min_font"]),
    ("E_入口引用核对纪律被删", ENTRY_RULE, '    ""\n', ["test_entry_html_references_must_exist_before_delivery"]),
]

# 组合逻辑变异：输出样式不再替换编程纪律。三个前端纪律文件各有一条元测试，
# 观察的是同一个单点缺陷，因此期望恰好这三条红、所有 rule 判据绿。
STYLE_MUTATION = (
    "F_输出样式不再替换编程纪律",
    '    body = _SYSTEM_HEAD + (CODING_INSTRUCTIONS if (not prompt or keep) else "") + _SYSTEM_TAIL\n',
    "    body = _SYSTEM_HEAD + CODING_INSTRUCTIONS + _SYSTEM_TAIL  # 变异：样式不再替换编程纪律\n",
)
STYLE_MUTATION_EXPECTED = {
    "test_four_rules_follow_output_style_semantics",
    "test_six_rules_follow_output_style_semantics",
    STYLE_TEST,
}


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


def _apply_and_run(original: str, before: str, label: str, old: str, new: str) -> tuple[bool, list[str]]:
    """写入单点变异并跑测试；返回 (装置是否通过, 红集合)。finally 里字节级还原。"""
    eol = "\r\n" if "\r\n" in original else "\n"
    old = old.replace("\n", eol)
    new = new.replace("\n", eol)
    anchor = original.count(old)
    if anchor != 1:
        print(f"[FAIL] {label}：锚点出现 {anchor} 次（要求恰好 1 次）")
        return False, []
    mutated = original.replace(old, new, 1).encode("utf-8")
    if _sha(mutated) == before:
        print(f"[FAIL] {label}：变异是 no-op")
        return False, []
    GRAPH.write_bytes(mutated)
    try:
        rc, output = _run_tests(PROMPT_TESTS)
        if rc == 0:
            print(f"[FAIL] {label}：变异后测试全绿，这条判据是装饰")
            return False, []
        return True, _red_names(output)
    finally:
        GRAPH.write_bytes(original.encode("utf-8"))
        if _sha(GRAPH.read_bytes()) != before:
            print(f"[FAIL] {label}：还原后 sha256 不一致")


def main() -> int:
    failures = 0
    original_bytes = GRAPH.read_bytes()
    before = _sha(original_bytes)
    original = original_bytes.decode("utf-8")

    rc, output = _run_tests(PROMPT_TESTS)
    if rc != 0:
        print(f"[FAIL] 基线不是全绿，变异读数作废：{_red_names(output)}")
        return 1
    print(f"[OK] 基线全绿（{len(PROMPT_TESTS)} 个目标）")

    for label, old, new, expected in MUTATIONS:
        ok, red = _apply_and_run(original, before, label, old, new)
        if not ok:
            failures += 1
            continue
        style_red = STYLE_TEST in red
        rule_red = [name for name in red if name != STYLE_TEST]
        if not style_red:
            print(f"[FAIL] {label}：元测试没红，说明新纪律词条可能还有别处副本")
        if rule_red != expected:
            print(f"[FAIL] {label}：红集合与期望不同源")
            print(f"  期望: {expected}")
            print(f"  实际: {rule_red}")
            failures += 1
            continue
        if not style_red:
            failures += 1
            continue
        print(f"[OK] {label}：如期只红 {rule_red}（元测试同源红）")

    ok, red = _apply_and_run(original, before, STYLE_MUTATION[0], STYLE_MUTATION[1], STYLE_MUTATION[2])
    if not ok:
        failures += 1
    elif set(red) != STYLE_MUTATION_EXPECTED:
        print(f"[FAIL] {STYLE_MUTATION[0]}：期望 {sorted(STYLE_MUTATION_EXPECTED)}，实际红 {sorted(set(red))}")
        failures += 1
    else:
        print(f"[OK] {STYLE_MUTATION[0]}：如期红三条元测试")

    rc, output = _run_tests(PROMPT_TESTS)
    if rc != 0:
        print(f"[FAIL] 还原后目标测试不绿，恢复不干净：{_red_names(output)}")
        failures += 1
    else:
        print("[OK] 还原后目标测试全绿（自检通过）")

    total = len(MUTATIONS) + 1
    print(f"\n变异电池：{total - failures}/{total} 条变异如期红")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
