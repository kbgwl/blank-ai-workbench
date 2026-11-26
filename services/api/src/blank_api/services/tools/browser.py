"""browser_check：让模型自己"看一眼"页面。

这个工具存在的理由是一条真机轨迹：模型写完一个 3D 页面，**没有办法看见它**，
只能起 electron 去试，GPU 进程连崩五次，最后在交付里写「浏览器里从未跑通，
需要你换 Chrome 打开确认」。而那个页面其实是好的——用这个工具三秒钟就能看到。

它做五件事，一件不多：打开、收控制台错误、按选择器断言、按步骤做一遍动作、
截两张图（首屏 / 末态）。截图会作为图片附在工具结果里（和 read_file 读到图片
同一条通道），模型于是不是"听说页面能跑"，而是真的看见了。

动作序列（``steps``）是后加的一件事，起因很具体：一个任务看板交了四条判据全过的
成品——拖拽真派事件验过，localStorage 真写三条刷过——但交付文件里没有数据，
用户打开只有三个空框。**空页面永远不会有控制台错误**，所以"无报错"这种判据
在空壳身上是最容易满足的。步骤让验收问的是"打开时有什么、动一下变不变"。

边界（写在代码里，不靠模型自觉）：

* 只允许 ``file://``（且必须落在工作区内）与 ``localhost`` / ``127.0.0.1``。
  外网地址一律拒绝——模型驱动的浏览器 = 一个带着你登录态上网的东西，
  "只是看一眼"是最容易被说服的一句话。
* 截图只能写进工作区的 ``.blank/shots/``，不接受模型指定任意路径。
* 浏览器进程用完就杀，临时 profile 在系统临时目录里，退出时删除。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome, fail, ok

PROBE = Path(__file__).resolve().parents[4] / "scripts" / "browser_probe.mjs"
SHOT_DIR = ".blank/shots"
SHOT_KEEP = 20
# 外网一律拒绝；这三个是本机回环的合法写法。
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})
MAX_ATTACH_BYTES = 1_800_000


def find_browser_hint() -> str:
    """本机有没有可用的 Chromium（只用于提前给出可读的错误与测试跳过）。"""
    explicit = os.environ.get("BLANK_CHROME") or ""
    candidates = [
        explicit,
        "C:/Program Files/Google/Chrome/Application/chrome.exe",
        "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe"),
        "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
    ]
    return next((path for path in candidates if path and Path(path).is_file()), "")


def node_exe() -> str:
    """找 node：环境变量 → PATH → 共享工具链（E:\\env）。"""
    explicit = os.environ.get("BLANK_NODE") or ""
    if explicit and Path(explicit).exists():
        return explicit
    found = shutil.which("node")
    if found:
        return found
    for candidate in (
        "E:/env/node/node-v24.20.0-win-x64/node.exe",
        "/usr/bin/node",
        "/usr/local/bin/node",
    ):
        if Path(candidate).exists():
            return candidate
    return ""


def resolve_target(root: Path, url: str) -> tuple[str, str]:
    """把工具参数变成 (可打开的 URL, 错误说明)。错误非空时第一个返回空串。

    规则只有两条，但都要写死：路径必须落在工作区内（否则等于让浏览器读你磁盘上
    任何东西），网络地址只能是本机回环。
    """
    value = str(url or "").strip()
    if not value:
        return "", "缺少 url"
    if value.startswith(("http://", "https://")):
        from urllib.parse import urlparse

        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        if host not in LOCAL_HOSTS:
            return (
                "",
                f"只能打开本机地址（localhost / 127.0.0.1），拒绝访问 {host or value}。"
                "要看外网页面请让用户自己打开，或先用 web_fetch 取正文。",
            )
        return value, ""
    if value.startswith("file://"):
        # file:// 也必须落在工作区内。只放行前缀等于 "" 或 localhost 的本机文件：
        # 放过 file:///C:/Users/... 等于把浏览器变成任意本地文件的读取器。
        from urllib.parse import unquote, urlparse

        parsed = urlparse(value)
        if (parsed.netloc or "").lower() not in ("", "localhost"):
            return "", f"file:// 只能打开本机文件，拒绝 {parsed.netloc}"
        raw = unquote(parsed.path or "")
        # Windows 的路径在 URL 里长这样：/C:/work/index.html
        if os.name == "nt" and len(raw) >= 3 and raw[0] == "/" and raw[2] == ":":
            raw = raw[1:]
        try:
            target = Path(raw).resolve()
            root_path = Path(root).resolve()
        except OSError as exc:
            return "", f"文件路径无法解析：{exc}"
        if target != root_path and root_path not in target.parents:
            return "", "file:// 只能打开工作区内的文件"
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            return "", f"页面不存在：{value}"
        return "file:///" + quote(target.as_posix(), safe="/:"), ""
    # 当作工作区相对路径：允许目录形式（自动补 index.html）。
    from blank_api.services.workspace import resolve_inside

    try:
        target = resolve_inside(root, value)
    except Exception as exc:  # 越界、非法路径由 paths 层抛 ApiError
        return "", f"路径不在工作区内：{exc}"
    if target.is_dir():
        target = target / "index.html"
    if not target.is_file():
        return "", f"页面不存在：{value}"
    return "file:///" + quote(target.as_posix(), safe="/:"), ""


def _prune_shots(directory: Path, keep: int = SHOT_KEEP) -> None:
    shots = sorted(directory.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in shots[keep:]:
        for path in (stale, stale.with_suffix(".png.jpg")):
            try:
                path.unlink()
            except OSError:
                pass


async def _run_probe(argv: list[str], timeout: float) -> tuple[dict | None, str]:
    """跑探针，解析最后一行 JSON。返回 (结果, 错误说明)。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return None, "没有找到 node，无法运行浏览器探针"
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return None, f"浏览器探针超时（{timeout:.0f}s），页面可能卡在加载中"
    text = (stdout or b"").decode("utf-8", "replace").strip().splitlines()
    if not text:
        detail = (stderr or b"").decode("utf-8", "replace")[-400:]
        return None, f"浏览器探针没有输出：{detail or '未知原因'}"
    try:
        return json.loads(text[-1]), ""
    except json.JSONDecodeError:
        return None, f"浏览器探针输出无法解析：{text[-1][:300]}"


