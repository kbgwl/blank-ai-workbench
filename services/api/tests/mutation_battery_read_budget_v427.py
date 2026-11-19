"""读文件预算的变异电池（内核第四刀）。

这一刀改的是"一次读多少"：预算不再是一个与文件大小无关的常数 6,000。
但"把数字调大"本身不是能力，能力在于三条同时成立——

- 装得下的文件真的整份给；
- 装不下的文件给的量真的随体量走（而不是永远那个数）；
- 提示里的行号 / offset 与正文一字不差。

所以这里的六个变异都是"看起来很合理、实际把这件事做岔"的退化：
把预算改回常数、小文件也切、巨物也放开、没有 size 时拿已读长度冒充文件大小、
以及行号报多/报少一行（后者正是这条提示要消灭的那件事）。

用法：`python tests/mutation_battery_read_budget_v427.py`，退出码 0 = 全部被抓。
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

API = Path(__file__).resolve().parent.parent
PY = API / ".venv" / "Scripts" / "python.exe"
TARGET = API / "src" / "blank_api" / "services" / "agent" / "payload.py"

TESTS = ["tests/test_read_budget_v427.py"]

MUTATIONS = [
    (
        "预算回到常数",
        "    if size <= READ_SMALL_FILE:\n        return READ_SMALL_FILE\n    if size <= READ_MEDIUM_FILE:\n        return READ_HEAD_CAP",
        "    if size <= READ_SMALL_FILE:\n        return base\n    if size <= READ_MEDIUM_FILE:\n        return base",
        "test_medium_file_gets_a_larger_head_than_the_old_constant",
    ),
    (
        "小文件也按旧常数切",
        "    if size <= READ_SMALL_FILE:\n        return READ_SMALL_FILE",
        "    if size <= READ_SMALL_FILE:\n        return base",
        "test_file_that_fits_in_one_read_is_not_cut",
    ),
    (
        "巨物也放开给满",
        "    if size <= READ_MEDIUM_FILE:\n        return READ_HEAD_CAP\n    return base",
        "    if size <= READ_MEDIUM_FILE:\n        return READ_HEAD_CAP\n    return READ_HEAD_CAP",
        "test_monster_file_keeps_the_old_conservative_head",
    ),
    (
        "没有 size 就拿已读长度冒充文件大小",
        "        return max(base, output_len) if output_len <= READ_SMALL_FILE else base",
        "        return READ_HEAD_CAP",
        "test_missing_size_falls_back_to_what_was_actually_read",
    ),
    (
        "提示多报一行",
        "    end = start + len(lines) - 1",
        "    end = start + len(lines)",
        "test_offset_after_a_larger_head_still_points_at_the_next_unread_line",
    ),
    (
        "提示少报一行",
        "    end = start + len(lines) - 1",
        "    end = start + len(lines) - 2",
        "test_offset_after_a_larger_head_still_points_at_the_next_unread_line",
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
        print("[复原失败] payload.py 与跑之前不一致")
        failures += 1
    rc, red = run_tests()
    if rc != 0:
        print("还原后不绿：", red)
        failures += 1

    print(f"\n变异：{len(MUTATIONS) - failures}/{len(MUTATIONS)} 被抓")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
