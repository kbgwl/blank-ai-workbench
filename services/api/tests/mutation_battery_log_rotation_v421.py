"""后台日志轮转的变异电池（工具层第三刀）。

七条变异，每条都是一种"看起来像优化、实际把证据丢掉"的退化：

1. 溢出后不再写尾段——退回旧行为：只留开头，报错全丢；
2. 切点按字节硬切——文件末尾留下半个汉字，读回变替换符；
3. 压缩不记账——丢了多少字节没人知道，"只有这么多"和"少了一段"分不清；
4. 尾段不压缩——磁盘占用随运行时长线性增长；
5. 增量读不报"中间丢了"——调用方会以为看到的是连续的；
6. 增量读退回整段——每轮都把已经读过的内容再发一遍；
7. 重建 sink 时把头段长度当 0——取消标记写进已封笔的头段，位置错乱。

用法：`python tests/mutation_battery_log_rotation_v421.py`，退出码 0 = 全部被抓。
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

API = Path(__file__).resolve().parent.parent
PY = API / ".venv" / "Scripts" / "python.exe"
TARGET = API / "src" / "blank_api" / "services" / "shell_tasks.py"

TESTS = ["tests/test_shell_log_rotation_v421.py"]

MUTATIONS = [
    (
        "溢出后不写尾段（退回只留开头）",
        "        self._write_tail(data)\n\n    def _write_head(self, data: bytes) -> None:",
        "        return\n\n    def _write_head(self, data: bytes) -> None:",
        "test_latest_output_survives_after_head_is_full",
    ),
    (
        "切点按字节硬切",
        "            head_part = _cut_utf8(data, room)",
        "            head_part = data[:room]",
        "test_head_cut_lands_on_a_character_boundary",
    ),
    (
        "压缩不记账",
        "            self.record.omitted += omitted",
        "            self.record.omitted += 0",
        "test_compaction_records_how_much_it_dropped",
    ),
    (
        "尾段不压缩（磁盘无界）",
        "        if self.tail_bytes >= TAIL_LOG_BYTES:\n            self._compact_tail()",
        "        if False:\n            self._compact_tail()",
        "test_disk_usage_stays_bounded",
    ),
    (
        "增量读不报中间丢失",
        "    dropped = window > can_go_back",
        "    dropped = False",
        "test_incremental_read_reports_when_the_window_is_gone",
    ),
    (
        "增量读退回整段",
        "    read_bytes = min(window, can_go_back) if dropped else window",
        "    read_bytes = can_go_back",
        "test_incremental_read_returns_only_new_bytes",
    ),
    (
        "重建 sink 时把头段长度当 0",
        "            self.head_bytes = min(_file_size(record.log_path), HEAD_LOG_BYTES)",
        "            self.head_bytes = 0",
        "test_cancel_marker_goes_to_the_tail_segment",
    ),
    (
        "结束通知也带上头部",
        "    if not with_head:\n"
        "        return _decode(_drop_partial_prefix(_read_span(record.tail_path, limit)))",
        "    if False:\n"
        "        return _decode(_drop_partial_prefix(_read_span(record.tail_path, limit)))",
        "test_notification_tail_only_carries_the_end",
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
