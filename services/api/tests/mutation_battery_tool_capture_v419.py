"""工具层捕获上限的变异电池（工具层第一刀）。

三条变异，对应三种"看起来对、其实把风险留给用户"的退化：

1. 去掉捕获上限（把上限读回成整段读）——内存又跟着命令走；
2. 解码顺序反过来（先试 gbk 再裁尾巴）——半行中文变 `涓腑` 那种乱码；
3. 超时路径不再交回已跑出的输出——模型只看到"超时了"，判断不了卡在哪。

用法：`python tests/mutation_battery_tool_capture_v419.py`，退出码 0 = 全部被抓。
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

API = Path(__file__).resolve().parent.parent
PY = API / ".venv" / "Scripts" / "python.exe"
SHELL = API / "src" / "blank_api" / "services" / "tools" / "shell.py"

TESTS = ["tests/test_shell_capture_cap_v419.py"]

MUTATIONS = [
    (
        "去掉捕获上限",
        "    raw = handle.read(MAX_CAPTURE_BYTES)",
        "    raw = handle.read()",
        "test_a_huge_output_is_capped_and_reported",
    ),
    (
        "解码顺序反过来（gbk 抢在裁尾巴之前）",
        '    for encoding in ("utf-8", *_FALLBACK_ENCODINGS):\n'
        "        for cut in range(0, 4):",
        '    for cut in range(0, 4):\n'
        '        for encoding in ("utf-8", *_FALLBACK_ENCODINGS):',
        "test_the_cap_does_not_split_a_chinese_character",
    ),
    (
        "超时不交回已跑出的输出",
        "            stdout, out_total = _read_capture(out)\n"
        "            stderr, err_total = _read_capture(err)\n"
        "            return {\n"
        '                "exit_code": None,',
        '            stdout, out_total = "", 0\n'
        "            stderr, err_total = _read_capture(err)\n"
        "            return {\n"
        '                "exit_code": None,',
        "test_a_timeout_still_hands_back_what_ran",
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
    original = SHELL.read_bytes()
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
        if text.count(old_eol) != 1:
            print(f"[锚点不符] {label}：出现 {text.count(old_eol)} 次")
            failures += 1
            continue
        SHELL.write_bytes(text.replace(old_eol, new_eol).encode("utf-8"))
        try:
            rc, red = run_tests()
        finally:
            SHELL.write_bytes(original)
        if rc == 0:
            print(f"[没抓住] {label}：改坏了实现，判据却全绿")
            failures += 1
        elif must_red not in red:
            print(f"[红错位置] {label}：期望 {must_red} 红，实际 {red}")
            failures += 1
        else:
            print(f"[OK] {label} → {must_red}（本变异共红 {len(red)} 条）")

    if hashlib.sha256(SHELL.read_bytes()).hexdigest() != digest:
        print("[复原失败] shell.py 与跑之前不一致")
        failures += 1
    rc, red = run_tests()
    if rc != 0:
        print("还原后不绿：", red)
        failures += 1

    print(f"\n变异：{len(MUTATIONS) - failures}/{len(MUTATIONS)} 被抓")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
