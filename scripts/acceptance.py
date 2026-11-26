#!/usr/bin/env python
"""执行式验收：不问"功能在不在"，问"用户打开看到什么、做完一件事有没有变化"。

## 为什么要有这个东西

夜班 L6 交了一个任务看板：三条判据（可拖拽 / localStorage 能恢复 / 无第三方库 /
无控制台报错）**全部真通过**，模型也确实派事件验过拖拽。但用户打开看到的是三个
空框，第一句话是"就做了个这个？"。

这两件事不矛盾。它验的是**自己临时造的现场**（脚本里塞三条数据、拖一下、刷新、
看到三条还在），交出去的是**没有数据的文件**。判据里没有任何一条问"打开时看见
什么"，所以"打开是空的"不算失败。

结论：判据的形状决定产出的形状。全是"不许有错"的判据，最优解就是**什么都不做**
——空页面永远不会有控制台错误。

## 一趟验收分两段

- `open`：页面刚打开、**用户还没动**的时候，必须有什么。
  这一段专门用来杀空壳：没有种子数据、没有首屏内容的交付物在这里就死了。
- `steps`：走一遍真实动作（填、点、拖、刷新），每一步断言**动作之后的状态**。

动作写法见 `acceptance/kanban.json`。断言一律写成"表达式 + 期望"，表达式在页面里
求值，返回布尔或数字（配 `min`）。

## 用法

    python scripts/acceptance.py --journey acceptance/kanban.json
    python scripts/acceptance.py --journey x.json --page other.html --shot out.png

退出码：0 全部通过；1 有任一条没过；2 跑不起来（探针/浏览器/文件问题）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

REPO = Path(__file__).resolve().parent.parent
PROBE = REPO / "services" / "api" / "scripts" / "browser_probe.mjs"
CANDIDATES = (
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
)


def find_node() -> str:
    explicit = os.environ.get("BLANK_NODE") or ""
    if explicit and Path(explicit).exists():
        return explicit
    found = shutil.which("node")
    if found:
        return found
    for candidate in ("E:/env/node/node-v24.20.0-win-x64/node.exe", "/usr/local/bin/node"):
        if Path(candidate).exists():
            return candidate
    return ""


def find_browser() -> str:
    explicit = os.environ.get("BLANK_CHROME") or ""
    return next((p for p in (explicit, *CANDIDATES) if p and Path(p).is_file()), "")


def as_url(page: str) -> str:
    if page.startswith(("http://", "https://", "file://")):
        return page
    path = Path(page).resolve()
    if path.is_dir():
        path = path / "index.html"
    if not path.is_file():
        raise SystemExit(f"页面不存在：{page}")
    return "file:///" + quote(path.as_posix(), safe="/:")


def load_journey(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("name", path.stem)
    data.setdefault("open", [])
    data.setdefault("steps", [])
    return data


def run_probe(url: str, steps: list[dict], shot: str | None) -> dict:
    node = find_node()
    if not node:
        raise SystemExit("没有找到 node，验收跑不起来（设置 BLANK_NODE）")
    if not find_browser():
        raise SystemExit("没有找到 Chrome/Edge，验收跑不起来（设置 BLANK_CHROME）")
    if not PROBE.is_file():
        raise SystemExit(f"探针脚本缺失：{PROBE}")

    with tempfile.TemporaryDirectory(prefix="acceptance-") as tmp:
        step_file = Path(tmp) / "steps.json"
        step_file.write_text(json.dumps(steps, ensure_ascii=False), encoding="utf-8")
        argv = [node, str(PROBE), "--url", url, "--steps", str(step_file), "--wait", "600"]
        if shot:
            shot_path = Path(shot).resolve()
            shot_path.parent.mkdir(parents=True, exist_ok=True)
            argv.extend(["--shot", str(shot_path)])
            # 末态图证明不了"打开时是什么样"。首屏单独留一张，两张图摆一起才看得出差别。
            argv.extend(["--shot-open", str(shot_path.with_name(shot_path.stem + "-open" + shot_path.suffix))])
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=180)
    lines = (proc.stdout or "").strip().splitlines()
    if not lines:
        raise SystemExit(f"探针没有输出：{(proc.stderr or '')[-400:]}")
    try:
        result = json.loads(lines[-1])
    except json.JSONDecodeError:
        raise SystemExit(f"探针输出无法解析：{lines[-1][:300]}")
    if result.get("fatal"):
        raise SystemExit(f"浏览器没能启动：{result['fatal']}")
    return result


def render(name: str, url: str, open_count: int, result: dict) -> tuple[str, int]:
    records = result.get("steps") or []
    head = records[:open_count]
    body = records[open_count:]
    errors = [item for item in result.get("console") or [] if item.get("level") == "error"]
    failed = [item for item in records if not item.get("ok")]

    out: list[str] = [f"验收：{name}", f"页面：{url}", ""]
    for title, items in (("打开（用户还没动手）", head), ("旅程（做一遍真实动作）", body)):
        if not items:
            continue
        out.append(title)
        for item in items:
            mark = "✓" if item.get("ok") else "✗"
            label = item.get("text") or item.get("do") or ""
            got = item.get("actual")
            got_text = "" if got in (None, True) else f"　实际：{_brief(got)}"
            out.append(f"  {mark} {label}{got_text}")
        out.append("")

    if errors:
        out.append(f"控制台错误 {len(errors)} 条：")
        for item in errors[:5]:
            out.append(f"  · {str(item.get('text'))[:160]}")
        out.append("")

    # 结论：空壳要和"某条没做对"分开说，否则用户看不出这是两类问题。
    empty = [item for item in head if not item.get("ok")]
    if empty and not body:
        verdict = "不可交付：打开是空的，什么也没证明"
    elif empty:
        verdict = "不可交付：第一屏就不成立，后面做得再对也没用"
    elif failed:
        verdict = f"不可交付：{len(failed)} 条没过"
    elif errors:
        verdict = "不可交付：功能走通了，但控制台有错"
    else:
        verdict = "通过：打开有东西，动作有变化，刷新还在"
    out.append(f"结论：{verdict}")
    out.append(f"截图：{result.get('shots', {}).get('png', '未落盘')}")
    return "\n".join(out), (0 if not failed and not errors else 1)


def _brief(value) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    text = str(value)
    return text if len(text) <= 60 else text[:60] + "…"


def main() -> int:
    parser = argparse.ArgumentParser(description="执行式验收：打开时有什么、动一下变不变")
    parser.add_argument("--journey", required=True, help="验收脚本 JSON")
    parser.add_argument("--page", help="页面路径或 URL（默认取 journey 里的 page）")
    parser.add_argument("--shot", help="截图落盘路径")
    parser.add_argument("--json", action="store_true", help="只输出原始结果")
    args = parser.parse_args()

    journey_path = Path(args.journey)
    if not journey_path.is_file():
        return _fail(f"验收脚本不存在：{journey_path}")
    journey = load_journey(journey_path)
    page = args.page or journey.get("page")
    if not page:
        return _fail("没有指定页面：用 --page 或写在 journey 的 page 字段里")
    if not Path(page).is_absolute() and not page.startswith(("http", "file://")):
        base = Path.cwd() if args.page else journey_path.parent
        page = str((base / page).resolve())

    url = as_url(page)
    steps = list(journey["open"]) + list(journey["steps"])
    result = run_probe(url, steps, args.shot)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0 if result.get("ok") else 1
    text, code = render(journey["name"], url, len(journey["open"]), result)
    print(text)
    return code


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
