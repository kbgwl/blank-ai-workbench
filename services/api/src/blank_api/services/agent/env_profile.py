# -*- coding: utf-8 -*-
"""开局环境预检：一次性问清这台机器有什么，别再让模型逐条探测。

真实台账（2026-11-23「给我写一个项目计划书」）：模型为了找一个能装包的 Python，
连着跑了 `where python`、`py -3`、`python -m ensurepip`、`where uv`……十条命令，
其中九条失败。这些失败全是**信息缺失**造成的，不是任务本身有多难。

所以在任务开局跑一次很轻的探测（每个进程 6 秒封顶，冷启动 import 也够），把结果拼成一小段
「本机环境」放进系统提示。模型看到的就是事实，不需要再猜、也不需要再试。

设计约束：

* 只报**版本与存在性**，不报主机绝对路径——这段会进模型上下文与事件账；
* 探测自身失败一律降级成「未找到」，绝不让任务起不来；
* 同一工作区 + 同一 PATH 的探测结果缓存 5 分钟，避免每个任务都 fork 六个进程。
"""
from __future__ import annotations

import asyncio
import os
import platform
import shutil
import subprocess
import sys
import time
from typing import Any

# 冷启动 import lxml/Pillow（python-pptx 的依赖）在 Windows 上可能超过 3 秒：
# 第一次真正跑探针时实测 6.7 秒，三条解释器探测里两条被判超时、文档库被误报"未安装"。
PROBE_TIMEOUT = 6.0
CACHE_TTL_SECONDS = 300.0
MAX_OUTPUT_CHARS = 4000

# 探针脚本：版本 + pip + 常用文档库（没有就输出空行，不抛异常到退出码之外）。
# 文档库单独探一次，是为了让模型知道"要写 .docx 到底要不要装包"——这一步以前
# 是靠 python -c "import docx" 一条条试出来的。
_PY_CODE = (
    "import sys\n"
    "print(sys.version.split()[0])\n"
    "try:\n"
    "    import pip\n"
    "    print(pip.__version__)\n"
    "except Exception:\n"
    "    print('')\n"
    "libs = []\n"
    "for name in ('docx', 'openpyxl', 'pptx'):\n"
    "    try:\n"
    "        __import__(name)\n"
    "        libs.append(name)\n"
    "    except Exception:\n"
    "        pass\n"
    "print(','.join(libs))\n"
)

_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}


def _run(argv: list[str], *, cwd: str, env: dict[str, str]) -> tuple[int, str]:
    """跑一条探测命令；任何异常都算「没探测到」。"""
    try:
        completed = subprocess.run(  # noqa: S603 - 命令是常量，参数不来自模型
            argv,
            cwd=cwd,
            env=env,
            capture_output=True,
            timeout=PROBE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return 127, ""
    raw = completed.stdout or b""
    if len(raw) > MAX_OUTPUT_CHARS:
        raw = raw[:MAX_OUTPUT_CHARS]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        for encoding in ("gbk", "mbcs"):
            try:
                text = raw.decode(encoding)
                break
            except (UnicodeDecodeError, LookupError):
                continue
        else:
            text = raw.decode("utf-8", errors="replace")
    return completed.returncode, text.strip()


def _which(name: str, env: dict[str, str]) -> str:
    return shutil.which(name, path=env.get("PATH", "") or "") or ""


def _python(exe: str, env: dict[str, str], cwd: str, prefix: list[str] | None = None) -> dict[str, str]:
    """解释器的版本与 pip 版本。空 dict 表示这个入口不可用。"""
    if not exe and not prefix:
        return {}
    argv = ([exe] if exe else []) + [*(prefix or []), "-c", _PY_CODE]
    code, output = _run(argv, cwd=cwd, env=env)
    if code != 0:
        return {}
    lines = [line.strip() for line in output.splitlines()]
    version = lines[0] if lines else ""
    pip_version = lines[1] if len(lines) > 1 else ""
    libs = [item for item in (lines[2].split(",") if len(lines) > 2 else []) if item]
    if not version:
        return {}
    return {"version": version, "pip": pip_version, "libs": libs}


def _version(argv: list[str], env: dict[str, str], cwd: str) -> str:
    code, output = _run(argv, cwd=cwd, env=env)
    if code != 0:
        return ""
    first = output.splitlines()[0].strip() if output else ""
    # `git version 2.47.1.windows.1` / `v20.11.1` → 只留版本号可用的一段
    token = first.split()[-1] if first else ""
    return token.lstrip("v")[:40]


def _collect(root: str, env: dict[str, str]) -> dict[str, Any]:
    info: dict[str, Any] = {
        "os": f"{platform.system()} {platform.release()}".strip(),
        "shell": "cmd.exe" if os.name == "nt" else (os.environ.get("SHELL") or "sh"),
    }
    python_exe = _which("python", env)
    info["python"] = _python(python_exe, env, root)
    py_exe = _which("py", env)
    info["py3"] = _python(py_exe, env, root, prefix=["-3"]) if py_exe else {}
    # 应用自己的解释器：run_shell 会把裸 python 改写到它（见 shell.normalize_python_command），
    # 所以它的文档库可用性就是"任务里 import docx 到底行不行"的直接答案。
    info["app"] = _python(sys.executable, env, root)
    # 工作区自己的 venv：用 shell_env 的同一套探测，避免两处口径分叉。
    try:
        from blank_api.services.shell_env import detect_venv

        venv_path = detect_venv(root)
    except Exception:  # noqa: BLE001 - 探测失败按没有 venv 处理
        venv_path = ""
    info["venv"] = _python(venv_path, env, root) if venv_path else {}
    info["node"] = _version([_which("node", env), "--version"], env, root) if _which("node", env) else ""
    info["git"] = _version([_which("git", env), "--version"], env, root) if _which("git", env) else ""
    info["uv"] = _version([_which("uv", env), "--version"], env, root) if _which("uv", env) else ""
    return info


def profile(root: str, env: dict[str, str] | None = None, *, now: float | None = None) -> dict[str, Any]:
    """探测工作区所在机器的可用能力；结果按 (root, PATH) 缓存 5 分钟。"""
    resolved = str(root or "")
    path = (env or {}).get("PATH", "")
    key = (resolved, path)
    stamp = time.monotonic() if now is None else now
    cached = _cache.get(key)
    if cached is not None and stamp - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]
    info = _collect(resolved, dict(env or {}))
    _cache[key] = (stamp, info)
    if len(_cache) > 64:
        # 简单上限：只清最旧的一批，不值得为缓存引入依赖。
        for old_key, _value in sorted(_cache.items(), key=lambda item: item[1][0])[:16]:
            _cache.pop(old_key, None)
    return info


