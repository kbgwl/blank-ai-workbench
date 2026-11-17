"""空流退路时限的变异电池。

每格只改一处，跑 v173 的四个判据，红集合按名字精确比对：
  1. 基线全绿；2. 锚点恰好一次；3. 写入后 sha256 必变（非 no-op）；
  4. 字节级还原并复核 sha256，还原后重新全绿。
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

API = Path(__file__).resolve().parent.parent
PY = API / ".venv" / "Scripts" / "python.exe"
GRAPH = API / "src" / "blank_api" / "services" / "agent" / "graph.py"
TESTS = "tests/test_stream_fallback_budget_v173.py"

WIRE = """                    adapter, specs, chat, node, role, effort=effort, thinking=thinking, model=model,
                    budget_seconds=budget,
                )"""
WIRE_OFF = """                    adapter, specs, chat, node, role, effort=effort, thinking=thinking, model=model
                )"""
RETRYABLE = '                    retryable=False,\n                ) from None'
RETRYABLE_ON = '                    retryable=True,\n                ) from None'
HELPER = "    if not configured_seconds or configured_seconds <= 0:\n        return FALLBACK_TIMEOUT_SECONDS\n    return min(float(configured_seconds), FALLBACK_TIMEOUT_SECONDS)"
HELPER_OFF = "    if not configured_seconds or configured_seconds <= 0:\n        return FALLBACK_TIMEOUT_SECONDS\n    return float(configured_seconds)"
EVENT_BUDGET = '"budget_ms": int(budget * 1000)},\n                )\n                return await self._call_non_stream(\n'
EVENT_BUDGET_OFF = '},\n                )\n                return await self._call_non_stream(\n'

T_CAPPED = "test_fallback_budget_is_capped_by_its_own_deadline"
T_SHORTER = "test_fallback_uses_the_shorter_configured_timeout"
T_EVENT = "test_fallback_event_records_the_budget"
T_HELPER = "test_fallback_budget_helper_semantics"

MUTATIONS = [
    ("A_退路不再带预算", WIRE, WIRE_OFF, 2, [T_CAPPED, T_SHORTER, T_EVENT]),
    ("B_退路超时改成可重试", RETRYABLE, RETRYABLE_ON, 1, [T_CAPPED]),
    ("C_预算不再取 min(配置, 封顶)", HELPER, HELPER_OFF, 1, [T_CAPPED, T_EVENT, T_HELPER]),
    ("D_事件里不再写预算", EVENT_BUDGET, EVENT_BUDGET_OFF, 2, [T_EVENT]),
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
    original = GRAPH.read_bytes()
    before = _sha(original)
    text = original.decode("utf-8")

    rc, red = _run()
    if rc != 0:
        print(f"[FAIL] 基线不是全绿，读数作废：{red}")
        return 1
    print("[OK] 基线 4/4 绿")

    for label, old, new, count, expected in MUTATIONS:
        anchor = text.count(old)
        if anchor != count:
            print(f"[FAIL] {label}：锚点出现 {anchor} 次（要求 {count} 次）")
            failures += 1
            continue
        mutated = text.replace(old, new).encode("utf-8")
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

    total = len(MUTATIONS)
    print(f"\n变异电池：{total - failures}/{total} 条如期红")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
