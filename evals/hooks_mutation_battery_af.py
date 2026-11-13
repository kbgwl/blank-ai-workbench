"""AF 批判据的变异电池：把 `hooks.py` 里 AF 批新增的每条判定改坏，看判据红在哪些用例上。

跑法（不花钱，全程只动 `services/api/src/blank_api/services/hooks.py` 一个文件，
跑完按原行尾逐字节还原）：

    services/api/.venv/Scripts/python.exe -X utf8 evals/hooks_mutation_battery_af.py

退出码与点验装置同一副纪律：`0` = 每条变异都红、还原后复跑全绿、红集合与登记逐字相等；
`1` = 有哑弹或与登记的红集合不一致；`2` = 电池自己没跑起来（基线不绿、锚点没命中、
源文件没能逐字节还原）。

期望红集合**按实测登记**在 `evals/fixtures/battery-booked-af.json`：第一轮跑出来什么就
登记什么，第二轮起逐字核对。判据改了却不改登记，这里就打不平——这正是它的用处。
CHANGELOG 里那串「变异 8/8 红（… → N 红）」必须与这里的实测读数逐条对上，对不上就改
文档，不许改读数。
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
HOOKS = REPO / "services" / "api" / "src" / "blank_api" / "services" / "hooks.py"
TARGETS = (HOOKS,)
API = REPO / "services" / "api"
BOOK = HERE / "fixtures" / "battery-booked-af.json"

# 只跑 AF 批自己的判据：这两条文件是本批新加的，红集合也只该落在它们身上。
TESTS = ("tests/test_hook_triggers_af3ab.py", "tests/test_hook_kinds_af3c.py")

MUTATIONS = [
    # M1/M4 打的是同一条语义的两头：http 2xx 时上游把 statusCode 记成 exitCode、
    # 却仍是 success——退出码不等于成败。摘掉 `ok=True`（M1）或让 `_finish` 忽略
    # `ok`（M4），200 都会被当成失败，两条判据都该红。
    ("M1 http 2xx 去掉 ok=True", HOOKS,
     "            spec, event, text, \"\", exit_code=status_code, elapsed=elapsed, ok=True",
     "            spec, event, text, \"\", exit_code=status_code, elapsed=elapsed"),
    ("M4 _finish 忽略 ok", HOOKS,
     "        if ok is None:\n"
     "            status = \"ok\" if exit_code == 0 else \"failed\"\n"
     "        else:\n"
     "            status = \"ok\" if ok else \"failed\"",
     "        status = \"ok\" if exit_code == 0 else \"failed\""),
    # M2/M3 打 script 型：类型集合里摘掉 script，执行时就认不出来（未知类型告警跳过，
    # 而不是报上游那句）；或者认出来了、却把那句文案改坏。
    ("M2 HOOK_KINDS 摘 script", HOOKS,
     "HOOK_KINDS = frozenset({\"command\", \"prompt\", \"agent\", \"http\", \"mcp_tool\", \"script\"})",
     "HOOK_KINDS = frozenset({\"command\", \"prompt\", \"agent\", \"http\", \"mcp_tool\"})"),
    ("M3 script 措辞改坏", HOOKS,
     "            reason=\"script hooks are not available in this build\",",
     "            reason=\"script hooks are unavailable in this build\","),
    # M5 打事件归类：user_prompt_expansion 与 user_prompt_submit 同族，退出码 2 就是
    # "这次展开作废"。摘出阻断集合，退出码 2 会被当成一条非阻断错误，否决直接失效。
    ("M5 user_prompt_expansion 摘出阻断集合", HOOKS,
     "        \"user_prompt_expansion\",\n        \"task_created\",",
     "        \"task_created\","),
    # M6 打抑制原文：钩子要求 suppressOriginalPrompt 时，阻断消息里要略去用户原文。
    ("M6 展开钩子忽略 suppress_original", HOOKS,
     "        suppressed = any(item.suppress_original for item in results)",
     "        suppressed = False"),
    # M7/M8 打 TeammateIdle 的两条处置：退出码 2 只算一条非阻断错误（不能变成"阻止
    # 继续"），缺 stopReason 时用上游那句缺省文案。
    ("M7 idle 把退出码 2 当阻止", HOOKS,
     "    prevented = next((item for item in results if item.prevent_continuation), None)",
     "    prevented = next(\n"
     "        (item for item in results if item.prevent_continuation or item.exit_code == 2), None\n"
     "    )"),
    ("M8 idle 缺省文案改坏", HOOKS,
     "        reason = (prevented.stop_reason or DEFAULT_IDLE_STOP_REASON)[:300]",
     "        reason = (prevented.stop_reason or \"\")[:300]"),
]


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def run_pytest() -> tuple[list[str], int]:
    proc = subprocess.run(
        [str(API / ".venv" / "Scripts" / "python.exe"), "-X", "utf8", "-E", "-m", "pytest",
         "-q", *TESTS, "-rf", "--no-header"],
        cwd=str(API), capture_output=True, text=True, encoding="utf-8", errors="replace")
    # 用例名是中文，靠 `-X utf8` 让 pytest 按 UTF-8 出，别去解析终端的 GBK 码页。
    # 行尾 pytest 还会拼上 "- AssertionError: ..."，那一段随终端宽度截断，拿它当登记键
    # 等于把读数绑在排版上，所以只取 " - " 之前的节点名。
    fails = sorted({m.split(" - ")[0].strip()
                    for m in re.findall(r"^FAILED \S+::(.+)$", proc.stdout, re.M)})
    return fails, proc.returncode


def load(path: Path) -> tuple[str, str]:
    """正文按 `\n` 供锚点匹配，行尾风格单独记下来。

    直接 `read_text` / `write_text` 在 Windows 上会把 LF 翻成 CRLF，跑完一轮电池就
    把源文件的行尾无声改了——「逐字节还原」那句承诺就是假的。混合行尾的文件一律先
    拒绝：那种文件还原会把裸 LF 那几行悄悄翻成 CRLF，红的原因是电池自己改了行尾，
    别把产品的账记到装置头上。
    """
    raw = path.read_bytes()
    crlf = raw.count(b"\r\n")
    bare = raw.count(b"\n") - crlf
    if crlf and bare:
        raise AssertionError(
            f"{path.name} 行尾混合（CRLF={crlf} 裸 LF={bare}）："
            "电池按单一行尾还原，会把另一种翻掉。先归一化再跑。"
        )
    return raw.decode("utf-8").replace("\r\n", "\n"), ("\r\n" if crlf else "\n")


def save(path: Path, text: str, newline: str) -> None:
    path.write_bytes(text.replace("\n", newline).encode("utf-8"))


def apply(path: Path, old: str, new: str) -> tuple[str, str]:
    text, newline = load(path)
    assert text.count(old) == 1, f"锚点命中 {text.count(old)} 次：{path.name}\n{old[:60]}"
    assert new != old, "变异与原样相同：这条是 no-op，先证非 no-op 再进电池"
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