async def profile_async(root: str, env: dict[str, str] | None = None) -> dict[str, Any]:
    """在线程里探测：六条子进程不该卡住事件循环。"""
    return await asyncio.to_thread(profile, root, env)


def _python_line(label: str, row: dict[str, str]) -> str:
    if not row:
        return f"- {label}: 未找到"
    pip = row.get("pip") or ""
    suffix = f"（pip {pip} 可用）" if pip else "（没有 pip，不能用它装包）"
    return f"- {label}: {row.get('version') or '版本未知'}{suffix}"


_DOC_LIB_LABELS = {"docx": "python-docx", "openpyxl": "openpyxl", "pptx": "python-pptx"}


def _doc_libs_line(info: dict[str, Any]) -> str:
    found: list[str] = []
    for label, row in (
        ("python", info.get("python")),
        ("py -3", info.get("py3")),
        ("工作区 .venv", info.get("venv")),
        ("应用解释器", info.get("app")),
    ):
        libs = (row or {}).get("libs") or []
        if libs:
            found.append(f"{label}: {'、'.join(_DOC_LIB_LABELS.get(item, item) for item in libs)}")
    if found:
        return "- 文档库已装: " + "；".join(found)
    return (
        "- 文档库: 未安装；要写 Word/Excel/PPT 时用标了「pip 可用」的解释器装一次"
        "（首次需审批），否则用 Markdown/CSV 交付"
    )


def render_block(info: dict[str, Any]) -> str:
    """把探测结果压成一小段给模型看的文字；只讲事实与下一步，不讲原理。"""
    if not info:
        return ""
    lines = [f"- 系统: {info.get('os') or '未知'}；shell: {info.get('shell') or '未知'}"]
    lines.append(_python_line("python", info.get("python") or {}))
    if info.get("py3"):
        lines.append(_python_line("py -3", info["py3"]))
    if info.get("venv"):
        lines.append(_python_line("工作区 .venv", info["venv"]))
    else:
        lines.append("- 工作区 .venv: 没有")
    if info.get("app"):
        lines.append(_python_line("应用解释器", info["app"]))
    lines.append(f"- node: {info.get('node') or '未安装'}")
    lines.append(f"- git: {info.get('git') or '未安装'}")
    lines.append(f"- uv: {info.get('uv') or '未安装'}")
    lines.append(_doc_libs_line(info))
    has_pip = bool((info.get("python") or {}).get("pip") or (info.get("py3") or {}).get("pip") or (info.get("venv") or {}).get("pip"))
    if has_pip:
        lines.append("- 需要装包时，用上面标了「pip 可用」的解释器；装包失败两次就停，改用标准库或说明缺口。")
    else:
        lines.append("- 所有解释器都没有 pip：不要尝试装包，用标准库或无依赖方案完成任务，把缺口写进交付。")
    lines.append("- 以上是开局实测：不要再用 where / --version 逐条探测解释器。")
    return "\n".join(lines)
