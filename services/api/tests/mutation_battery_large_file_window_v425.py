"""大文件 window 读的变异电池（内核第二刀）。

这一刀修的是"把前 512KB 当成整个文件"：4 万行的日志自称 9893 行，深处 offset 读空。

十个变异，每个都是一种"看起来仍然能跑、其实把事实说错"的退化：

1. 大文件不进窗口分支——退回老路，总行数又变成前缀行数；
2. 行起点差一个字节——每段开头多一个换行，模型复制过去就带脏字符；
3. 起点永远从文件头算——offset 形同虚设；
4. 总行数不补"没有结尾换行的那一行"——最后一行读不到，还不报错；
5. 编码探测不从末尾退字节——头正好切在汉字中间时整份中文文件被判二进制；
6. 只认 utf-8 不认 gbk——中文 Windows 上的老文件全变成"不可读"；
7. 单行超长仍然给续读 offset——那个 offset 读不到任何东西，是空话；
8. limit 不生效——模型要 3 行拿回 9892 行；
9. 不丢半行——返回一段残句，模型照着改；
10. truncated 恒为假——半份内容被当成完整文件（哈希也跟着变成有效的）。

用法：`python tests/mutation_battery_large_file_window_v425.py`，退出码 0 = 全部被抓。
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

API = Path(__file__).resolve().parent.parent
PY = API / ".venv" / "Scripts" / "python.exe"
TARGET = API / "src" / "blank_api" / "services" / "workspace.py"

TESTS = ["tests/test_large_file_window_v425.py"]

MUTATIONS = [
    (
        "大文件不进窗口分支",
        "    if truncated:\n        # 大文件不能拿",
        "    if False:\n        # 大文件不能拿",
        "test_deep_offset_reaches_lines_beyond_the_first_512kb",
    ),
    (
        "行起点差一个字节",
        "                line_start = base + newline + 1",
        "                line_start = base + newline",
        "test_deep_offset_reaches_lines_beyond_the_first_512kb",
    ),
    (
        "起点永远从文件头算",
        "                if found is None and line == start_line:\n                    found = line_start\n                newlines += 1",
        "                if found is None and line == start_line:\n                    found = 0\n                newlines += 1",
        "test_deep_offset_reaches_lines_beyond_the_first_512kb",
    ),
    (
        "总行数不补没有结尾换行的那一行",
        '    total = newlines if last == b"\\n" or scanned == 0 else newlines + 1',
        "    total = newlines",
        "test_file_without_trailing_newline_counts_the_last_line",
    ),
    (
        "编码探测不从末尾退字节",
        "        for cut in range(4):",
        "        for cut in range(1):",
        # 判据要点在"单行超长"那条：多行中文样本里，512KB 那个切口偶尔正好落在
        # 行尾（换行是 1 字节，把 3 的倍数顶开了），抓不抓得住看运气。
        # 单行全是 3 字节汉字时必然切在字中间，是确定性的那条。
        "test_single_huge_line_does_not_promise_a_next_offset",
    ),
    (
        "只认 utf-8 不认 gbk",
        '    for encoding in ("utf-8", "gbk") if os.name == "nt" else ("utf-8",):\n        for cut in range(4):',
        '    for encoding in ("utf-8",):\n        for cut in range(4):',
        "test_gbk_large_file_keeps_its_encoding",
    ),
    (
        "单行超长仍然给续读 offset",
        "            cut_mid_line = True",
        "            cut_mid_line = False",
        "test_single_huge_line_does_not_promise_a_next_offset",
    ),
    (
        "limit 不生效",
        "    kept = chunk_lines if limit is None else chunk_lines[: max(int(limit), 0)]",
        "    kept = chunk_lines",
        "test_limit_is_honoured_exactly",
    ),
    (
        "不丢半行",
        "            raw = raw[: cut + 1]",
        "            raw = raw",
        "test_window_never_ends_with_half_a_line",
    ),
    (
        "truncated 恒为假",
        '        "truncated": not (start_line == 1 and reached_eof and exact),',
        '        "truncated": False,',
        "test_partial_read_carries_no_hash_and_is_marked_truncated",
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
        print("[复原失败] workspace.py 与跑之前不一致")
        failures += 1
    rc, red = run_tests()
    if rc != 0:
        print("还原后不绿：", red)
        failures += 1

    print(f"\n变异：{len(MUTATIONS) - failures}/{len(MUTATIONS)} 被抓")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
