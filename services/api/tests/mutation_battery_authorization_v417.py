"""权限口径的变异电池（卡 2b）。

电池测的不是代码，是**判据**：把实现故意改坏，对应那条测试必须红；改回来必须绿。
一条变异后测试仍然全绿，说明那条判据是摆设——比没有测试更危险。

覆盖八个变异体：
1. 严格复核被摘掉（高危自动放行不再升级成问人）
2. 能力边界块被删（边界退化成"策略说了算"）
3. 资源清单退化成 `tool:`（路径/命令不再进审批卡与审计）
4. 审查员的否决回到"直接拒"（不可逆动作不再提级出卡）
5. 代批重新忽略风险等级（auto 档又能替人按高危确认键）
6. 交付对账被摘掉（零动作却照抄"已删除"）
7. 资源串用朴素空格切分来解码（规则会变宽：带空白的 token 被切散）
8. 落盘不再校验往返（切残的规则被认下来，覆盖范围比原来大）

用法：`python tests/mutation_battery_authorization_v417.py`，退出码 0 = 全部变异都被抓住。
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

API = Path(__file__).resolve().parent.parent
PY = API / ".venv" / "Scripts" / "python.exe"
AUTHZ = API / "src" / "blank_api" / "services" / "authorization.py"
GUARDIAN = API / "src" / "blank_api" / "services" / "guardian.py"
GRAPH = API / "src" / "blank_api" / "services" / "agent" / "graph.py"

TESTS = [
    "tests/test_authorize_single_entry_v417.py",
    "tests/test_guardian_trail_v51.py",
    "tests/test_no_false_action_claim_v416.py",
    "tests/test_rules_resources_diff_v418.py",
]

RULES = API / "src" / "blank_api" / "services" / "approval_rules.py"

# (标签, 文件, 原文片段, 替换成, 必须变红的测试名)
MUTATIONS = [
    (
        "摘掉严格复核",
        AUTHZ,
        "        decided = permissions.decide(tool, args, resolved)\n"
        "        return _with_resources(enforce_strict_review(decided, resolved, mode), tool.name, args)",
        "        decided = permissions.decide(tool, args, resolved)\n"
        "        return _with_resources(decided, tool.name, args)",
        "test_an_automatic_allow_of_a_high_risk_action_becomes_a_question",
    ),
    (
        "删掉能力边界",
        AUTHZ,
        "    safety = getattr(context, \"safety\", None)\n    if safety is not None:",
        "    safety = getattr(context, \"safety\", None)\n    if False:",
        "test_the_capability_boundary_wins_over_the_policy",
    ),
    (
        "资源退化成工具名",
        AUTHZ,
        '    if tool in _FILE_TOOLS:\n        return (f"path:{path_scope(args) or \'*\'}",)',
        '    if tool in _FILE_TOOLS:\n        return (f"tool:{tool}",)',
        "test_resources_are_paths_and_commands",
    ),
    (
        "审查否决回到直接拒",
        GRAPH,
        "                must_ask = (not reversible) or host_bypass_requested(name, arguments)\n"
        "                outcome = \"asked\" if must_ask else \"blocked\"",
        "                outcome = \"blocked\"",
        "test_an_irreversible_denial_escalates_to_a_card",
    ),
    (
        "代批重新忽略风险",
        GUARDIAN,
        '        return self.mode == "auto" and verdict.allowed and risk not in ("high", "critical")',
        '        return self.mode == "auto" and verdict.allowed',
        "test_auto_mode_only_auto_approves_low_risk",
    ),
    (
        "摘掉交付对账",
        GRAPH,
        "    if not report or _acted_successfully(state):\n        return report",
        "    if not report or True:\n        return report",
        "test_a_denied_action_is_not_reported_as_done",
    ),
    (
        "资源串用朴素切分解码（会变宽）",
        RULES,
        '        parsed = prefix_rule(text[len(CMD) :])',
        '        parsed = tuple(text[len(CMD) :].split())',
        "test_a_v2_roundtrip_is_lossless",
    ),
    (
        "落盘不再校验往返",
        RULES,
        "    if not text or prefix_rule(text) != normalized[1]:\n        return None",
        "    if not text:\n        return None",
        "test_a_v1_rule_that_cannot_roundtrip_is_dropped_not_widened",
    ),
]


def run_tests() -> tuple[int, list[str]]:
    proc = subprocess.run(
        [str(PY), "-m", "pytest", *TESTS, "-q", "--tb=no", "-p", "no:cacheprovider"],
        cwd=str(API),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    red = [
        line.split("::")[-1].split()[0]
        for line in (proc.stdout + proc.stderr).splitlines()
        if line.startswith("FAILED") and "::" in line
    ]
    return proc.returncode, red


def main() -> int:
    originals = {path: path.read_bytes() for path in {AUTHZ, GUARDIAN, GRAPH, RULES}}
    digests = {path: hashlib.sha256(raw).hexdigest() for path, raw in originals.items()}
    texts = {path: raw.decode("utf-8") for path, raw in originals.items()}

    rc, red = run_tests()
    if rc != 0:
        print("基线不绿，先修再跑电池：", red)
        return 1

    failures = 0
    for label, path, old, new, must_red in MUTATIONS:
        text = texts[path]
        eol = "\r\n" if "\r\n" in text else "\n"
        old_eol, new_eol = old.replace("\n", eol), new.replace("\n", eol)
        if text.count(old_eol) != 1:
            print(f"[锚点不符] {label}：出现 {text.count(old_eol)} 次")
            failures += 1
            continue
        path.write_bytes(text.replace(old_eol, new_eol).encode("utf-8"))
        try:
            rc, red = run_tests()
        finally:
            path.write_bytes(originals[path])
        if rc == 0:
            print(f"[没抓住] {label}：改坏了实现，判据却全绿 —— 这条判据是摆设")
            failures += 1
        elif must_red not in red:
            print(f"[红错位置] {label}：期望 {must_red} 红，实际红的是 {red}")
            failures += 1
        else:
            print(f"[OK] {label} → {must_red}（本变异共红 {len(red)} 条）")

    for path, raw in originals.items():
        if hashlib.sha256(path.read_bytes()).hexdigest() != digests[path]:
            print(f"[复原失败] {path}")
            failures += 1
    rc, red = run_tests()
    if rc != 0:
        print("还原后不绿：", red)
        failures += 1

    caught = len(MUTATIONS) - failures
    print(f"\n变异：{caught}/{len(MUTATIONS)} 被抓")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
