"""run_tests: detect the project's test command and execute it inside the workspace."""

from __future__ import annotations

import json
import re
import shutil
import sys

from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome, ok
from blank_api.services.tools.shell import (
    cmd_semicolon_hint,
    execute_command,
    mask_output,
    normalize_python_command,
    policy_gate,
    project_python,
    python_test_command,
    runs_in_docker,
    truncate,
)

SUMMARY = re.compile(r"(\d+)\s+(passed|failed|errors?|skipped|xfailed|xpassed|error)")
PYTEST_MARKERS = ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "conftest.py")

# ── 跑不完的命令要记住 ─────────────────────────────────────────────────────
#
# 真机一天撞了三次同一个形状：模型改了 deliverables/ 下的一个静态页面，然后调
# `run_tests {}`；自动识别到的是**仓库根的 `npm test`**——全量后端单测，约 25 分钟，
# 单次上限 120–180 秒，必然超时。于是它白等 3 分钟、烧掉一轮上下文，再被判失败。
#
# 不去猜"这次该跑哪个"（`src/` 改动 + 根 `tests/` 的结构会让任何猜测都误伤），
# 只记一个确定的事实：**这条命令在这台机器上跑不完**。记住之后，下次无参调用直接
# 跳过并说明；要跑就用 command 显式指定——把决定权留给模型，但别让它每次都重新踩。
HISTORY_FILE = "test-command-history.json"
HISTORY_LIMIT = 20


def _history_path():
    from pathlib import Path

    try:
        from blank_api.services.settings import SettingsStore

        return SettingsStore().path.parent / HISTORY_FILE
    except Exception:  # noqa: BLE001 - 拿不到设置目录就退到 cwd/data，别让记忆功能挡住测试
        return Path("data") / HISTORY_FILE


