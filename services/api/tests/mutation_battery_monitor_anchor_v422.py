"""monitor 事件字节锚的变异电池（工具层第四刀）。

八条变异，每条都是一种「看起来还行、实际把锚点弄丢」的退化：

1. end 按截断后的长度算——用 end 增量读会重复读到被截掉的尾巴；
2. 空行不计字节——每个空行都让后面所有 offset 集体偏移；
3. offset 按解码后的字符数算——中文行的 offset 与日志文件对不上账；
4. 丢事件后不调整区间左界——通知里的区间从投递的事件开始，丢掉那段无迹可寻；
5. from_offset 固定写成 0——第二轮投递看起来像从头开始；
6. 上报后不清零丢弃计数——同一个丢弃被反复上报；
7. 空行也产生事件——空白行挤掉真正的输出；
8. 起点不算 tag 分隔行——stderr 的 offset 比日志里的真实位置少几个字节；
9. 无换行尾行的 end 当 0——最后一行没有换行时，用 end 会从头重读它。

用法：`python tests/mutation_battery_monitor_anchor_v422.py`，退出码 0 = 全部被抓。
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

API = Path(__file__).resolve().parent.parent
PY = API / ".venv" / "Scripts" / "python.exe"
TARGET = API / "src" / "blank_api" / "services" / "shell_tasks.py"

TESTS = ["tests/test_monitor_log_anchor_v422.py"]

MUTATIONS = [
    (
        "end 用截断后的长度",
        "        end = start + max(0, size)",
        "        end = start + max(0, min(size, MONITOR_MAX_EVENT_CHARS + 1))",
        "test_truncated_line_keeps_the_original_end",
    ),
    (
        "空行不计字节",
        "            pos += len(raw) + 1",
        "            pos += (len(raw) + 1) if raw else 0",
        "test_blank_lines_do_not_shift_later_offsets",
    ),
    (
        "offset 用解码后的字符数",
        "            pos += len(raw) + 1",
        "            pos += len(_decode(raw)) + 1",
        "test_offsets_advance_line_by_line_in_bytes",
    ),
    (
        "丢弃事件后不调整区间",
        "            first = dropped_from if dropped_from is not None else _first_event_offset(events)",
        "            first = _first_event_offset(events)",
        "test_dropped_events_keep_the_covered_range",
    ),
    (
        "from_offset 固定写成 0",
        '            note["from_offset"] = int(first)',
        '            note["from_offset"] = 0',
        "test_second_drain_starts_where_the_previous_ended",
    ),
    (
        "上报后不清零丢弃计数",
        "        record.dropped_events = 0\n        record.dropped_events_from = None",
        "        record.dropped_events = dropped\n        record.dropped_events_from = None",
        "test_second_drain_starts_where_the_previous_ended",
    ),
    (
        "空行也产生事件",
        "        if not line.strip():\n            return",
        "        if False:\n            return",
        "test_blank_lines_do_not_shift_later_offsets",
    ),
    (
        "起点不算 tag 分隔行",
        "        start = record.produced + len(header)",
        "        start = record.produced",
        "test_tag_switch_header_does_not_shift_offsets",
    ),
    (
        "无换行尾行的 end 当 0",
        "                self._push(tag, raw, self._starts.get(tag, 0), len(raw))",
        "                self._push(tag, raw, self._starts.get(tag, 0), 0)",
        "test_tail_line_without_newline_still_has_an_end",
    ),
    (
        # 这一条是这次实际写出来又改回去的回退：写盘失败时把切行一起跳过，
        # 等于「日志没了，模型也瞎了」。判据必须把它挡住。
        "写盘失败就连事件一起停",
        "            splitter.feed(tag, chunk, start)",
        "            if start is not None:\n                splitter.feed(tag, chunk, start)",
        "test_events_survive_a_dead_log_handle",
    ),
    (
        "兜底游标每块都从 0 起",
        "            start = self._cursor",
        "            start = 0",
        "test_events_survive_a_dead_log_handle",
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
        if text.count(old_eol) != 1:
            print(f"[锚点不符] {label}：出现 {text.count(old_eol)} 次")
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
