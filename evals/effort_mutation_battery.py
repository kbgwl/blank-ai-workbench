"""effort 批判据的变异电池：把 effort 批新增的每条判定改坏，看判据红在哪些用例上。

跑法（不花钱，全程只动 `services/api/src/blank_api/services/` 下的几个源文件，
跑完按原行尾逐字节还原）：

    services/api/.venv/Scripts/python.exe -X utf8 evals/effort_mutation_battery.py

退出码与点验装置同一副纪律：`0` = 每条变异都红、还原后复跑全绿、红集合与登记逐字相等；
`1` = 有哑弹或与登记的红集合不一致；`2` = 电池自己没跑起来（基线不绿、锚点没命中、
源文件没能逐字节还原）。

期望红集合**按实测登记**在 `evals/fixtures/battery-booked-effort.json`：第一轮跑出来
什么就登记什么，第二轮起逐字核对。判据改了却不改登记，这里就打不平——这正是它的用处。
CHANGELOG 里那串「变异 N/N 红（… → N 红）」必须与这里的实测读数逐条对上，对不上就改
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
SVC = REPO / "services" / "api" / "src" / "blank_api" / "services"
EFFORT = SVC / "effort.py"
PROMPT_ARGS = SVC / "prompt_args.py"
SHELL_ENV = SVC / "shell_env.py"
ANTHROPIC = SVC / "models" / "anthropic.py"
OPENAI_COMPAT = SVC / "models" / "openai_compat.py"
TARGETS = (EFFORT, PROMPT_ARGS, SHELL_ENV, ANTHROPIC, OPENAI_COMPAT)
API = REPO / "services" / "api"
BOOK = HERE / "fixtures" / "battery-booked-effort.json"

# 只跑 effort 批自己的判据：这条文件是本批新加的，红集合也只该落在它身上。
TESTS = ("tests/test_effort_batch.py",)

MUTATIONS = [
    # M1 打钳制：clamp 不压，超过封顶的档原样返回。封顶形同虚设，钳制/告警/裁决三条都红。
    ("M1 clamp 不压", EFFORT,
     "        if cap is not None and rank(level) > rank(cap):\n"
     "            return cap\n"
     "        return level",
     "        return level"),
    # M2 打封顶取严：effective_cap 取成更松的一侧，封顶方向反了，封顶/可用档/钳制都红。
    ("M2 effective_cap 取松", EFFORT,
     "        return family if rank(family) <= rank(provider) else provider",
     "        return family if rank(family) >= rank(provider) else provider"),
    # M3 打关键词升档：keyword_effort 永远返回 None，关键词抬档被吞，关键词/裁决都红。
    ("M3 keyword 不抬档", EFFORT,
     "    keyword = detect_keyword(text)",
     "    keyword = None"),
    # M4 打占位符：${CLAUDE_EFFORT} 不替换，占位符原样发给模型，占位符/渲染两条都红。
    ("M4 占位符不替换", PROMPT_ARGS,
     "    if effort:\n"
     "        result = result.replace(\"${CLAUDE_EFFORT}\", str(effort))",
     "    if False:\n"
     "        result = result.replace(\"${CLAUDE_EFFORT}\", str(effort))"),
    # M5 打环境键：apply_effort 不写 CLAUDE_EFFORT，钩子/命令子进程读不到档位，环境键判据红。
    ("M5 环境键不写", SHELL_ENV,
     "    return {**env, CLAUDE_EFFORT_ENV: level}",
     "    return env"),
    # M6 打 adapter 载荷：anthropic 不下发 output_config.effort，模型侧拿不到档位，载荷判据红。
    ("M6 anthropic 不下发 effort", ANTHROPIC,
     "        if effort:\n"
     "            payload[\"output_config\"] = {\"effort\": effort}",
     "        if False:\n"
     "            payload[\"output_config\"] = {\"effort\": effort}"),
    # M7 打 thinking 关闭回落：clamp_without_thinking 永远不压，thinking 关断回落失效，回落判据红。
    ("M7 thinking 关闭不回落", EFFORT,
     "    if not thinking.disabled:\n"
     "        return effort, None",
     "    if True:\n"
     "        return effort, None"),
    # M8 打档位词表：词表被摘掉一档，词表/rank/多条判据集体红。
    ("M8 词表被改", EFFORT,
     "EFFORT_LEVELS: tuple[str, ...] = (\"low\", \"medium\", \"high\", \"xhigh\", \"max\")",
     "EFFORT_LEVELS: tuple[str, ...] = (\"low\", \"medium\", \"high\", \"xhigh\")"),
    # M9 打 openai_compat 映射：_reasoning_effort 永远回落配置静态值，档位映射失效，映射判据红。
    ("M9 openai_compat 不映射", OPENAI_COMPAT,
     "        if effort:\n"
     "            if effort in _OPENAI_EFFORTS:",
     "        if False:\n"
     "            if effort in _OPENAI_EFFORTS:"),
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
