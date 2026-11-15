"""空响应 + 零交付的变异电池（#394–#396）。

装置自己的四条自检，缺一条整批读数作废：
  1. 基线：变异之前目标判据必须全绿（否则「红」可能是本来就红）；
  2. 锚点唯一：old 在源码里恰好出现一次（出现 0 次或多次都判失败）；
  3. 非 no-op：写入后字节 sha256 必须变化；
  4. 字节级还原：恢复后 sha256 与变异前一致，测试重新全绿。

每条变异还要求「红的测试与变异同源」：只红别的测试等于判据没钉在实现上。
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

API = Path(__file__).resolve().parent.parent
PY = API / ".venv" / "Scripts" / "python.exe"
GRAPH = API / "src" / "blank_api" / "services" / "agent" / "graph.py"
VERIFIER = API / "src" / "blank_api" / "services" / "agent" / "verifier.py"
ZERO = "tests/test_empty_response_zero_delivery.py"
FALLBACK = "tests/test_stream_output.py::test_fallback_keeps_the_model_override"

# (label, path, old, new, 目标测试, 期望至少红的测试名片段)
MUTATIONS = [
    (
        "A_空响应闸门被拔掉",
        GRAPH,
        '        if not str(response.text or "").strip() and not response.tool_calls:\n'
        "            # 推理型模型",
        "        if False:  # 变异：空响应闸门被拔掉\n            # 推理型模型",
        [ZERO],
        ["test_empty_model_response_fails_the_task_with_a_code", "test_empty_response_retry_recovers"],
    ),
    (
        "B_重试不带明确提示",
        GRAPH,
        "            retry_messages = [\n"
        "                *messages,\n"
        '                authored({"role": "system", "content": EMPTY_RESPONSE_NUDGE}, self.run_id),\n'
        "            ]",
        "            retry_messages = list(messages)  # 变异：重试不带提示",
        [ZERO],
        ["test_empty_response_retry_recovers"],
    ),
    (
        "C_第二次空也放行",
        GRAPH,
        "                raise ModelError(\n"
        '                    "model_empty_response",\n'
        "                    f\"模型 {retry.model or self.adapter.config.model} 连续两次返回空响应\"\n"
        '                    "（既没有正文也没有工具调用），已停止本轮",\n'
        "                )",
        "                pass  # 变异：第二次空也放行",
        [ZERO],
        ["test_empty_model_response_fails_the_task_with_a_code"],
    ),
    (
        "D_未完成词表丢掉新增词",
        VERIFIER,
        "    # 真机 9eac5bed：交付写「完成：工作区仍为空，三个文件均未创建，功能尚未实现」，\n"
        "    # 旧词表没有覆盖「未实现/仍为空」，整轮被判 succeeded 且零个文件。\n"
        '    "未实现", "仍为空", "没有任何改动", "未做任何改动",\n',
        "    # 变异：新增未完成词被拔掉\n",
        [ZERO],
        ["test_an_admitted_zero_delivery_is_blocking"],
    ),
    (
        "E_零交付不拦",
        VERIFIER,
        '    if not checks["delivery_matches_plan"]:\n'
        '        findings.append("目标有验收标准、计划要求改动，但磁盘零改动（零交付）")\n'
        "        blocking = True\n",
        "    # 变异：零交付只记账，不改判\n",
        [ZERO],
        ["test_zero_changes_against_an_edit_plan_is_blocking_even_with_neutral_text"],
    ),
    (
        "F_零交付丢掉验收标准前提",
        VERIFIER,
        '    checks["delivery_matches_plan"] = not (not paths and bool(done_when) and plan_edits)',
        '    checks["delivery_matches_plan"] = not (not paths and plan_edits)  # 变异：丢掉验收标准前提',
        [ZERO],
        ["test_an_edit_plan_without_acceptance_criteria_stays_incomplete_not_failed"],
    ),
    (
        "G_流式报错退路丢模型覆盖",
        GRAPH,
        '                    {"reason": error.message, "code": error.code, "status": error.status, "role": role},\n'
        "                )\n"
        "                return await self._call_non_stream(\n"
        "                    adapter, specs, chat, node, role, effort=effort, thinking=thinking, model=model\n"
        "                )",
        '                    {"reason": error.message, "code": error.code, "status": error.status, "role": role},\n'
        "                )\n"
        "                return await self._call_non_stream(\n"
        "                    adapter, specs, chat, node, role, effort=effort, thinking=thinking\n"
        "                )",
        [FALLBACK],
        ["test_fallback_keeps_the_model_override"],
    ),
    (
        "H_传输失败退路丢模型覆盖",
        GRAPH,
        '                     "role": role},\n'
        "                )\n"
        "                return await self._call_non_stream(\n"
        "                    adapter, specs, chat, node, role, effort=effort, thinking=thinking, model=model\n"
        "                )",
        '                     "role": role},\n'
        "                )\n"
        "                return await self._call_non_stream(\n"
        "                    adapter, specs, chat, node, role, effort=effort, thinking=thinking\n"
        "                )",
        [FALLBACK],
        ["test_fallback_keeps_the_model_override"],
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