def _summarize(result: dict, shot_rel: str) -> str:
    lines = [f"已打开：{result.get('url') or ''}"]
    title = str(result.get("title") or "").strip()
    if title:
        lines.append(f"标题：{title}")
    errors = [item for item in result.get("console") or [] if item.get("level") == "error"]
    warns = [item for item in result.get("console") or [] if item.get("level") == "warning"]
    lines.append(
        f"控制台：{len(errors)} 条错误、{len(warns)} 条警告"
        if errors or warns
        else "控制台：没有任何错误或警告"
    )
    for item in errors[:5]:
        lines.append(f"  · {str(item.get('text'))[:200]}")
    failed = result.get("errors") or []
    if failed:
        lines.append(f"加载失败：{len(failed)} 个")
        for item in failed[:5]:
            lines.append(f"  · {item.get('url')}（{item.get('error')}）")
    asserts = result.get("asserts") or []
    if asserts:
        lines.append("断言：")
        for item in asserts:
            mark = "通过" if item.get("ok") else "不通过"
            lines.append(
                f"  · {item.get('selector')} 期望至少 {item.get('min')} 个，实际 {item.get('count')} 个 → {mark}"
            )
    steps = result.get("steps") or []
    if steps:
        lines.append("动作：")
        for item in steps:
            mark = "通过" if item.get("ok") else "不通过"
            label = item.get("text") or item.get("do") or ""
            got = item.get("actual")
            tail = "" if got in (None, True) else f"，实际 {str(got)[:80]}"
            lines.append(f"  · {label} → {mark}{tail}")
    if shot_rel:
        lines.append(f"截图：{shot_rel}（这张图已经附给你，直接看）")
    lines.append(
        "结论：页面能打开，动作走得通，控制台无错"
        if result.get("ok")
        else "结论：页面有问题，先看上面这几条再改"
    )
    return "\n".join(lines)


