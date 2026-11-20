#!/usr/bin/env python
"""夜班压测：从小任务一路加到项目级，全程只记录、不改产品代码。

## 三条纪律（用户 2026-11-20 明确）

1. **只测试，不修**。发现的问题一律写进 `issues.md`，不动一行产品代码。
   理由：夜里改的东西没人复核，第二天分不清"原本的缺陷"和"夜里改出来的缺陷"。
2. **不外发**。所有结果只落本机文件，不推送、不发消息。
3. **等级要升**。每轮取阶梯上还没做过的最高一级，不允许反复跑同一级刷数量。

## 一轮做什么

建任务 → 跑到终态（无人值守时自动批审批）→ 收集事实 → 静态评分 → 记问题。

评分是**启发式**的，不是测量：能编译、有测试、测试过、没有明显坏味道。
它只用来横向比"这一版交付的成色"，不能当成"代码质量"的绝对值。

用法：

    python scripts/night_shift.py --next      # 跑下一级
    python scripts/night_shift.py --status    # 看进度
    python scripts/night_shift.py --report    # 生成早报
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

API = "http://127.0.0.1:8000/api/v1"
REPO = Path(__file__).resolve().parent.parent
API_DIR = REPO / "services" / "api"
ROOT = Path("D:/kbg harness/night-shift")
WORKSPACE = ROOT / "workspace"
LEDGER = ROOT / "ledger.jsonl"
ISSUES = ROOT / "issues.md"
STATE = ROOT / "state.json"
LOCK = ROOT / ".lock"
HEALTH_URL = "http://127.0.0.1:8000/api/v1/health"

# 阶梯：从小工具到项目级，中间插一次代码审查（不是所有等级都是"写代码"）。
LADDER: list[dict] = [
    {
        "level": 1,
        "name": "单文件工具",
        "kind": "dev",
        "goal": "写一个 Python 模块 strutil.py，实现三个函数：slugify(文本)、truncate(文本, 长度, 省略号='…')、word_count(文本)。要求：中英文都要能处理，空输入返回合理结果，不要抛异常。同时写 tests/test_strutil.py 覆盖空串、纯中文、纯英文、超长截断四种情况，并用 run_tests 跑到通过。",
        "criteria": ["strutil.py 三个函数都能导入", "tests/test_strutil.py 覆盖四种边界", "run_tests 退出码为 0"],
    },
    {
        "level": 2,
        "name": "单模块 CLI",
        "kind": "dev",
        "goal": "写一个命令行工具 csvstat.py：读取 CSV，输出每个数值列的最小值/最大值/均值/缺失数，以及行数。要求用 argparse 支持 --file 和 --columns（逗号分隔，可选）；文件不存在或列名不存在时给出清楚的中文错误并以非零退出码结束，不要抛 traceback。附带 tests/test_csvstat.py，用 tmp 文件构造用例，run_tests 跑到通过。",
        "criteria": ["csvstat.py 可直接 python 执行", "非法输入走中文错误而非 traceback", "tests 通过"],
    },
    {
        "level": 3,
        "name": "多模块小型库",
        "kind": "dev",
        "goal": "写一个小型库 miniqueue/：一个纯内存的优先级队列（push/pop/peek/size），支持同优先级 FIFO，pop 空队列时抛自定义 EmptyQueueError。拆成 package：__init__.py 导出公开接口、core.py 放实现、errors.py 放异常。补 tests/ 覆盖：同优先级保序、空队列异常、一万次随机读写后 size 一致。写 README.md 说明用法。run_tests 必须通过。",
        "criteria": ["包结构可分模块导入", "同优先级保序", "空队列抛自定义异常", "一万次随机读写后 size 一致", "tests 通过"],
    },
    {
        "level": 4,
        "name": "代码审查",
        "kind": "review",
        "goal": "在 workspace 下有一个 legacy/ 目录，里面是别人写的三个模块。请做一次代码审查：找出其中的真实缺陷（不要改代码，只报告），每条给出文件、行号、问题、为什么是问题、严重程度（高/中/低）。重点看：资源泄漏、边界条件、并发安全、错误处理。把结论写成 review-report.md。",
        "criteria": ["报告定位到具体文件与行号", "至少指出 3 个真实缺陷", "每条给出严重程度与理由"],
    },
    {
        "level": 5,
        "name": "中型 Web API",
        "kind": "dev",
        "goal": "用 FastAPI 写一个待办事项服务 app/：支持创建、列表（可按完成状态过滤）、完成、删除四个接口，数据存 SQLite（用标准库 sqlite3，不要引入 ORM）。要求：输入校验（空标题拒绝、标题超 200 字拒绝）、统一错误响应格式、每个接口都有 tests 覆盖（用 httpx 的 TestClient）。写 README 说明接口与启动方式。run_tests 必须通过。",
        "criteria": ["四个接口齐全", "输入校验生效", "错误响应格式统一", "tests 通过", "README 说明启动方式"],
    },
    {
        "level": 6,
        "name": "前端交互页",
        "kind": "dev",
        "journey": "kanban.json",
        "page": "index.html",
        # 2026-11-20 改：原判据是四条"不许有错"（不引第三方库 / 无控制台错误 / 可拖拽 /
        # 能恢复），空页面天然满足其中两条，剩下两条由模型自己派事件验过就算过，
        # 于是交付了一个打开是三个空框的看板。判据改成"打开时有什么 + 做一遍动作"。
        "goal": "写一个单文件 index.html 的「任务看板」：三列（待办/进行中/完成），支持新增任务、拖动跨列、删除、按关键字过滤，刷新后从 localStorage 恢复。要求：不引入任何第三方库，键盘可达（Tab 能走到所有可操作元素）。**交付文件必须自带 3 条示例任务**（例如「梳理本周需求」「修复登录超时」「写发布说明」），分布在至少两列，首屏有每列计数与总数——不允许交一个空板子，也不允许要求用户先输入才看得出它是什么。写完后用 browser_check 自检：打开时卡片 ≥3 且分布在 ≥2 列；加一条后 +1；拖到进行中后刷新仍在；过滤到 1 条时只剩 1 张；控制台零错误。",
        "criteria": [
            "打开 index.html 首屏就有 ≥3 张卡片，且分布在 ≥2 列",
            "输入并点添加后，卡片数比之前多 1 张",
            "拖到「进行中」之后刷新，它仍在该列",
            "过滤到只剩 1 张时，列表确实只有 1 张",
            "控制台零错误",
        ],
    },
    {
        "level": 7,
        "name": "多模块项目",
        "kind": "dev",
        "goal": "写一个日志分析工具 logscope/：把 nginx 风格的 access.log 解析成统计结果——按小时请求量、状态码分布、Top10 路径、平均响应时间、独立 IP 数。要求模块化（parser/store/report 分离）、支持 --top N、输出人类可读表格与 --json 两种格式、大文件流式读取不许一次性读进内存。自带一份样例日志 fixtures/sample.log 与 tests，run_tests 必须通过。",
        "criteria": ["模块化分离", "支持 --json 与表格", "流式读取", "样例日志与 tests 齐备", "tests 通过"],
    },
    {
        "level": 8,
        "name": "重构与回归",
        "kind": "dev",
        "goal": "workspace/legacy2/ 里有一个 400 行的单文件脚本 monolithic.py，行为正确但结构糟糕。请把它重构成模块化的包 refactored/，**保持行为完全一致**：先补一组针对原脚本的回归测试，跑绿之后再重构，重构后同一组测试必须仍然全绿。不允许改变任何对外输出格式。最后说明你做了哪些拆分以及为什么。",
        "criteria": ["先补回归测试再改", "重构前后同一组测试都绿", "对外输出格式不变", "说明拆分理由"],
    },
    {
        "level": 9,
        "name": "交叉审查",
        "kind": "review",
        "goal": "workspace 下是前面几轮自动产出的代码（strutil/csvstat/miniqueue/app/logscope 等）。请对它们做一次真实代码审查：挑出最严重的 5 个问题，每条给出文件、行号、问题、后果、严重程度，并判断这些代码「如果上线会先炸在哪里」。不要修改任何文件，只写 review-pass2.md。",
        "criteria": ["报告基于真实文件与行号", "每条给出后果判断", "不改任何文件"],
    },
    {
        "level": 10,
        "name": "跨产物整合",
        "kind": "dev",
        "goal": "把 workspace 里已有的 csvstat.py（CSV 统计）与 logscope/（日志分析）整合成一个统一 CLI toolbox.py：子命令 csv 与 log，共用一套参数解析、统一的中文错误处理与退出码约定（0 成功 / 1 输入错误 / 2 用法错误），共用 --json。要求保持原有功能全部可用，并补一组集成测试证明两个子命令都能跑。",
        "criteria": ["两个子命令都可用", "退出码约定统一", "共用 --json", "集成测试通过"],
    },
    {
        "level": 11,
        "name": "性能与正确性",
        "kind": "dev",
        "goal": "给 logscope 加一个 --profile 选项：处理大文件时输出解析耗时、峰值内存（用 tracemalloc）、每秒处理行数。并写一个基准脚本 bench.py 生成 20 万行样例日志、跑三次取中位数。要求处理过程仍是流式的（内存不随文件增大线性增长），用数据说明这一点。",
        "criteria": ["--profile 输出耗时/内存/吞吐", "bench.py 可复现", "用数据证明流式"],
    },
]

MINUTES_PER_ROUND = 45


# --------------------------------------------------------------------------- #
# 基础设施
# --------------------------------------------------------------------------- #

def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def call(method: str, path: str, body: dict | None = None, timeout: float = 30) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"{API}{path}", data=data, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode() or "{}"
        return json.loads(raw)


def healthy() -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=5) as response:
            return response.status == 200
    except Exception:  # noqa: BLE001
        return False


def ensure_backend() -> bool:
    """后端必须活着。夜里没人看着，起不来就整轮跳过并记一笔。"""
    if healthy():
        return True
    log = ROOT / "backend.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    python = API_DIR / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        record_issue("infra", "找不到后端解释器", str(python), severity="high")
        return False
    with open(log, "ab") as handle:
        subprocess.Popen(  # noqa: S603 - 固定路径，无 shell
            [str(python), "-m", "uvicorn", "blank_api.app:app", "--app-dir", "src",
             "--host", "127.0.0.1", "--port", "8000"],
            cwd=str(API_DIR),
            stdout=handle,
            stderr=handle,
            start_new_session=True,
        )
    for _ in range(40):
        if healthy():
            return True
        time.sleep(3)
    record_issue("infra", "后端起不来", "40 次健康检查都没通，见 backend.log", severity="high")
    return False


def ensure_project() -> str | None:
    """一个专用项目 + 专用工作区。产物不落进产品仓库。"""
    state = load_state()
    project_id = str(state.get("project_id") or "")
    if project_id:
        try:
            call("GET", f"/projects/{project_id}")
            return project_id
        except Exception:  # noqa: BLE001
            project_id = ""
    try:
        project = call("POST", "/projects", {"name": "夜班压测", "description": "自动压测产物，可整目录删除"})
        project_id = str(project["id"])
        WORKSPACE.mkdir(parents=True, exist_ok=True)
        call("POST", f"/projects/{project_id}/workspaces", {"path": str(WORKSPACE), "label": "夜班"})
        state["project_id"] = project_id
        save_state(state)
        return project_id
    except Exception as exc:  # noqa: BLE001
        record_issue("infra", "建项目失败", str(exc)[:300], severity="high")
        return None


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_state(state: dict) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def read_ledger() -> list[dict]:
    try:
        lines = LEDGER.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def append_ledger(entry: dict) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def record_issue(kind: str, title: str, detail: str, *, severity: str = "medium",
                 level: int = 0, task_id: str = "", evidence: str = "") -> None:
    """只记录，不修。这是本轮的硬约束。"""
    ROOT.mkdir(parents=True, exist_ok=True)
    if not ISSUES.exists():
        ISSUES.write_text(
            "# 夜班问题记录\n\n> 只记录、不修复。明早接管后再逐条处理。\n\n", encoding="utf-8"
        )
    lines = [
        f"## [{now()}] L{level} {title}",
        "",
        f"- 类型：{kind}",
        f"- 严重程度：{severity}",
    ]
    if task_id:
        lines.append(f"- 任务：`{task_id}`")
    lines.append(f"- 现象：{detail}")
    if evidence:
        lines.append(f"- 证据：`{evidence[:400]}`")
    lines.append("")
    with ISSUES.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


# --------------------------------------------------------------------------- #
# 一轮
# --------------------------------------------------------------------------- #

MAX_ATTEMPTS = 2


def next_level() -> dict | None:
    """取下一级。两轮制：

    第一轮把 11 级各做一遍（阶梯）；第二轮只复测**开发级**，用来看"同一级两次做的
    差别有多大"——单次结果说明不了稳定性，这一条比多刷几级有用。
    """
    rows = read_ledger()
    success: dict[int, int] = {}
    attempts: dict[int, int] = {}
    for item in rows:
        level = int(item.get("level") or 0)
        attempts[level] = attempts.get(level, 0) + 1
        if item.get("status") == "succeeded":
            success[level] = success.get(level, 0) + 1

    for spec in LADDER:  # 第一轮：还没成功过的
        level = spec["level"]
        if success.get(level, 0) == 0 and attempts.get(level, 0) < MAX_ATTEMPTS:
            return {**spec, "pass": 1}

    for spec in LADDER:  # 第二轮：只复测开发级，最多两次
        level = spec["level"]
        if spec["kind"] != "dev":
            continue
        if success.get(level, 0) == 1 and attempts.get(level, 0) < MAX_ATTEMPTS + 1:
            return {**spec, "pass": 2}
    return None


def prepare_level(spec: dict) -> None:
    """给需要"别人写的代码"的等级铺素材。审查/重构级必须有料可审。"""
    if spec["level"] == 4:
        legacy = WORKSPACE / "legacy"
        if legacy.exists():
            return
        legacy.mkdir(parents=True, exist_ok=True)
        # 三个各自埋了真缺陷的模块。缺陷是真的：资源泄漏、边界、并发。
        (legacy / "reader.py").write_text(
            'import os\n\n\ndef read_all(paths):\n'
            '    """把多个文件读成列表。"""\n'
            '    handles = []\n'
            '    out = []\n'
            '    for path in paths:\n'
            '        handle = open(path, "r", encoding="utf-8")\n'
            '        handles.append(handle)\n'
            '        out.append(handle.read())\n'
            '    return out\n',
            encoding="utf-8",
        )
        (legacy / "pager.py").write_text(
            'def page(items, page_no, size=10):\n'
            '    """取第 page_no 页（从 1 开始）。"""\n'
            '    start = (page_no - 1) * size\n'
            '    return items[start : start + size]\n\n\n'
            'def total_pages(items, size=10):\n'
            '    return len(items) // size\n',
            encoding="utf-8",
        )
        (legacy / "counter.py").write_text(
            'import threading\n\n\nclass Counter:\n'
            '    def __init__(self):\n'
            '        self.value = 0\n\n'
            '    def add(self, n=1):\n'
            '        current = self.value\n'
            '        current += n\n'
            '        self.value = current\n',
            encoding="utf-8",
        )
    if spec["level"] == 8:
        legacy2 = WORKSPACE / "legacy2"
        if legacy2.exists():
            return
        legacy2.mkdir(parents=True, exist_ok=True)
        body = "import sys\n\n\nRECORDS = []\n\n\n"
        body += "def load(path):\n    rows = []\n"
        body += "    with open(path, 'r', encoding='utf-8') as f:\n"
        body += "        for line in f:\n            parts = line.strip().split(',')\n"
        body += "            if len(parts) >= 3:\n"
        body += "                rows.append({'id': parts[0], 'kind': parts[1], 'amount': float(parts[2])})\n"
        body += "    return rows\n\n\n"
        body += "def summarize(rows):\n    total = 0.0\n    by_kind = {}\n"
        body += "    for r in rows:\n        total += r['amount']\n"
        body += "        by_kind[r['kind']] = by_kind.get(r['kind'], 0.0) + r['amount']\n"
        body += "    return total, by_kind\n\n\n"
        body += "def render(total, by_kind):\n    lines = ['合计: %.2f' % total]\n"
        body += "    for k in sorted(by_kind):\n        lines.append('  %s: %.2f' % (k, by_kind[k]))\n"
        body += "    return '\\n'.join(lines)\n\n\n"
        body += "def main(argv):\n    if len(argv) < 2:\n        print('用法: monolithic.py <csv>')\n        return 2\n"
        body += "    rows = load(argv[1])\n    total, by_kind = summarize(rows)\n"
        body += "    print(render(total, by_kind))\n    return 0\n\n\n"
        body += "if __name__ == '__main__':\n    sys.exit(main(sys.argv))\n"
        (legacy2 / "monolithic.py").write_text(body, encoding="utf-8")
        (legacy2 / "sample.csv").write_text(
            "1,food,12.50\n2,toy,3.00\n3,food,7.25\n", encoding="utf-8"
        )


def drive(task_id: str, limit_seconds: float) -> tuple[str, list[str]]:
    """推到终态。无人值守时自动批审批——这件事本身要记进问题里。"""
    approved: set[str] = set()
    auto_approved: list[str] = []
    deadline = time.time() + limit_seconds
    status = ""
    while time.time() < deadline:
        try:
            task = call("GET", f"/tasks/{task_id}")
        except Exception:  # noqa: BLE001
            time.sleep(5)
            continue
        status = str(task.get("status") or "")
        if status in ("succeeded", "failed", "cancelled", "paused"):
            return status, auto_approved
        if status == "waiting_approval":
            try:
                evidence = call("GET", f"/tasks/{task_id}/evidence")
                pending = evidence.get("pending_approvals") or []
                for item in pending:
                    key = str(item.get("approval_id") or "")
                    if not key or key in approved:
                        continue
                    call(
                        "POST",
                        f"/tasks/{task_id}/approval",
                        {"approve": True, "approval_id": key},
                        timeout=60,
                    )
                    approved.add(key)
                    auto_approved.append(key)
            except Exception:  # noqa: BLE001
                pass
        time.sleep(4)
    return status or "timeout", auto_approved


def score_review(files: list[Path] | None = None) -> dict:
    """审查级按**报告质量**评分：有没有定位到行、有没有分级、有没有后果判断。

    不能拿"有没有 .py"去评一份审查报告——那是两码事（第一版就是这么把 L4 打成了 1/8）。
    """
    docs = [p for p in (files or []) if p.suffix.lower() in (".md", ".txt")]
    text = ""
    for path in docs:
        try:
            text += path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
    refs = len(re.findall(r"\.[a-z]{1,4}:\d+", text))          # 文件:行号
    graded = len(re.findall(r"(高|中|低|严重|critical|major|minor)", text))
    reasons = len(re.findall(r"(因为|原因|后果|导致|会炸|风险)", text))
    parts = {
        "有报告": 2 if docs and len(text) > 200 else 0,
        "定位到行": 2 if refs >= 3 else (1 if refs else 0),
        "有分级": 2 if graded >= 3 else (1 if graded else 0),
        "给了后果": 2 if reasons >= 3 else (1 if reasons else 0),
    }
    return {
        "files": len(docs),
        "lines": len(text.splitlines()),
        "tests": 0,
        "compile_ok": True,
        "smells": [],
        "parts": parts,
        "score": sum(parts.values()),
        "kind": "review",
    }


def score_deliverable(files: list[Path] | None = None, base: Path | None = None) -> dict:
    """静态评分：能编译、有测试、没有明显坏味道。**启发式，不是测量。**

    只评**本轮新增的文件**。扫整个工作区的话，第一轮的产物会被后面每一轮重复计入，
    分数只会单调上升，那就不是"这一级交付的成色"了。
    """
    if files is None:
        files = [p for p in (base or WORKSPACE).rglob("*.py") if ".venv" not in str(p)]
    py_files = [p for p in files if p.suffix == ".py"]
    test_files = [p for p in py_files if "test" in p.name.lower() or "tests" in p.parts]
    lines = 0
    for path in py_files:
        try:
            lines += len(path.read_text(encoding="utf-8", errors="ignore").splitlines())
        except OSError:
            continue
    compile_ok = True
    for path in py_files:
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "py_compile", str(path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            compile_ok = False
            break

    smells: list[str] = []
    for path in py_files:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if re.search(r"\bTODO\b|\bFIXME\b", text):
            smells.append(f"{path.name}: TODO/FIXME")
        if re.search(r"except\s*:", text):
            smells.append(f"{path.name}: 裸 except")
        if re.search(r"print\(\s*['\"](debug|xxx)", text, re.IGNORECASE):
            smells.append(f"{path.name}: 调试输出")

    parts = {
        "交付存在": 2 if py_files else 0,
        "可编译": 2 if compile_ok and py_files else 0,
        "有测试": 2 if test_files else 0,
        "无坏味道": 1 if not smells else 0,
        "分模块": 1 if len([p for p in py_files if "test" not in p.name.lower()]) >= 3 else 0,
    }
    return {
        "files": len(py_files),
        "lines": lines,
        "tests": len(test_files),
        "compile_ok": compile_ok,
        "smells": smells[:5],
        "parts": parts,
        "score": sum(parts.values()),
    }


def score_page(journey: str, page: str) -> dict:
    """页面类交付按**执行式验收**打分，不看"有没有交付文件"。

    教训来自 2026-11-20 的 L6：一个任务看板四条判据全真通过（拖拽真派事件验过），
    但打开是三列空框，用户第一句是"就做了个这个"。原因不是它偷懒，是判据只问
    "功能在不在"，没问"打开时是什么样"——而空页面永远没有控制台错误，天然达标。

    所以这里换成 `scripts/acceptance.py`：打开先断言首屏有没有东西（杀空壳），
    再走一遍填/点/拖/刷新（杀假象）。任何一条不过就不是这一级的交付。
    """
    journey_path = REPO / "acceptance" / journey
    if not journey_path.is_file():
        return {"score": 0, "parts": {"验收脚本存在": 0}, "error": f"缺少 {journey_path}"}
    try:
        spec = json.loads(journey_path.read_text(encoding="utf-8"))
        open_count = len(spec.get("open") or [])
    except (OSError, json.JSONDecodeError):
        open_count = 0
    target = WORKSPACE / page
    proc = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(REPO / "scripts" / "acceptance.py"),
            "--journey",
            str(journey_path),
            "--page",
            str(target),
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    try:
        data = json.loads((proc.stdout or "").strip() or "{}")
    except json.JSONDecodeError:
        return {"score": 0, "parts": {"验收能跑": 0}, "error": (proc.stderr or "")[-300:]}
    steps = data.get("steps") or []
    head, body = steps[:open_count], steps[open_count:]
    errors = data.get("error_count", len(data.get("console") or []))
    head_ok = bool(head) and all(item.get("ok") for item in head)
    parts = {
        "页面能打开": 2 if data.get("title") is not None else 0,
        "首屏有东西": 2 if head_ok else 0,
        "动作走通": 2 if body and all(item.get("ok") for item in body) else 0,
        "零控制台错误": 2 if errors == 0 else 0,
    }
    score = sum(parts.values())
    if not head_ok:
        # 第一屏不成立就不算交付，不是"扣两分"。空壳拿 6/8 会让横向比较失真：
        # 一个打开是空框的页面和一个真能用的页面看起来差不多，那这套分数就白做了。
        score = 0
    return {
        "files": 1 if target.is_file() else 0,
        "lines": 0,
        "tests": 0,
        "compile_ok": True,
        "smells": [],
        "parts": parts,
        "score": score,
        "kind": "page",
        "blocked": not head_ok,
        "failed": [
            item.get("text") or item.get("do")
            for item in steps
            if not item.get("ok")
        ][:6],
    }


def run_round(spec: dict) -> dict:
    project_id = ensure_project()
    if not project_id:
        return {"level": spec["level"], "ok": False, "error": "no project"}

    prepare_level(spec)
    # 用**修改时间**判定本轮产出，而不是文件集合差集：
    # 复测时是覆盖写已有文件，差集为空 → 会被误判成"什么都没交付"。
    # （第一版就是这么把 L5 复测和 L6 打成 1 分的。）
    round_started = time.time()
    entry: dict = {
        "pass": spec.get("pass", 1),
        "at": now(),
        "level": spec["level"],
        "name": spec["name"],
        "kind": spec["kind"],
        "goal": spec["goal"],
    }
    try:
        task = call(
            "POST",
            f"/projects/{project_id}/tasks",
            {
                "goal": spec["goal"],
                "title": f"L{spec['level']} {spec['name']}",
                "scenario": "dev",
                "acceptance_criteria": spec["criteria"],
            },
            timeout=120,
        )
        task_id = str(task["id"])
        entry["task_id"] = task_id
        started = time.time()
        # `/run` 是同步推进的：它会一直跑到第一个暂停点才返回，30 秒经常不够。
        # 超时**不代表没起来**——所以只当当失败处理，继续往下轮询状态。
        try:
            call("POST", f"/tasks/{task_id}/run", {}, timeout=180)
        except Exception:  # noqa: BLE001 - 客户端超时但服务端在跑
            pass
        status, auto_approved = drive(task_id, MINUTES_PER_ROUND * 60)
        entry["status"] = status
        entry["seconds"] = round(time.time() - started, 1)
        entry["auto_approved"] = len(auto_approved)
        if auto_approved:
            record_issue(
                "process",
                "无人值守时审批被自动批准",
                f"这一轮自动批了 {len(auto_approved)} 次；审批交互本身没有被真实验证",
                severity="low",
                level=spec["level"],
                task_id=task_id,
            )

        try:
            task = call("GET", f"/tasks/{task_id}")
            entry["steps"] = task.get("steps_used")
            entry["failure_reason"] = task.get("failure_reason") or ""
        except Exception:  # noqa: BLE001
            pass

        fresh = sorted(
            p for p in WORKSPACE.rglob("*")
            if p.is_file() and p.stat().st_mtime >= round_started
        )
        entry["written"] = [str(p.relative_to(WORKSPACE)) for p in fresh][:15]
        entry.pop("_before", None)
        if spec.get("journey"):
            # 页面类：交付文件的成色由"打开能不能用"回答，不由行数回答。
            entry["score"] = score_page(str(spec["journey"]), str(spec.get("page") or "index.html"))
        elif spec["kind"] == "review":
            entry["score"] = score_review(fresh)
        else:
            entry["score"] = score_deliverable(fresh)

        if status != "succeeded":
            record_issue(
                "task",
                f"任务未成功结束：{status}",
                entry.get("failure_reason") or "没有 failure_reason",
                severity="high" if status == "failed" else "medium",
                level=spec["level"],
                task_id=task_id,
            )
    except Exception as exc:  # noqa: BLE001 - 一轮炸了不能让夜班停
        entry["status"] = "error"
        entry["error"] = f"{exc.__class__.__name__}: {exc}"[:400]
        record_issue("harness", "这一轮跑挂了", entry["error"], severity="high", level=spec["level"])
    append_ledger(entry)
    return entry


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #

def report() -> str:
    rows = read_ledger()
    issues = 0
    try:
        issues = ISSUES.read_text(encoding="utf-8").count("\n## [")
    except FileNotFoundError:
        issues = 0
    if not rows:
        return "还没有任何一轮记录。"
    lines = [
        "# 夜班压测早报",
        "",
        f"生成时间：{now()}　共 {len(rows)} 轮　记录问题 {issues} 条",
        "",
        "| 等级 | 任务 | 类型 | 结果 | 耗时 | 步数 | 交付文件 | 测试 | 静态分 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for item in rows:
        score = item.get("score") or {}
        lines.append(
            "| L{lv} | {name} | {kind} | {status} | {sec}s | {steps} | {files} | {tests} | {score}/8 |".format(
                lv=item.get("level"),
                name=item.get("name"),
                kind="开发" if item.get("kind") == "dev" else "审查",
                status=item.get("status"),
                sec=int(item.get("seconds") or 0),
                steps=item.get("steps") or "-",
                files=score.get("files", "-"),
                tests=score.get("tests", "-"),
                score=score.get("score", "-"),
            )
        )
    lines += ["", "## 说明", "",
              "- 静态分是启发式（能编译/有测试/无坏味道/分模块），**不是代码质量的绝对值**。",
              "- 全程未修改任何产品代码；问题见 `issues.md`，明早接管后再处理。",
              "- 未发送任何通知，结果只在本机。"]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="夜班压测：只测试、只记录")
    parser.add_argument("--next", action="store_true", help="跑下一级")
    parser.add_argument("--status", action="store_true", help="看进度")
    parser.add_argument("--report", action="store_true", help="生成早报")
    parser.add_argument("--loop", action="store_true", help="连着跑，直到预算用完或等级跑完")
    parser.add_argument("--budget-minutes", type=int, default=40, help="这次调用最多跑多久")
    args = parser.parse_args()

    ROOT.mkdir(parents=True, exist_ok=True)
    if args.report:
        text = report()
        (ROOT / "report.md").write_text(text, encoding="utf-8")
        print(text)
        return 0

    if args.status:
        rows = read_ledger()
        print(f"已完成 {len(rows)}/{len(LADDER)} 级")
        for item in rows:
            print(f"  L{item.get('level')} {item.get('name')} · {item.get('status')}")
        nxt = next_level()
        print(f"下一级：L{nxt['level']} {nxt['name']}" if nxt else "全部等级已完成")
        return 0

    if args.loop:
        deadline = time.time() + max(5, args.budget_minutes) * 60
        rounds = 0
        while time.time() < deadline:
            spec = next_level()
            if spec is None:
                print("等级全部跑完，提前收工。")
                break
            if not ensure_backend():
                append_ledger({"at": now(), "level": 0, "name": "后端不可用", "status": "skipped"})
                break
            print(f"[{now()}] 开始 L{spec['level']} {spec['name']} …", flush=True)
            entry = run_round(spec)
            rounds += 1
            print(f"[{now()}] 结束：{entry.get('status')}　静态分 "
                  f"{entry.get('score', {}).get('score')}/8", flush=True)
        text = report()
        (ROOT / "report.md").write_text(text, encoding="utf-8")
        print(f"\n本次跑了 {rounds} 轮，报告已更新到 {ROOT / 'report.md'}")
        return 0

    if not args.next:
        parser.print_help()
        return 0

    if not ensure_backend():
        append_ledger({"at": now(), "level": 0, "name": "后端不可用", "status": "skipped"})
        print("后端不可用，本轮跳过（已记录）。")
        return 1

    spec = next_level()
    if spec is None:
        print("八级全部跑完，转做复测与报告。")
        print(report())
        return 0

    print(f"开始 L{spec['level']} {spec['name']} …")
    entry = run_round(spec)
    print(f"结束：{entry.get('status')}　静态分 {entry.get('score', {}).get('score')}/8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
