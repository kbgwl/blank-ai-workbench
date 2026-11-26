# -*- coding: utf-8 -*-
"""确定性前端冒烟：模型不肯跑 browser_check 时，验证节点自己开一枪。

B7/B8 真机反复出现同一个形状：页面其实能打开，模型却拿到 browser_check 提示
后仍然不调用；只靠预算保留和提示词，交付会被“没自检”卡死。

这里只做**低风险兜底**，不替代模型的动作验收：

* 只认工作区里的 ``package.json`` → ``scripts.start`` 且启动命令是
  ``node <文件>`` 的项目（B7 的 ``node server.js`` 就是这种）；
* 用随机回环端口起服务，等待 HTTP 就绪；
* 用现有 ``browser_probe.mjs`` 打开首页，断言 ``body`` 存在、页面正文非空且
  不含 ``undefined``，再 reload 一次；
* 控制台/请求错误为 0 才算通过；跑完立即杀掉服务。

它不执行 ``npm``，不跑任意脚本，也不修改工作区；只给“页面能不能打开”一个
确定性回执。真正的交互验收仍应由模型用 browser_check 完成。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import tempfile
from pathlib import Path
from urllib.parse import quote

from blank_api.services.tools.browser import PROBE, node_exe


def _start_script(root: Path) -> Path | None:
    """把 package.json 的 start 解析成工作区内的 node 脚本。

    只接受 ``node <file>`` / ``node --flags <file>``；npm scripts 可能是任意
    shell，这里不执行。文件必须存在且落在 root 内。
    """
    package = root / "package.json"
    try:
        data = json.loads(package.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    scripts = data.get("scripts") if isinstance(data, dict) else None
    start = scripts.get("start") if isinstance(scripts, dict) else None
    if not isinstance(start, str):
        return None
    match = re.match(r"^\s*node(?:\.exe)?\s+(.+?)\s*$", start, re.IGNORECASE)
    if not match:
        return None
    words = [word.strip("\"'") for word in match.group(1).split()]
    target = next((word for word in words if not word.startswith("-")), "")
    if not target:
        return None
    try:
        script = (root / target).resolve()
        script.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return script if script.is_file() else None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _http_ready(port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
            sock.sendall(b"GET / HTTP/1.0\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
            return bool(sock.recv(1))
    except OSError:
        return False


async def _wait_http(port: int, deadline: float) -> bool:
    loop = asyncio.get_running_loop()
    while loop.time() < deadline:
        if await asyncio.to_thread(_http_ready, port):
            return True
        await asyncio.sleep(0.25)
    return False


def _stop(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        proc.kill()
    except ProcessLookupError:
        pass


async def auto_ui_check(root: Path, *, startup_timeout: float = 12.0) -> dict:
    """尝试启动工作区服务并做一次通用页面冒烟。

    返回 ``{"status": "passed"|"failed"|"unavailable", ...}``。任何环境缺口
    （没有 node、没有可识别的 start、端口起不来）都返回 unavailable，不把
    环境问题伪装成页面失败。
    """
    root = Path(root)
    script = _start_script(root)
    if script is None:
        return {"status": "unavailable", "reason": "没有可识别的 node start 脚本"}
    node = node_exe()
    if not node or not PROBE.is_file():
        return {"status": "unavailable", "reason": "没有 node 或 browser_probe.mjs"}

    port = _free_port()
    env = {key: value for key, value in os.environ.items() if key.upper().startswith(("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "USERPROFILE", "COMSPEC", "PATHEXT"))}
    env.update({"PORT": str(port), "HOST": "127.0.0.1", "NODE_ENV": "test"})
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            node,
            str(script),
            cwd=str(root),
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        loop = asyncio.get_running_loop()
        if not await _wait_http(port, loop.time() + startup_timeout):
            return {"status": "unavailable", "reason": "服务在超时内没有监听回环端口"}
        url = f"http://127.0.0.1:{port}/"
        with tempfile.TemporaryDirectory(prefix="blank-ui-smoke-") as tmp:
            steps = Path(tmp) / "steps.json"
            steps.write_text(
                json.dumps(
                    [
                        {"do": "reload"},
                        {
                            "do": "assert",
                            "expr": "document.body && document.body.innerText.trim().length > 100 && !document.body.innerText.includes('undefined')",
                        },
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            shots = root / ".blank/shots"
            shots.mkdir(parents=True, exist_ok=True)
            argv = [
                node,
                str(PROBE),
                "--url",
                url,
                "--wait",
                "1000",
                "--timeout",
                "20000",
                "--expect",
                "body:1",
                "--steps",
                str(steps),
                "--shot",
                str(shots / "ui-smoke.png"),
                "--shot-open",
                str(shots / "ui-smoke-open.png"),
            ]
            probe = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(probe.communicate(), timeout=40)
        lines = (stdout or b"").decode("utf-8", "replace").strip().splitlines()
        if not lines:
            return {"status": "failed", "reason": f"浏览器探针没有输出：{(stderr or b'').decode('utf-8','replace')[-300:]}"}
        data = json.loads(lines[-1])
        errors = [item for item in data.get("console") or [] if item.get("level") == "error"]
        failed_asserts = [item for item in data.get("asserts") or [] if not item.get("ok")]
        failed_steps = [item for item in data.get("steps") or [] if not item.get("ok")]
        ok = bool(data.get("ok")) and not errors and not failed_asserts and not failed_steps
        if ok:
            return {
                "status": "passed",
                "url": url,
                "auto": True,
                "asserts": len(data.get("asserts") or []),
                "steps": len(data.get("steps") or []),
                "console": len(data.get("console") or []),
            }
        first = (
            (errors[0].get("text") if errors else "")
            or (failed_asserts[0].get("selector") if failed_asserts else "")
            or (failed_steps[0].get("actual") if failed_steps else "")
            or "页面冒烟未通过"
        )
        return {"status": "failed", "url": url, "reason": str(first)[:500], "data": data}
    except (OSError, json.JSONDecodeError, asyncio.TimeoutError) as exc:
        return {"status": "unavailable", "reason": f"自动冒烟不可用：{exc}"}
    finally:
        if proc is not None:
            _stop(proc)
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except (asyncio.TimeoutError, ProcessLookupError):
                pass
