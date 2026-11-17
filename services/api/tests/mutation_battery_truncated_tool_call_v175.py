"""工具调用截断判据的变异电池：每格只改一处，红集合精确比对。"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

API = Path(__file__).resolve().parent.parent
PY = API / ".venv" / "Scripts" / "python.exe"
RESPONSES = API / "src" / "blank_api" / "services" / "models" / "openai_responses.py"
COMPAT = API / "src" / "blank_api" / "services" / "models" / "openai_compat.py"
STREAMING = API / "src" / "blank_api" / "services" / "models" / "streaming.py"
TESTS = "tests/test_truncated_tool_call_v175.py"

T_TRUNC = "test_responses_truncated_arguments_are_rejected"
T_COMPLETE = "test_responses_complete_arguments_still_pass"
T_COMPAT = "test_compat_truncated_arguments_are_rejected"
T_HELPER = "test_helper_flags_only_incomplete_json"
T_WIRING = "test_truncated_stream_never_becomes_a_fallback_request"

RESPONSES_CHECK = (
    "        final_calls = calls.as_list()\n"
    "        bad = invalid_tool_arguments(final_calls)\n"
    "        if bad:\n"
    "            name, size = bad[0]\n"
    "            # 截断的调用不许派发，也不许写回上下文；重试一次流才是对的路\n"
    "            raise ModelError(\n"
    '                "model_stream_truncated",\n'
    '                f"流在工具调用 {name} 的参数中途结束：{size} 字不是完整 JSON",\n'
    "                retryable=True,\n"
    "            )\n"
)
RESPONSES_OFF = "        final_calls = calls.as_list()\n"
COMPAT_CHECK = (
    "        final_calls = calls.as_list()\n"
    "        bad = invalid_tool_arguments(final_calls)\n"
    "        if bad:\n"
    "            name, size = bad[0]\n"
    "            raise ModelError(\n"
    '                "model_stream_truncated",\n'
    '                f"流在工具调用 {name} 的参数中途结束：{size} 字不是完整 JSON",\n'
    "                retryable=True,\n"
    "            )\n"
)
COMPAT_OFF = "        final_calls = calls.as_list()\n"
RETRYABLE = (
    "                retryable=True,\n"
    "            )\n"
    "        # 没等到 response.completed"
)
RETRYABLE_OFF = (
    "                retryable=False,\n"
    "            )\n"
    "        # 没等到 response.completed"
)
HELPER_BODY = '            bad.append((str(function.get("name") or ""), len(raw)))\n'

MUTATIONS = [
    ("A_Responses 不再检查截断", RESPONSES, RESPONSES_CHECK, RESPONSES_OFF, [T_TRUNC, T_WIRING]),
    ("B_兼容层不再检查截断", COMPAT, COMPAT_CHECK, COMPAT_OFF, [T_COMPAT]),
    ("C_截断被标成不可重试", RESPONSES, RETRYABLE, RETRYABLE_OFF, [T_TRUNC]),
    ("D_校验器不再标记坏参数", STREAMING, HELPER_BODY, "            pass\n", True),
]

EXPECT_HELPER = {T_TRUNC, T_COMPAT, T_HELPER, T_WIRING}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _run() -> tuple[int, list[str]]:
    result = subprocess.run(
        [str(PY), "-m", "pytest", TESTS, "-v", "--tb=no", "-q"],
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
    rc, red = _run()
    if rc != 0:
        print(f"[FAIL] 基线不是全绿，读数作废：{red}")
        return 1
    print("[OK] 基线 5/5 绿")

    for label, path, old, new, expected in MUTATIONS:
        original = path.read_bytes()
        before = _sha(original)
        text = original.decode("utf-8")
        eol = "\r\n" if "\r\n" in text else "\n"
        old = old.replace("\n", eol)
        new = new.replace("\n", eol)
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
        path.write_bytes(mutated)
        try:
            rc, red = _run()
        finally:
            path.write_bytes(original)
            if _sha(path.read_bytes()) != before:
                print(f"[FAIL] {label}：还原后 sha256 不一致")
                failures += 1
                continue
        if rc == 0:
            print(f"[FAIL] {label}：变异后全绿，判据是装饰")
            failures += 1
            continue
        want = sorted(EXPECT_HELPER) if expected is True else expected
        if sorted(red) != want:
            print(f"[FAIL] {label}：红集合不同源\n  期望 {want}\n  实际 {sorted(red)}")
            failures += 1
            continue
        print(f"[OK] {label}：如期只红 {red}")

    rc, red = _run()
    if rc != 0:
        print(f"[FAIL] 还原后不绿：{red}")
        failures += 1
    else:
        print("[OK] 还原后 5/5 绿（自检通过）")
    print(f"\n变异电池：{len(MUTATIONS) - failures}/{len(MUTATIONS)} 条如期红")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