def load_command_history() -> dict:
    """读"跑不完的命令"记录。文件坏了就当没记过——一份经验不该让测试起不来。"""
    path = _history_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def remember_timeout(command: str, seconds: float) -> None:
    """记下"这条命令跑了多久没跑完"。只记超时，不记失败——失败是有结论的。"""
    text = str(command or "").strip()
    if not text:
        return
    import datetime

    history = load_command_history()
    history[text] = {
        "timeout_seconds": round(float(seconds), 1),
        "at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    # 只留最近若干条：这是经验，不是账本。
    if len(history) > HISTORY_LIMIT:
        ordered = sorted(history.items(), key=lambda kv: str((kv[1] or {}).get("at") or ""))
        history = dict(ordered[-HISTORY_LIMIT:])
    path = _history_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(history, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass  # 记不住不影响这次执行


def skippable_command(command: str) -> dict | None:
    """这条命令是不是"上次就没跑完"的那条。"""
    entry = load_command_history().get(str(command or "").strip())
    return entry if isinstance(entry, dict) else None
CONTAINER_PYTHON = "python3"
GO_MARKERS = ("go.mod", "go.sum")

# pytest 的 exit 5 是「没收集到测试」，不是「测试没通过」。严格评测把隐藏测试移出
# 工作区后，校验通道自动识别到 pytest 配置、跑 `pytest -q` 必然拿 exit 5；旧逻辑
# 记 failed，任务判「测试未通过」，而交付里代码早已改对（2026-11-16 coding-008
# t1 后端任务 1bc0bd26 实测）。两条判据都要成立才豁免：命令是 pytest、输出确实说
# 没收集到测试——别家脚本的 exit 5 照旧算失败。
PYTEST_CMD = re.compile(
    r"(?i)(?:^|[\s;&|(])(?:pytest(?:\.exe)?|python[0-9.]*\s+-m\s+pytest)\b"
)
PYTEST_NO_TESTS = re.compile(r"(?i)no tests ran|collected 0 items|no tests collected")


def is_pytest_no_tests(command: str, output: str) -> bool:
    return bool(
        PYTEST_CMD.search(str(command or "")) and PYTEST_NO_TESTS.search(str(output or ""))
    )

# 「命令根本没启动」的退出码与回执片段（Windows cmd 9009 / POSIX 127）。
# 这跟「测试没通过」是两件事：前者该去补命令/改调用方式，后者才该改代码。
NOT_FOUND_EXIT_CODES = (9009, 127)
NOT_FOUND_OUTPUT = (
    re.compile(r"不是内部或外部命令"),
    re.compile(r"is not recognized as an internal or external command", re.IGNORECASE),
)

# 「跑不了测试」的特征：解释器里没 pytest，或 PATH 上压根没有这个命令。
# 只认这几个稳定片段，不要把任何非零退出都归成环境问题——那样会把真失败洗白。
MISSING_RUNNER = (
    (re.compile(r"No module named ['\"]?pytest['\"]?"), "pytest"),
    (re.compile(r"No module named ['\"]?py\.test['\"]?"), "pytest"),
    (re.compile(r"pytest: (command )?not found"), "pytest"),
    (re.compile(r"'pytest' is not recognized", re.IGNORECASE), "pytest"),
)


def missing_runner(output: str) -> str:
    """测试运行器本身不可用时返回它的名字，否则空串。"""
    for pattern, name in MISSING_RUNNER:
        if pattern.search(output or ""):
            return name
    return ""


def missing_command(output: str, exit_code: int | None = None) -> bool:
    """命令根本没启动（不是测试跑完失败）。"""
    if exit_code in NOT_FOUND_EXIT_CODES:
        return True
    return any(pattern.search(output or "") for pattern in NOT_FOUND_OUTPUT)


# 裸 `pytest` 与裸 `python` 是同一类问题：不传 command 那一路走 `python_test_command`
# （项目 venv → 本进程解释器 → -m pytest），传了 command 的裸 `pytest` 却原样交给系统
# shell。PATH 上没有 pytest 时拿 9009，模型据此认定「环境没有 Python」，整轮去找解释器
# 甚至 winget 装运行时（2026-11-16 coding-026 t1 实测：10 分钟窗口烧在 `where python`
# / `winget install` 上，money.py 其实早已改对）。
BARE_PYTEST = re.compile(r'(?i)^(\s*)["\']?pytest(?:\.exe)?["\']?(?=\s|$)')


def normalize_bare_pytest(command: str, interpreter: str) -> str:
    """把命令首段的裸 `pytest` 换成 `<解释器> -m pytest`，其余参数原样保留。"""
    if not interpreter or not isinstance(command, str):
        return command
    match = BARE_PYTEST.match(command)
    if not match:
        return command
    head = f'"{interpreter}"' if " " in interpreter else interpreter
    return f"{match.group(1)}{head} -m pytest{command[match.end():]}"


# 链式命令的每一段都要解析到同一个解释器：`||` 后面的裸 python 不解析时，会落到
# PATH 上第一个同名程序（本机是商店占位符），9009 污染整条命令的退出码，而前半段
# 测试其实已经跑完。2026-11-16 coding-031 t1（后端任务 7150924d）：链尾裸 python
# 把 run_tests 变成「测试命令不存在」，模型据此烧掉 10 分钟找 Python / 装运行时。
CHAIN_HEADS = re.compile(
    r'(?i)^\s*["\']?(python3?|py|pytest(?:\.exe)?)["\']?(?=\s|$)'
)


def _chain_parts(command: str) -> list[tuple[str, str]]:
    """按 ``&&`` / ``||`` 切分并保留分隔符；引号内的同名片段不切。"""
    parts: list[tuple[str, str]] = []
    buf: list[str] = []
    quote = ""
    i = 0
    while i < len(command):
        ch = command[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if command.startswith("&&", i) or command.startswith("||", i):
            end = i + 2
            while end < len(command) and command[end] in " \t":
                end += 1
            parts.append(("".join(buf), command[i:end]))
            buf = []
            i = end
            continue
        buf.append(ch)
        i += 1
    parts.append(("".join(buf), ""))
    return parts


def normalize_chained_interpreters(command: str, interpreter: str) -> str:
    """整条命令都是裸解释器调用时，把每一段都换成同一个解释器。

    只处理「每一段首词都是裸 python / pytest」的链式命令：出现显式路径、
    ``set PATH``、其它程序就整条原样返回——那是模型写死的意图，替它改会改语义。
    """
    if not interpreter or not isinstance(command, str):
        return command
    parts = _chain_parts(command)
    if len(parts) < 2:
        return command
    if not all(CHAIN_HEADS.match(segment) for segment, _sep in parts):
        return command
    fixed = []
    for segment, sep in parts:
        segment = normalize_python_command(segment, None, fallback=interpreter)
        segment = normalize_bare_pytest(segment, interpreter)
        fixed.append(segment + sep)
    return "".join(fixed)


def detect_test_command(root) -> tuple[list[str], bool] | None:
    """Return (argv, use_shell) for the detected test runner, or None."""
    package_json = root / "package.json"
    if package_json.exists():
        try:
            manifest = json.loads(package_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            manifest = {}
        if (manifest.get("scripts") or {}).get("test"):
            return ["npm", "test"], True
    # Go 工程：go.mod 在即认 go test，不动 docker 的 python 替换（argv[0] 是 go 不是 python）。
    # 放在 pytest 之前，避免 go 工程里偶发的 tests/ 目录被误判成 pytest。
    if (root / "go.mod").exists():
        return ["go", "test", "./...", "-count=1"], False
    for marker in PYTEST_MARKERS:
        if (root / marker).exists():
            return python_test_command(root), False
    if (root / "tests").is_dir():
        return python_test_command(root), False
    if any(root.glob("test_*.py")) or any(root.glob("*_test.py")):
        return python_test_command(root), False
    return None


def summarize_exit_codes(output: str) -> dict:
    counts: dict[str, int] = {}
    for amount, kind in SUMMARY.findall(output):
        counts[kind.rstrip("s")] = counts.get(kind.rstrip("s"), 0) + int(amount)
    return counts


def missing_test_runner(argv: list[str], tier: str = "") -> str:
    """检测到的运行器不在 PATH 上时返回它的名字，否则空串。

    只查裸命令名（`go`、`npm`）：带路径分隔符的（`.venv/Scripts/python`）由
    ``normalize_relative_executable`` 负责，用 which 判只会制造假红。
    容器档不查宿主 PATH——命令在镜像里跑，宿主有没有这个工具与它无关。
    """
    if not argv or runs_in_docker(tier):
        return ""
    name = str(argv[0])
    if not name or "/" in name or "\\" in name:
        return ""
    return "" if shutil.which(name) else name


async def _run_tests(context: ToolContext, arguments: dict) -> ToolOutcome:
    blocked = policy_gate(context, "run_tests", arguments)
    if blocked is not None:
        return blocked
    timeout = float(arguments.get("timeout_seconds", 180))
    tier = getattr(context, "sandbox_tier", "") or ""
    override = arguments.get("command")
    if override:
        # 和不传 command 的那一路同源解析解释器：venv 优先，否则本进程解释器；容器档
        # 用镜像自带的 python3。裸 python 原样交给 cmd 会落到 PATH 上第一个同名程序
        # ——本机是商店占位符，退出码 9009，被记成「测试未通过」，模型只好去下载
        # 便携运行时（coding-012/028/035 真机轨迹）。
        if runs_in_docker(tier):
            argv = normalize_chained_interpreters(str(override), CONTAINER_PYTHON)
            argv = normalize_python_command(argv, None, fallback=CONTAINER_PYTHON)
            argv = normalize_bare_pytest(argv, CONTAINER_PYTHON)
        else:
            interpreter = project_python(context.root) or sys.executable
            # 先整链逐段解析（否则段首被换成显式路径后，整链就不再是「裸解释器链」）；
            # 再兜底段首的单条命令。两条分支都解析到同一个解释器。
            argv = normalize_chained_interpreters(str(override), interpreter)
            argv = normalize_python_command(argv, context.root, fallback=sys.executable)
            argv = normalize_bare_pytest(argv, interpreter)
        shell = True
    else:
        detected = detect_test_command(context.root)
        if detected is None:
            return ToolOutcome(
                ok=False,
                status="error",
                message="未识别到测试命令：工作区中既没有 pytest 配置，也没有 package.json 的 test 脚本",
            )
        argv, shell = detected
        if runs_in_docker(tier) and argv and argv[0] in (sys.executable, project_python(context.root)):
            # 宿主虚拟环境不在容器里，测试要改用镜像自带的解释器
            argv = [CONTAINER_PYTHON, *argv[1:]]
        planned = " ".join(argv)
        remembered = skippable_command(planned)
        if remembered:
            seconds = remembered.get("timeout_seconds")
            when = str(remembered.get("at") or "")
            return ToolOutcome(
                ok=False,
                status="skipped",
                # 跳过不是失败：我们**没有拿到任何结论**，也不该再白等一次。
                # 今天三次超时都是这条：改的是 deliverables/ 下的静态页面，
                # 识别到的却是仓库根的全量后端单测（约 25 分钟 vs 单次 180 秒上限）。
                message=(
                    f"这条测试命令在这台机器上次就没跑完（{seconds}s 上限，记录于 {when}）：{planned}。"
                    "已跳过，不会再白等一次。要跑就二选一："
                    "① 用 command 显式指定一条能在单次调用内跑完的范围（具体测试文件/目录）；"
                    "② 用 run_shell 起后台跑、再用 task_output 取结果。"
                    "如果你改的是页面或脚本，browser_check 往往比全量测试更贴题。"
                ),
                data={
                    "command": planned,
                    "skipped_reason": "此前超时未跑完",
                    "remembered": remembered,
                },
            )
        absent = missing_test_runner(argv, tier)
        if absent:
            # 识别出了测试命令、但本机根本没装这个运行器（典型：Go 项目没有 go）。
            # 判 failed 会让任务去"修"一段本来是对的代码；判 error 会进修复回路，
            # 模型重试同一个不存在的命令直到步数烧完。跳过并说清缺什么才诚实。
            reason = f"本机没有 {absent} 工具链，测试没有执行（不是代码没通过）"
            return ToolOutcome(
                ok=False,
                status="skipped",
                # 回执要带上补齐路径，不是只报"缺"。Codex 的原话：「If installation is not
                # possible in this environment, tell the user which dependency is missing and
                # how to install it into their active environment.」——先按顺序试，试不动
                # 才交回用户，而且交回时要写清缺什么、怎么装。
                message=(
                    reason + "。先补齐再重跑：在磁盘上找现成安装，或用包管理器装"
                    "（winget / choco / scoop）；装好后用 run_tests 重跑（可传 command 覆盖，"
                    "把 PATH 前置写进同一条命令）；run_shell 里跑通不算验证证据。"
                    "确实装不上，再告诉用户缺什么、怎么装。"
                ),
                data={
                    "command": " ".join(argv),
                    "skipped_reason": reason,
                    "missing_test_runner": absent,
                },
            )
        if shell:
            argv = " ".join(argv)
    result = await execute_command(
        argv,
        context.root,
        timeout,
        shell=shell,
        safety=getattr(context, "safety", None),
        tier=tier,
        isolation_profile=str(getattr(context, "isolation_profile", "") or ""),
    )
    timeout = result.get("timeout_budget", timeout)
    stdout = truncate(mask_output(result["stdout"], context.root))[0]
    stderr = truncate(mask_output(result["stderr"], context.root))[0]
    combined = stdout if not stderr else f"{stdout}\n[stderr]\n{stderr}".strip()
    command_text = argv if isinstance(argv, str) else " ".join(argv)
    if result.get("isolation_blocked"):
        return ToolOutcome(
            ok=False,
            status="denied",
            output=combined,
            message=f"任务级隔离不可用，测试未执行：{result.get('stderr') or ''}".strip(),
            data={"tier": result.get("tier", {}), "isolation_blocked": True,
                  "enforcement": result["enforcement"]},
        )
    if result.get("tier_blocked"):
        return ToolOutcome(
            ok=False,
            status="denied",
            output=combined,
            message=f"沙箱档位不可用，测试未执行：{result.get('stderr') or ''}".strip(),
            data={"tier": result.get("tier", {}), "tier_blocked": True,
                  "enforcement": result["enforcement"]},
        )
    if result["timed_out"]:
        # 记住这条命令跑不完：下次无参调用直接跳过，别让每个新任务都重新踩一遍。
        remember_timeout(command_text, timeout)
        return ToolOutcome(
            ok=False,
            status="timeout",
            output=combined,
            # 超时要和"没通过"分开说：这条命令**没跑完**，拿不到任何结论。
            # 真机轨迹：工作区里一个静态页面目录，自动识别到的却是仓库根的 `npm test`
            # （全量后端单测，约 25 分钟），180 秒上限必然超时——模型读到的却是
            # "测试未通过"，于是回头去"修"一段没问题的代码。
            message=(
                f"测试命令超时（{timeout:.0f}s），进程已终止，**没有拿到任何测试结论**：{command_text}。"
                "这不等于测试失败。要么换一条能在单次调用内跑完的范围"
                "（用 command 指定具体测试文件/目录），要么用 run_shell 起后台跑、"
                "再用 task_output 取结果。"
                + (f"已捕获的最后输出：{combined[-400:]}" if combined else "（超时前没有任何输出，通常说明它在等输入或起了长驻进程）")
            ),
            data={"command": command_text, "timed_out": True, "timeout_seconds": timeout,
                  "sandbox": result["sandbox"],
                  "tier": result.get("tier", {}), "enforcement": result["enforcement"]},
        )
    passed = result["exit_code"] == 0
    if result["exit_code"] == 5 and is_pytest_no_tests(command_text, combined):
        # 没收集到测试 ≠ 测试没通过：工作区里根本没有可跑的测试文件（严格评测会把
        # 隐藏测试移出工作区、判分时才装回）。判 failed 会把任务引去「修」不存在的
        # bug，也会让代码已经改对的交付被判失败。
        reason = "pytest 没收集到测试（exit 5），这不是测试未通过"
        return ToolOutcome(
            ok=False,
            status="skipped",
            output=combined,
            message=(
                reason + "。工作区里没有可收集的测试文件（评测场景下测试会被移出、"
                "判分时装回）；不要据此判定代码有错，是否补测试按任务要求决定。"
            ),
            data={
                "command": command_text,
                "exit_code": 5,
                "passed": False,
                "skipped_reason": reason,
                "no_tests_collected": True,
                "sandbox": result["sandbox"],
                "tier": result.get("tier", {}),
                "enforcement": result["enforcement"],
                "summary": summarize_exit_codes(combined),
            },
        )
    if not passed:
        swallowed = cmd_semicolon_hint(command_text, combined)
        if swallowed:
            # cmd.exe 把 `;` 吞进参数后，输出与「pytest 真没装」一模一样（都是
            # `No module named pytest;`）。先按分号解释，否则模型会去装包/找解释器
            # （2026-11-16 coding-012 t1 真机：10 分钟窗口全烧在这上面）。
            return ToolOutcome(
                ok=False,
                status="error",
                output=combined,
                message=f"测试未能执行：{swallowed}",
                data={
                    "command": command_text,
                    "exit_code": result["exit_code"],
                    "passed": False,
                    "cmd_semicolon": True,
                    "sandbox": result["sandbox"],
                    "tier": result.get("tier", {}),
                    "enforcement": result["enforcement"],
                    "summary": summarize_exit_codes(combined),
                },
            )
        missing = missing_runner(combined)
        if missing:
            # 「测试挂了」和「这台机器上跑不了测试」是两件事。混成一句"测试未通过"，
            # 用户会去改代码，而真正该做的是把测试运行器装上。真实台账里的证据：
            # 同一条命令四次退出码 1，输出是 `No module named pytest`，交付却写成
            # "测试未通过"，任务被判失败——代码其实早就改对了。
            return ToolOutcome(
                ok=False,
                status="error",
                output=combined,
                message=(
                    f"测试未能执行：环境里没有 {missing}。"
                    f"请先安装（例如 `python -m pip install {missing}`）或改用项目虚拟环境里的解释器"
                ),
                data={
                    "command": command_text,
                    "exit_code": result["exit_code"],
                    "passed": False,
                    "missing_test_runner": missing,
                    "sandbox": result["sandbox"],
                    "tier": result.get("tier", {}),
                    "enforcement": result["enforcement"],
                    "summary": summarize_exit_codes(combined),
                },
            )
        if missing_command(combined, result["exit_code"]):
            # 9009 / 127：命令没启动。真机里这被写成「测试未通过，退出码 9009」，
            # 模型据此认定环境没有解释器，下载便携 Python 进工作区、污染交付。
            # 链式命令的退出码可能来自后面的某一段（例如 `||` 后面的裸 python 落到
            # 商店占位符），前半段测试其实已经跑过——这时诬告「系统找不到段首」会把
            # 模型引向满盘找解释器（2026-11-16 coding-031 t1 实测烧掉整个窗口）。
            chained = len(_chain_parts(command_text)) > 1
            ran = bool(SUMMARY.search(combined) or is_pytest_no_tests(command_text, combined))
            if chained and ran:
                return ToolOutcome(
                    ok=False,
                    status="error",
                    output=combined,
                    message=(
                        "链式命令没有完整执行：前半段测试已经跑过（输出见上），但后面的某一段"
                        f"没能启动（整条命令退出码 {result['exit_code']}，常见原因是链尾的裸 "
                        "`python` 落到了 PATH 上的占位符）。这不是环境缺 Python，不要用 "
                        "where / reg / winget 去找解释器；把整条命令都写成裸 python/pytest "
                        "的分段（run_tests 会统一解析成同一个解释器），或者不传 command "
                        "让 run_tests 自动识别。"
                    ),
                    data={
                        "command": command_text,
                        "exit_code": result["exit_code"],
                        "passed": False,
                        "command_not_found": True,
                        "chain_tail_failed": True,
                        "sandbox": result["sandbox"],
                        "tier": result.get("tier", {}),
                        "enforcement": result["enforcement"],
                        "summary": summarize_exit_codes(combined),
                    },
                )
            head = re.match(r'\s*["\']?([^\s"\']+)', command_text)
            name = head.group(1) if head else command_text
            return ToolOutcome(
                ok=False,
                status="error",
                output=combined,
                message=(
                    f"测试命令不存在：系统找不到 `{name}`（退出码 {result['exit_code']}），"
                    "测试没有启动。这不是测试未通过。改用 run_tests 默认识别（不传 command）"
                    "或显式写解释器的绝对路径后重跑；不要为了跑测试在工作区里下载运行时。"
                ),
                data={
                    "command": command_text,
                    "exit_code": result["exit_code"],
                    "passed": False,
                    "command_not_found": True,
                    "sandbox": result["sandbox"],
                    "tier": result.get("tier", {}),
                    "enforcement": result["enforcement"],
                    "summary": summarize_exit_codes(combined),
                },
            )
    return ToolOutcome(
        ok=passed,
        status="ok" if passed else "failed",
        output=combined,
        message="" if passed else f"测试未通过，退出码 {result['exit_code']}",
        data={
            "command": command_text,
            "exit_code": result["exit_code"],
            "passed": passed,
            "sandbox": result["sandbox"],
            "tier": result.get("tier", {}),
            # 测试跑不动的原因之一可能是限额把子进程拦了；没有这一行，用户只能看到"退出码非零"。
            "enforcement": result["enforcement"],
            "summary": summarize_exit_codes(combined),
        },
    )


def test_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="run_tests",
            description=(
                "运行项目测试。不传 command 时自动识别：存在 go.mod 时执行 go test ./...；"
                "存在 pytest.ini/pyproject.toml/tests 目录时执行 pytest；"
                "存在 package.json test 脚本时执行 npm test"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "自定义测试命令，留空则自动识别"},
                    "timeout_seconds": {"type": "number", "description": "超时秒数，默认 180"},
                },
            },
            risk_level="medium",
            handler=_run_tests,
            timeout_seconds=200.0,
        )
    ]