async def _browser_check(context: ToolContext, arguments: dict) -> ToolOutcome:
    url, problem = resolve_target(Path(context.root), str(arguments.get("url") or ""))
    if problem:
        return fail("error", problem)

    node = node_exe()
    if not node:
        return fail("error", "没有找到 node 运行时，浏览器检查不可用（设置 BLANK_NODE 指向 node.exe）")
    if not PROBE.is_file():
        return fail("error", f"探针脚本缺失：{PROBE}")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    shot_dir = Path(context.root) / SHOT_DIR
    shot_dir.mkdir(parents=True, exist_ok=True)
    shot_path = shot_dir / f"{stamp}.png"
    # 动作跑完之后页面已经不是用户打开时的那一屏。两张都留：
    # 首屏证明"有没有东西"，末态证明"动作之后变成什么"。
    shot_open_path = shot_dir / f"{stamp}-open.png"

    viewport = str(arguments.get("viewport") or "1280x800")
    expect = [str(item) for item in (arguments.get("expect") or []) if str(item).strip()]
    argv = [
        node,
        str(PROBE),
        "--url",
        url,
        "--wait",
        str(int(arguments.get("wait_ms") or 800)),
        "--timeout",
        str(int(arguments.get("timeout_ms") or 20000)),
        "--settle",
        str(int(arguments.get("settle_ms") or 600)),
        "--viewport",
        viewport,
        "--shot",
        str(shot_path),
        "--shot-open",
        str(shot_open_path),
    ]
    for item in expect:
        argv.extend(["--expect", item])
    evaluate = str(arguments.get("eval") or "").strip()
    if evaluate:
        argv.extend(["--eval", evaluate])
    # 动作序列：填、点、拖、刷新。页面"长得对"不等于"用起来对"——
    # 一个打开是空白的看板可以没有一条控制台错误。要走一遍才算验过。
    steps = [item for item in (arguments.get("steps") or []) if isinstance(item, dict)]
    with tempfile.TemporaryDirectory(prefix="blank-steps-") as tmp:
        if steps:
            step_file = Path(tmp) / "steps.json"
            step_file.write_text(json.dumps(steps, ensure_ascii=False), encoding="utf-8")
            argv.extend(["--steps", str(step_file)])
        timeout = float(arguments.get("timeout_ms") or 20000) / 1000 + 15
        result, problem = await _run_probe(argv, timeout)
    if problem or result is None:
        return fail("error", problem or "浏览器探针失败")
    if result.get("fatal"):
        return fail("error", f"浏览器没能启动：{result['fatal']}")

    jpeg = shot_path.with_suffix(".png.jpg")
    shot_rel = ""
    image = None
    # 附给模型看的是**首屏**：它的验收标准里现在第一条就是"打开时有什么"，
    # 给末态图等于让它在自己造出来的现场里自证。
    pick = shot_open_path if shot_open_path.is_file() else shot_path
    if pick.is_file():
        shot_rel = pick.relative_to(Path(context.root)).as_posix()
        size = jpeg.stat().st_size if (pick is shot_path and jpeg.is_file()) else pick.stat().st_size
        if size <= MAX_ATTACH_BYTES:
            source = jpeg if (pick is shot_path and jpeg.is_file()) else pick
            media = "image/jpeg" if source is jpeg else "image/png"
            image = {
                "media_type": media,
                "data": base64.b64encode(source.read_bytes()).decode("ascii"),
                "path": shot_rel,
                "bytes": size,
            }
    _prune_shots(shot_dir)

    ok_page = bool(result.get("ok"))
    outcome = ok(
        _summarize(result, shot_rel),
        url=result.get("url") or url,
        title=result.get("title") or "",
        console=[item for item in result.get("console") or []][:20],
        failed_requests=[item for item in result.get("errors") or []][:10],
        asserts=result.get("asserts") or [],
        steps=result.get("steps") or [],
        screenshot=shot_rel,
        screenshot_end=(
            shot_path.relative_to(Path(context.root)).as_posix() if shot_path.is_file() else ""
        ),
        text=str(result.get("text") or "")[:2000],
        duration_ms=result.get("duration_ms"),
    )
    if image is not None:
        outcome.data["image"] = image
    if not ok_page:
        # 页面有问题不是"工具坏了"：ok=False 让模型进入修复回路，
        # status 保持 "failed" 而不是 error——它确实跑完了，只是结论是坏的。
        outcome.ok = False
        outcome.status = "failed"
        outcome.message = "页面打开了，但控制台报错或断言/动作不通过；先按上面的清单改，改完再检查一次"
    return outcome


def browser_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="browser_check",
            description=(
                "无头打开页面，回控制台错误、选择器断言、动作结果、截图（首屏+末态）。"
                "写完前端必用它自检。expect 写选择器可带数量，如 canvas:1；"
                "只放打开时元素，点击后元素放 steps 的 assert。"
                "steps 传动作数组，项形如 {do,selector,expr,min,value,from,to}；"
                "只断言元素存在不算验过——交付前至少验：打开有什么、点一下变没变、刷新还在不在。"
            ),
            input_schema={
                "type": "object",
                "required": ["url"],
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "工作区相对路径或 localhost 地址",
                    },
                    "expect": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "选择器，可写成 canvas:1",
                    },
                    "wait_ms": {"type": "integer", "description": "默认 800"},
                    "settle_ms": {
                        "type": "integer",
                        "description": "click/type/press/drag 后自动等待，默认 600；长请求再加 wait",
                    },
                    "viewport": {"type": "string", "description": "如 1280x800"},
                    "eval": {"type": "string", "description": "页面里求值的 JS"},
                    "steps": {
                        "type": "array",
                        "description": "动作项 {do,selector,expr,min,value,from,to}；do 取 assert/click/type/drag 等",
                        "items": {"type": "object"},
                    },
                    "timeout_ms": {"type": "integer", "description": "默认 20000"},
                },
            },
            risk_level="low",
            handler=_browser_check,
            timeout_seconds=90.0,
        ),
    ]
