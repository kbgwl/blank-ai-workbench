"""「只想不说」判据的变异电池：每格只改一处，红集合精确比对。"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

API = Path(__file__).resolve().parent.parent
PY = API / ".venv" / "Scripts" / "python.exe"
RESPONSES = API / "src" / "blank_api" / "services" / "models" / "openai_responses.py"
GRAPH = API / "src" / "blank_api" / "services" / "agent" / "graph.py"
TESTS = "tests/test_reasoning_only_stream_v174.py"

T_COUNTS = "test_reasoning_only_stream_reports_counts_and_is_not_empty"
T_GATE = "test_reasoning_only_stream_is_not_a_gateway_stream_problem"
T_WIRING = "test_no_non_stream_retry_when_the_stream_was_thinking"

COUNT = (
    '                    elif kind in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):\n'
    "                        reasoning_events += 1\n"
    '                        reasoning_chars += len(str(event.get("delta") or ""))\n'
)
COUNT_OFF = (
    '                    elif kind in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):\n'
    "                        pass\n"
)
GATE = (
    '    if error.code == "model_stream_empty" and error.reasoning_events:\n'
    "        return False\n"
)
DETAIL = (
    "            detail = (\n"
    '                f"（期间收到 {reasoning_events} 个思考增量、{reasoning_chars} 字，没有正文与工具调用）"\n'
    "                if reasoning_events\n"
    '                else ""\n'
    "            )\n"
)
DETAIL_OFF = '            detail = ""\n'
EVENT_FIELDS = (
    '                         "reasoning_events": error.reasoning_events,\n'
    '                         "reasoning_chars": error.reasoning_chars},\n'
)
EVENT_FIELDS_OFF = "                         },\n"

MUTATIONS = [
    ("A_不再数思考增量", RESPONSES, COUNT, COUNT_OFF, [T_COUNTS, T_GATE, T_WIRING]),
    ("B_只想不说也允许退整包", GRAPH, GATE, "", [T_GATE, T_WIRING]),
    ("C_报错不再写思考计数", RESPONSES, DETAIL, DETAIL_OFF, [T_COUNTS]),
    ("D_事件不再写思考计数", GRAPH, EVENT_FIELDS, EVENT_FIELDS_OFF, [T_WIRING]),
]


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
    print("[OK] 基线 4/4 绿")

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
        if red != expected:
            print(f"[FAIL] {label}：红集合不同源\n  期望 {expected}\n  实际 {red}")
            failures += 1
            continue
        print(f"[OK] {label}：如期只红 {red}")

    rc, red = _run()
    if rc != 0:
        print(f"[FAIL] 还原后不绿：{red}")
        failures += 1
    else:
        print("[OK] 还原后 4/4 绿（自检通过）")
    print(f"\n变异电池：{len(MUTATIONS) - failures}/{len(MUTATIONS)} 条如期红")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
