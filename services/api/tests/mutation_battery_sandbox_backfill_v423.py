"""沙箱补写分支的变异电池（工具层第五刀）。

这一刀修的是一个真实缺陷：补写分支手工拼分隔行 ``[stderr]\\n``，流式路径插的是
``\\n[stderr]\\n``。stdout 不以换行结尾时，日志连成 ``end[stderr]``——那 9 个字符
看起来就是命令输出。修法不是「补一个换行」，而是让补写也走 ``_Sink.write``：
同一件事只有一处实现，格式才不会分叉。

七个变异，每个都是一种「读起来没问题、实际把这件事做岔」的退化：

1. 分隔行去掉前导换行——就是本次修掉的那个 bug，必须能被抓回来；
2. offset 按字符数算——中文行的 offset 与日志字节对不上账；
3. stderr 起点退回自数游标——漏掉分隔行那 10 字节，增量读读到 ``[stderr]``；
4. 只有 stderr 时也插分隔行——起点从 0 变成 10，回读多出一行噪声；
5. 丢掉 stderr 的写入——stderr 只剩空，事件没了但没人报错；
6. flush 用 produced 当起点——没有换行结尾的最后一行，offset 指到日志尽头；
7. stderr 起点用 stdout 的起点——补写时拿写盘前的位置，正好漏掉分隔行那 10 字节。

用法：`python tests/mutation_battery_sandbox_backfill_v423.py`，退出码 0 = 全部被抓。
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

API = Path(__file__).resolve().parent.parent
PY = API / ".venv" / "Scripts" / "python.exe"
TARGET = API / "src" / "blank_api" / "services" / "shell_tasks.py"

TESTS = ["tests/test_sandbox_backfill_v423.py"]

MUTATIONS = [
    (
        "分隔行去掉前导换行",
        '        header = f"\\n[{tag}]\\n".encode("utf-8")',
        '        header = f"[{tag}]\\n".encode("utf-8")',
        "test_stderr_separator_is_its_own_line",
    ),
    (
        "offset 用字符数",
        "            splitter.feed(_STDOUT, stdout_bytes, stdout_start)",
        "            splitter.feed(_STDOUT, stdout_bytes, len(stdout))",
        "test_offsets_are_bytes_not_characters",
    ),
    (
        "stderr 起点退回自数游标",
        "            splitter.feed(_STDERR, stderr_bytes, stderr_start)",
        "            splitter.feed(_STDERR, stderr_bytes, None)",
        "test_stderr_starts_after_the_separator",
    ),
    (
        "只有 stderr 时也插分隔行",
        "        stderr_start = sink.write(_STDERR, stderr_bytes) if stderr_bytes else None",
        '        if stderr_bytes and not stdout_bytes:\n            sink.text("\\n[stderr]\\n")\n'
        "        stderr_start = sink.write(_STDERR, stderr_bytes) if stderr_bytes else None",
        "test_stderr_only_starts_at_zero",
    ),
    (
        "丢掉 stderr 的写入",
        "        stderr_start = sink.write(_STDERR, stderr_bytes) if stderr_bytes else None",
        "        stderr_start = None",
        "test_stderr_only_starts_at_zero",
    ),
    (
        "flush 用 produced 当起点",
        "                self._push(tag, raw, self._starts.get(tag, 0), len(raw))",
        "                self._push(tag, raw, self.record.produced, len(raw))",
        "test_flushed_line_without_newline_keeps_its_own_offset",
    ),
    (
        "stderr 起点用 stdout 的起点",
        "        stderr_start = sink.write(_STDERR, stderr_bytes) if stderr_bytes else None",
        "        stderr_start = (stdout_start or 0) if stderr_bytes else None",
        "test_stderr_starts_after_the_separator",
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
        print("[复原失败] shell_tasks.py 与跑之前不一致")
        failures += 1
    rc, red = run_tests()
    if rc != 0:
        print("还原后不绿：", red)
        failures += 1

    print(f"\n变异：{len(MUTATIONS) - failures}/{len(MUTATIONS)} 被抓")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
