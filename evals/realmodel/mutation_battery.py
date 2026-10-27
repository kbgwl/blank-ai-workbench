"""装置变异电池：把装置本身改坏，看离线自检红在哪些用例上。

跑法（不花钱，全程只动 `evals/realmodel/{lib,judgments}.py` 两个文件，跑完逐字节还原）：

    services/api/.venv/Scripts/python.exe -X utf8 evals/realmodel/mutation_battery.py

退出码与点验装置同一副纪律：`0` = 每条变异都红、还原后复跑全绿；
`1` = 有哑弹或与登记的红集合不一致；`2` = 电池自己没跑起来（基线不绿、锚点没命中）。

期望红集合**按实测登记**在 `fixtures/battery-booked-v120.json`，第一轮跑出来什么就登记
什么，第二轮起逐字核对。判据改了却不改登记，这里就打不平——这正是它的用处。
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
LIB = HERE / "lib.py"
JUD = HERE / "judgments.py"
CFG = HERE / "hooks_config.py"
DRV = HERE / "async_hooks_v119.py"
TARGETS = (LIB, JUD, CFG, DRV)
BOOK = HERE / "fixtures" / "battery-booked-v120.json"
API = REPO / "services" / "api"
TESTS = "tests/test_realmodel_device_v120.py"

MUTATIONS = [
    ("M1 对照格没红不再判作废", LIB,
     "    if control_red is False:\n        return EXIT_INCONCLUSIVE",
     "    if control_red is True:\n        return EXIT_INCONCLUSIVE"),
    ("M2 判据红也返回 0", LIB,
     "    return EXIT_RED if fails else EXIT_OK",
     "    return EXIT_OK"),
    ("M3 缺配置不再如实跳过而是硬跑", LIB,
     "    if skip:\n        return EXIT_OK",
     "    if skip:\n        return EXIT_RED"),
    ("M4 取数把缺键读成 0", LIB,
     "    if key not in obj:\n        return None",
     "    if key not in obj:\n        return 0"),
    ("M5 唤醒来源不看钩子事件", JUD,
     '            and any(s.get("event") == "post_tool_use" for s in p.get("sources", []))\n            for p in injected',
     "            for p in injected"),
    ("M6 句柄比对改成宽松存在", JUD,
     '    rec.check("116 模型用同一个句柄调 task_output", bool(asked_ids & handle_ids),',
     '    rec.check("116 模型用同一个句柄调 task_output", bool(asked_ids) or bool(handle_ids),'),
    ("M7 阻塞判据只比条数", JUD,
     "              bool(spawn) and all(isinstance(d, int) for d in durations)\n              and max(durations) < 15000,",
     "              bool(spawn),"),
    ("M8 编码格相等改成不小", JUD,
     "              bool(chars) and max(chars) == len(hook_text),",
     "              bool(chars) and max(chars) >= len(hook_text),"),
    ("M9 对照格归属反读", JUD,
     "    return CONTROL_RED_NAME in rec.fails",
     "    return CONTROL_RED_NAME not in rec.fails"),
    ("M10 回退点条数判据放宽成存在", JUD,
     '    rec.check("117 回退点含两轮", len(points) == 2,',
     '    rec.check("117 回退点含两轮", len(points) >= 1,'),
    ("M11 事件类型名打错（沉默判据风险）", JUD,
     '    wake = of_type(events, "hook.wakeup_delivered")\n    rec.check("119 唤醒投递事件"',
     '    wake = of_type(events, "hook.wakeup")\n    rec.check("119 唤醒投递事件"'),
    ("M12 包装语常量漂移", LIB,
     'WAKE_WRAPPER = "后台钩子以退出码 2 结束："',
     'WAKE_WRAPPER = "后台钩子以退出码 2 结束:"'),
    # M13/M14 打的是 #120 这一批新加严的那一格：改回「文件里出现过 worktree_remove」
    # 这种宽形状，或改回「拿原文去子串匹配转义过的 JSON」，都该被电池捉住。
    ("M13 清理通知退回只问审计文件里出现过", JUD,
     '    rec.check("118 清理通知里带着本轮副本的路径",\n'
     '              bool(mine) and any(Path(str(r.get("path") or "")) in mine for r in removals),',
     '    rec.check("118 清理通知里带着本轮副本的路径",\n'
     '              any("worktree_remove" in line for line in hook_log_lines) or bool(removals),'),
    ("M14 审计行解析退回子串匹配", JUD,
     '        try:\n            row = json.loads(line)\n        except ValueError:\n            continue',
     '        row = {"path": line} if "worktree_remove" in line else None'),
    ("M15 实例活着判据退回 4xx 也算在线", LIB,
     "            return r.status_code < 400",
     "            return r.status_code < 500"),
    ("M16 健康路由打到裸 /health", LIB,
     '        return f"{self.api}{HEALTH_PATH}"',
     '        return f"{self.base}{HEALTH_PATH}"'),
    # M17–M19 是 #120 真跑翻车那一条的形状：姿态没钉住就花钱。
    ("M17 姿态闸门短路（回到白跑三格那副样子）", LIB,
     "    fresh = [cell for cell in cells if not reuse.get(cell)]",
     "    fresh = []"),
    ("M18 钩子配置把 async 挂到 worktree_create", CFG,
     "        if event in ASYNC_EVENTS:",
     "        if event in SYNC_EVENTS:"),
    ("M19 钩子配置自检永远说没问题", CFG,
     '    except json.JSONDecodeError as exc:\n        return [f"BLANK_HOOKS 不是合法 JSON：{exc}"]',
     '    except json.JSONDecodeError:\n        return []'),
    # M20/M21 是这次闸门把合法跑法判成失效那一条的形状：变量名有两套。
    ("M20 闸门按格子名拼复用变量（回到 RM_TASK_CONTROL）", DRV,
     '"control": "RM_TASK_CTL"}',
     '"control": "RM_TASK_CONTROL"}'),
    ("M21 格子名写错不再拦", DRV,
     "    unknown = [c for c in cells if c not in TASK_ENV]",
     "    unknown: list[str] = []"),
]


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def run_pytest() -> tuple[list[str], int]:
    proc = subprocess.run(
        [str(API / ".venv" / "Scripts" / "python.exe"), "-X", "utf8", "-E", "-m", "pytest",
         "-q", TESTS, "-rf", "--no-header"],
        cwd=str(API), capture_output=True, text=True, encoding="utf-8", errors="replace")
    # 参数化用例的名字里带空格与方括号，用 (.+)$ 收全，别用 \S+（会截成 "test_x[119"）；
    # 行尾 pytest 还会拼上 "- AssertionError: ..."，那一段随终端宽度截断，
    # 拿它当登记键等于把读数绑在排版上，所以只取 " - " 之前的节点名。
    fails = sorted({m.split(" - ")[0].strip()
                    for m in re.findall(r"^FAILED \S+::(.+)$", proc.stdout, re.M)})
    return fails, proc.returncode


def load(path: Path) -> tuple[str, str]:
    """正文按 `\n` 供锚点匹配，行尾风格单独记下来。

    上一版直接 `read_text` 读、`write_text` 写：Windows 上写出去会翻成 CRLF，
    LF 的文件跑完一轮电池就被无声改了行尾——「逐字节还原」那句承诺当时是假的。
    """
    raw = path.read_bytes()
    newline = "\r\n" if b"\r\n" in raw else "\n"
    return raw.decode("utf-8").replace("\r\n", "\n"), newline


def save(path: Path, text: str, newline: str) -> None:
    path.write_bytes(text.replace("\n", newline).encode("utf-8"))


def apply(path: Path, old: str, new: str) -> tuple[str, str]:
    text, newline = load(path)
    assert text.count(old) == 1, f"锚点命中 {text.count(old)} 次：{path.name}\n{old[:60]}"
    save(path, text.replace(old, new), newline)
    return text, newline


def main() -> int:
    before = {str(p): sha(p) for p in TARGETS}
    results: dict[str, list[str]] = {}
    baseline_fails, baseline_rc = run_pytest()
    print(f"基线 rc={baseline_rc} 红={baseline_fails}")
    if baseline_rc != 0:
        print("[ABORT] 基线不绿，电池没意义")
        return 2

    for label, path, old, new in MUTATIONS:
        try:
            original, newline = apply(path, old, new)
        except AssertionError as exc:
            print(f"[ABORT] {label}: {exc}")
            return 2
        try:
            fails, rc = run_pytest()
        finally:
            save(path, original, newline)
        if sha(path) != before[str(path)]:
            print(f"[ABORT] {label} 之后 {path.name} 没回到原字节，电池停在这里")
            return 2
        results[label] = fails
        print(f"{label}: rc={rc} 红 {len(fails)} -> {fails}")
        if not fails:
            print(f"!! {label} 没红：这条变异是哑弹")

    if {str(p): sha(p) for p in TARGETS} != before:
        print("[ABORT] 源文件没能逐字节还原，后面的读数不可信")
        return 2
    verify_fails, verify_rc = run_pytest()
    print(f"还原后复跑 rc={verify_rc} 红={verify_fails}")

    if BOOK.exists():
        booked = json.loads(BOOK.read_text(encoding="utf-8"))
        mismatch = {k: {"登记": booked.get(k), "实测": v} for k, v in results.items()
                    if booked.get(k) != v}
        if mismatch or verify_rc != 0:
            print("[MISMATCH] " + json.dumps(mismatch, ensure_ascii=False, indent=1))
            return 1
        print("[ALL-OK] 红集合与登记逐字相等，源文件已还原，复跑全绿")
        return 0
    BOOK.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[BOOKED] 实测红集合已登记（{len(results)} 条变异），第二轮起做逐字核对")
    return 0 if verify_rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
