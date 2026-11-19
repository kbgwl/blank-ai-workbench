"""apply_patch 写盘形态的变异电池（内核第三刀）。

这一刀修的是"按本机换行写"：Windows 上 ``TextIOWrapper(newline=None)`` 会把每个
LF 文件改写成 CRLF，连 ``.sh`` 都不放过；BOM 文件上的补丁还会静默不生效。

六个变异，每个都是"看起来更省事、其实把文件改坏"的退化：

1. 写盘不 conform——补丁结果直接落盘，CRLF 文件被整体改成 LF；
2. 一律按 CRLF 写——LF 文件被整体改写；
3. BOM 不剥离——补丁的"第 1 行"永远对不上，改动被静默吞掉；
4. 备份存归一后的文本——还原回来是 LF，备份不再是"原来的那一份"；
5. diff 拿原文（含 CRLF）跟结果比——改一行报整文件；
6. BOM 不写回——文件身份被改掉。

用法：`python tests/mutation_battery_patch_form_v426.py`，退出码 0 = 全部被抓。
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

API = Path(__file__).resolve().parent.parent
PY = API / ".venv" / "Scripts" / "python.exe"
TARGET = API / "src" / "blank_api" / "services" / "tools" / "patch.py"

TESTS = ["tests/test_patch_form_v426.py"]

MUTATIONS = [
    (
        "写盘不 conform",
        "        payload_text = conform_written(patched, original)",
        "        payload_text = patched",
        "test_crlf_file_stays_crlf_after_a_patch",
    ),
    (
        "一律按 CRLF 写",
        "    if crlf > lf_only:",
        "    if True:",
        "test_lf_file_stays_lf_after_a_patch",
    ),
    (
        "BOM 不剥离",
        '        bom = original.startswith("\\ufeff")',
        "        bom = False",
        "test_bom_is_kept_and_the_patch_actually_applies",
    ),
    (
        "备份存归一后的文本",
        "            backup_rel = write_backup(root, task_id, relative, raw)",
        # 模拟旧写法：备份的是换行归一后的文本（旧的读法是 TextIOWrapper(newline=None)）。
        # 直接传 original.encode() 是等价变异——original 现在保留着原换行，抓不住才对。
        '            backup_rel = write_backup(root, task_id, relative, original.replace("\\r\\n", "\\n").encode("utf-8"))',
        "test_backup_keeps_the_original_bytes",
    ),
    (
        "diff 拿原文跟结果比",
        "    diff = unified_diff(working, patched, relative)",
        "    diff = unified_diff(original, patched, relative)",
        "test_crlf_file_stays_crlf_after_a_patch",
    ),
    (
        "BOM 不写回",
        "    if original.startswith(bom) and not text.startswith(bom):\n        text = bom + text",
        "    if original.startswith(bom) and not text.startswith(bom):\n        text = text",
        "test_bom_is_kept_and_the_patch_actually_applies",
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
        print("[复原失败] patch.py 与跑之前不一致")
        failures += 1
    rc, red = run_tests()
    if rc != 0:
        print("还原后不绿：", red)
        failures += 1

    print(f"\n变异：{len(MUTATIONS) - failures}/{len(MUTATIONS)} 被抓")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
