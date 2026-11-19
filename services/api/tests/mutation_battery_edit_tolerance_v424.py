"""编辑/写盘容错的变异电池（内核第一刀）。

这一刀补的是"模型做对了、工具却判错"的三处：引号形态、转义字面量、删行留空行；
外加 write_file 的 CRLF / BOM 丢失。十个变异，每个都是一种"读起来很合理、
实际把这件事做岔"的退化：

1. 只往一个方向折叠引号——最常见的错法（文件弯、模型直）正好漏掉；
2. 删除时不做整行处理——"删了一行，却多了一行空行"；
3. 用模型发的 needle 去替换而不是命中的原文——替换落空却报成功；
4. 整行判断不设边界——删一个词也会吞掉换行，两行被粘成一行；
5. 删行不带走换行——"删了一行，却多了一行空行"；
6. 写盘前不还原 BOM——文件身份被改掉；
7. 写盘一律用 LF——改两行报出整文件重写；
8. 只要原文件有一处 CRLF 就全用 CRLF——混合换行的文件被整体翻过来；
9. 只在比较时归一、写盘时不归一——判未变更却写出另一份内容；
10. 容错命中的方式不回传——模型永远不知道自己发的原文是错的。

用法：`python tests/mutation_battery_edit_tolerance_v424.py`，退出码 0 = 全部被抓。
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

API = Path(__file__).resolve().parent.parent
PY = API / ".venv" / "Scripts" / "python.exe"
TARGET = API / "src" / "blank_api" / "services" / "tools" / "editing.py"

TESTS = ["tests/test_edit_tolerance_v424.py"]

MUTATIONS = [
    (
        "只往一个方向折叠引号",
        "    if folded_text != text or folded != needle:",
        "    if folded != needle:",
        "test_curly_quotes_in_file_accept_straight_quotes_from_model",
    ),
    (
        "删除时不做整行处理",
        '    if replacement == "" and not needle.endswith("\\n"):',
        "    if False:",
        "test_deleting_a_line_takes_its_newline_with_it",
    ),
    (
        "用 needle 替换而不是命中的原文",
        "        updated = updated.replace(hit, replacement) if replace_all else updated.replace(hit, replacement, 1)",
        "        updated = updated.replace(needle, replacement) if replace_all else updated.replace(needle, replacement, 1)",
        "test_curly_quotes_in_file_accept_straight_quotes_from_model",
    ),
    (
        "整行判断不设边界",
        "            if starts_line and ends_line:",
        "            if True:",
        "test_deleting_part_of_a_line_keeps_the_newline",
    ),
    (
        "删行不带走换行",
        '                return found[0] + "\\n", "整行删除"',
        '                return found[0], "整行删除"',
        "test_deleting_a_line_takes_its_newline_with_it",
    ),
    (
        "写盘前不还原 BOM",
        "    if original.startswith(bom) and not text.startswith(bom):\n        text = bom + text",
        "    if original.startswith(bom) and not text.startswith(bom):\n        text = text",
        "test_write_keeps_bom",
    ),
    (
        "写盘一律用 LF",
        "    if crlf > lf_only:\n        return normalized.replace(\"\\n\", \"\\r\\n\")\n    return normalized",
        '    if crlf < 0:\n        return normalized.replace("\\n", "\\r\\n")\n    return normalized',
        "test_write_keeps_crlf_so_a_two_line_change_stays_two_lines",
    ),
    (
        "有一处 CRLF 就全用 CRLF",
        "    if crlf > lf_only:",
        "    if crlf > 0:",
        "test_conform_picks_the_majority_line_ending",
    ),
    (
        "只在比较时归一、写盘时不归一",
        "                text = _conform_written(text, original)",
        "                _conform_written(text, original)",
        "test_write_keeps_crlf_so_a_two_line_change_stays_two_lines",
    ),
    (
        "容错命中的方式不回传",
        "        if how:\n            matched.append(how)",
        "        if False:\n            matched.append(how)",
        "test_quote_match_is_reported_back_to_the_model",
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
        print("[复原失败] editing.py 与跑之前不一致")
        failures += 1
    rc, red = run_tests()
    if rc != 0:
        print("还原后不绿：", red)
        failures += 1

    print(f"\n变异：{len(MUTATIONS) - failures}/{len(MUTATIONS)} 被抓")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
