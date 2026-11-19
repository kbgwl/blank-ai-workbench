"""进展哨兵的变异电池（内核第六刀）。

这一刀的判据只有一句话：「这一步有没有带来新信息」。六个变异都是把这句判松的常见写法：

1. 重写阈值永不满足（等于没这个功能）；
2. 验证通过不清账（改完验过了，还被当成没验）；
3. 空转永不触发（等于只留了重写那半边）；
4. 读文件不算证据（把"读代码定位问题"判成空转）；
5. 失败也算证据（模型每失败一次就"有进展"，永远不提醒——真机里正是这条最容易写成）；
6. 冷却失效（每步念一句，从提醒变成噪音）。

用法：`python tests/mutation_battery_progress_sentinel_v427.py`，退出码 0 = 全部被抓。
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

API = Path(__file__).resolve().parent.parent
PY = API / ".venv" / "Scripts" / "python.exe"
TARGET = API / "src" / "blank_api" / "services" / "agent" / "progress.py"

TESTS = ["tests/test_progress_sentinel_v427.py"]

MUTATIONS = [
    (
        "重写阈值永不满足",
        "        if count < REWRITE_LIMIT or path not in self.unverified:",
        "        if True:",
        "test_rewriting_the_same_file_without_verifying_gets_one_nudge",
    ),
    (
        "验证通过不清账",
        "            self.unverified.clear()",
        "            pass",
        "test_a_successful_verification_clears_the_debt",
    ),
    (
        "空转永不触发",
        "        if idle < IDLE_LIMIT:",
        "        if True:",
        "test_four_steps_without_any_new_evidence_gets_a_nudge",
    ),
    (
        "读文件不算证据",
        '            if fingerprint not in self.seen_reads:\n                self.seen_reads.add(fingerprint)\n                self.last_evidence = step',
        '            if fingerprint not in self.seen_reads:\n                self.seen_reads.add(fingerprint)',
        "test_reading_a_new_file_counts_as_progress",
    ),
    (
        "失败也算证据",
        '        if not isinstance(payload, dict) or not payload.get("ok"):\n            return',
        '        if not isinstance(payload, dict):\n            return',
        "test_a_failed_write_is_not_a_write",
    ),
    (
        "冷却失效，每步都念",
        '        if step - last < NAG_COOLDOWN:\n            return ""\n        self.nagged["rewrite"] = step',
        '        self.nagged["rewrite"] = step',
        "test_the_nudge_does_not_repeat_every_step",
    ),
]


def run_tests() -> tuple[int, list[str]]:
    proc = subprocess.run(
        [str(PY), "-m", "pytest", *TESTS, "-q", "-p", "no:cacheprovider"],
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
    original = TARGET.read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    text = original.decode("utf-8")
    eol = "\r\n" if "\r\n" in text else "\n"

    rc, red = run_tests()
    if rc != 0:
        print("基线不绿：", red)
        return 1

    failures = 0
    for label, old, new, must_red in MUTATIONS:
        old_eol, new_eol = old.replace("\n", eol), new.replace("\n", eol)
        count = text.count(old_eol)
        if count != 1:
            print(f"[锚点不符] {label}：出现 {count} 次")
            failures += 1
            continue
        TARGET.write_bytes(text.replace(old_eol, new_eol).encode("utf-8"))
        try:
            rc, red = run_tests()
        finally:
            TARGET.write_bytes(original)
        if rc == 0:
            print(f"[没抓住] {label}：改坏了实现，判据却全绿")
            failures += 1
        elif must_red not in red:
            print(f"[红错位置] {label}：期望 {must_red} 红，实际 {red}")
            failures += 1
        else:
            print(f"[OK] {label} → {must_red}（本变异共红 {len(red)} 条）")

    if hashlib.sha256(TARGET.read_bytes()).hexdigest() != digest:
        print("[复原失败] progress.py 与跑之前不一致")
        failures += 1
    rc, red = run_tests()
    if rc != 0:
        print("还原后不绿：", red)
        failures += 1

    print(f"\n变异：{len(MUTATIONS) - failures}/{len(MUTATIONS)} 被抓")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
