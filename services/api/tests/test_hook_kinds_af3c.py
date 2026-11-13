"""AF3c：上游五种钩子类型的补全（command 之外的 prompt / agent / http / mcp_tool）。

对标 Claude Code 的钩子类型契约，逐条取证自上游实物（`claude.uniq.txt`）：
* 序列化形状：``{type:"http",command:url}``、``{type:"mcp_tool",command:"${server}/${tool}"}``、
  ``{type:"prompt",prompt}``、``{type:"agent",prompt}``；
* http 型走 ``dfe``：POST JSON，非 2xx 的报错文案逐字是 ``HTTP ${statusCode} from ${url}``；
  成功时上游把 ``statusCode`` 记成 exitCode、``body`` 当 stdout，而成败看的是 ``outcome``
  （2xx 即 success）——**退出码不等于成败**，不能拿"非 0 即失败"反推；
* prompt/agent 型在没有会话上下文时直接抛
  ``prompt-type hooks are not supported for ${event} events (no conversation context
  is available). Use a command-type hook instead.``（agent 型同句，前缀换 agent-type）；
* 四类非 command 类型的输出判定（``parse_hook_output``）与退出码语义与 command 共用。

判据分三层，每条都能失败：
* 配置识别：五种类型各自认出来、字段名对齐上游、认不出来的类型告警跳过（而不是当命令跑）；
* 执行器：http 真起本地服务收请求、mcp_tool/prompt/agent 注入 runner 后正文按同一套解析；
* 共用语义：2xx 判成功且退出码记状态码（反证"非 0 即失败"）、非 2xx 文案逐字、
  阻断事件的 body 决策照常生效。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from blank_api.services.hooks import HOOK_KINDS, HookRunner, HookSpec
from tests.test_agent_v04 import env  # noqa: F401  （asyncio_mode=auto，沿用装置约定）

UPSTREAM_PROMPT_ERROR = (
    "prompt-type hooks are not supported for {event} events "
    "(no conversation context is available). Use a command-type hook instead."
)
UPSTREAM_AGENT_ERROR = (
    "agent-type hooks are not supported for {event} events "
    "(no conversation context is available). Use a command-type hook instead."
)


# --------------------------------------------------------------------------- #
# 层 1：配置识别
# --------------------------------------------------------------------------- #
def _specs(monkeypatch, payload: dict) -> list[HookSpec]:
    monkeypatch.setenv("BLANK_HOOKS", json.dumps(payload))
    runner = HookRunner.from_env()
    return runner.hooks.get("pre_tool_use", [])


def test_六种类型都认出来且字段名对齐上游(monkeypatch) -> None:
    specs = _specs(
        monkeypatch,
        {
            "pre_tool_use": [
                {"type": "command", "command": "echo hi"},
                {"type": "http", "url": "http://127.0.0.1:9/hook"},
                {"type": "prompt", "prompt": "检查这步"},
                {"type": "agent", "prompt": "派个审核", "agent_type": "reviewer"},
                {"type": "mcp_tool", "server": "srv", "tool": "check"},
                {"type": "script", "file": "hooks/guard.py"},
            ]
        },
    )
    assert [s.kind for s in specs] == ["command", "http", "prompt", "agent", "mcp_tool", "script"]
    # 上游的序列化形状：http 写进 command 键的是 url，mcp_tool 是 `${server}/${tool}`，
    # script 是文件路径（内联正文不落审计，用固定标记代替）。
    by_kind = {s.kind: s for s in specs}
    assert by_kind["http"].command == "http://127.0.0.1:9/hook"
    assert by_kind["http"].url == "http://127.0.0.1:9/hook"
    assert by_kind["mcp_tool"].command == "srv/check"
    assert (by_kind["mcp_tool"].server, by_kind["mcp_tool"].tool) == ("srv", "check")
    assert by_kind["prompt"].prompt == "检查这步"
    assert by_kind["agent"].prompt == "派个审核"
    assert by_kind["script"].command == "hooks/guard.py"
    assert HOOK_KINDS == frozenset(
        {"command", "prompt", "agent", "http", "mcp_tool", "script"}
    )


def test_http型缺url与mcp_tool缺工具都告警跳过(monkeypatch) -> None:
    runner = HookRunner.from_env(
        raw=json.dumps(
            {
                "pre_tool_use": [
                    {"type": "http"},
                    {"type": "mcp_tool", "server": "srv"},
                    {"type": "mcp_tool", "tool": "check"},
                    {"type": "prompt"},
                ]
            }
        )
    )
    assert not runner.hooks.get("pre_tool_use"), "缺关键字段的钩子不该被加载"
    joined = "\n".join(runner.warnings)
    assert "http" in joined and "url" in joined
    assert "mcp_tool" in joined
    assert "prompt" in joined


def test_认不出来的类型告警跳过而不是当命令跑(monkeypatch) -> None:
    runner = HookRunner.from_env(
        raw=json.dumps({"pre_tool_use": [{"type": "bogus", "command": "rm -rf /"}]})
    )
    assert not runner.hooks.get("pre_tool_use"), "未知类型绝不能当成 shell 命令执行"
    assert any("bogus" in w and "尚未支持" in w for w in runner.warnings)


def test_script型缺file与内联正文都认得(monkeypatch) -> None:
    specs = _specs(
        monkeypatch,
        {
            "pre_tool_use": [
                {"type": "script", "file": "hooks/guard.py"},
                {"type": "script", "script": "print('x')"},
            ]
        },
    )
    assert [s.command for s in specs] == ["hooks/guard.py", "<inline-script>"]
    # 内联正文不落进审计的 command 字段，免得把脚本原文带进事件。
    assert "print" not in specs[1].command


def test_script型缺file与正文时告警跳过(monkeypatch) -> None:
    runner = HookRunner.from_env(raw=json.dumps({"pre_tool_use": [{"type": "script"}]}))
    assert not runner.hooks.get("pre_tool_use")
    assert any("script" in w and "file/script" in w for w in runner.warnings)


def test_http型也认command键的回退写法(monkeypatch) -> None:
    """上游序列化时把 url 写进 command 键；直接给 command 也要认，别把 url 当 shell 跑。"""
    specs = _specs(monkeypatch, {"pre_tool_use": [{"type": "http", "command": "http://x/y"}]})
    assert specs[0].kind == "http"
    assert specs[0].url == "http://x/y"


# --------------------------------------------------------------------------- #
# 层 2：执行器（http 真起本地服务）
# --------------------------------------------------------------------------- #
class _Recorder(BaseHTTPRequestHandler):
    captured: list[dict] = []
    status: int = 200
    body: bytes = b"{}"

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的命名
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b""
        type(self).captured.append(
            {
                "method": self.command,
                "path": self.path,
                "content_type": self.headers.get("content-type"),
                "body": raw,
            }
        )
        self.send_response(type(self).status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(type(self).body)))
        self.end_headers()
        self.wfile.write(type(self).body)

    def log_message(self, *args) -> None:  # 静音，别把访问日志混进判据输出
        return


class _Server:
    def __init__(self) -> None:
        _Recorder.captured = []
        _Recorder.status = 200
        _Recorder.body = b"{}"
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}/hook"

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


async def test_http型真POST且2xx判成功退出码记状态码() -> None:
    server = _Server()
    try:
        _Recorder.status = 200
        _Recorder.body = json.dumps(
            {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "补一段"}}
        ).encode("utf-8")
        runner = HookRunner(
            {"pre_tool_use": [HookSpec(kind="http", command=server.url, url=server.url)]}
        )
        results = await runner.run("pre_tool_use", "run_shell", {"tool_name": "run_shell"})
        assert len(_Recorder.captured) == 1, "钩子必须真的发出一次 POST"
        sent = _Recorder.captured[0]
        assert sent["method"] == "POST"
        assert sent["content_type"] == "application/json"
        assert json.loads(sent["body"].decode("utf-8"))["tool_name"] == "run_shell"
        result = results[0]
        # 反证"非 0 即失败"：2xx 的退出码是状态码，但成败必须是 ok。
        assert result.exit_code == 200
        assert result.status == "ok", "2xx 被误判成失败——退出码不等于成败"
        assert result.additional_context == "补一段"
    finally:
        server.stop()


async def test_http型非2xx的报错文案逐字对齐上游() -> None:
    server = _Server()
    try:
        _Recorder.status = 500
        _Recorder.body = b"boom"
        runner = HookRunner(
            {"pre_tool_use": [HookSpec(kind="http", command=server.url, url=server.url)]}
        )
        results = await runner.run("pre_tool_use", "run_shell", {})
        result = results[0]
        assert result.status == "failed"
        assert result.reason == f"HTTP 500 from {server.url}"
        assert result.exit_code == 500
    finally:
        server.stop()


async def test_http型连不上时是非阻断失败() -> None:
    runner = HookRunner(
        {"post_tool_use": [HookSpec(kind="http", command="http://127.0.0.1:1/nope", url="http://127.0.0.1:1/nope")]}
    )
    results = await runner.run("post_tool_use", "", {})
    assert results[0].status == "failed"
    assert results[0].exit_code is None


# --------------------------------------------------------------------------- #
# 层 3：mcp_tool / prompt / agent
# --------------------------------------------------------------------------- #
async def test_mcp_tool型注入runner后正文按同一套解析() -> None:
    seen: dict = {}

    async def mcp_runner(server: str, tool: str, payload: dict) -> str:
        seen.update(server=server, tool=tool, payload=payload)
        return json.dumps({"hookSpecificOutput": {"additionalContext": "mcp 补的"}})

    runner = HookRunner(
        {"post_tool_use": [HookSpec(kind="mcp_tool", command="srv/check", server="srv", tool="check")]},
        mcp_runner=mcp_runner,
    )
    results = await runner.run("post_tool_use", "", {"tool_name": "x"})
    assert seen == {"server": "srv", "tool": "check", "payload": {"tool_name": "x", "event": "post_tool_use", "hook_event_name": "post_tool_use", "target": ""}}
    assert results[0].status == "ok"
    assert results[0].additional_context == "mcp 补的"


async def test_mcp_tool型没有客户端时报错而不是当命令跑() -> None:
    runner = HookRunner(
        {"post_tool_use": [HookSpec(kind="mcp_tool", command="srv/check", server="srv", tool="check")]}
    )
    results = await runner.run("post_tool_use", "", {})
    assert results[0].status == "failed"
    assert "MCP 客户端" in results[0].reason
    assert results[0].exit_code is None


async def test_prompt型没有会话上下文时逐字报上游那句() -> None:
    runner = HookRunner({"pre_tool_use": [HookSpec(kind="prompt", command="检查", prompt="检查")]})
    results = await runner.run("pre_tool_use", "", {})
    assert results[0].status == "failed"
    assert results[0].reason == UPSTREAM_PROMPT_ERROR.format(event="pre_tool_use")


async def test_agent型没有会话上下文时前缀换成agent_type() -> None:
    runner = HookRunner({"pre_tool_use": [HookSpec(kind="agent", command="审核", prompt="审核")]})
    results = await runner.run("pre_tool_use", "", {})
    assert results[0].reason == UPSTREAM_AGENT_ERROR.format(event="pre_tool_use")


async def test_prompt型注入runner后正文按同一套解析() -> None:
    seen: dict = {}

    async def prompt_runner(prompt: str, payload: dict) -> str:
        seen.update(prompt=prompt, payload=payload)
        return json.dumps({"decision": "deny", "reason": "模型说不行"})

    runner = HookRunner(
        {"pre_tool_use": [HookSpec(kind="prompt", command="检查", prompt="检查这步")]},
        prompt_runner=prompt_runner,
    )
    results = await runner.run("pre_tool_use", "run_shell", {"tool_name": "run_shell"})
    assert seen["prompt"] == "检查这步"
    assert results[0].status == "ok"
    assert results[0].decision == "deny"
    assert results[0].reason == "模型说不行"


async def test_script型执行时报上游那句不可用() -> None:
    """上游这个构建里 ``Jst`` 直接抛 "script hooks are not available in this build"。"""
    runner = HookRunner(
        {"post_tool_use": [HookSpec(kind="script", command="hooks/guard.py")]}
    )
    results = await runner.run("post_tool_use", "", {})
    assert results[0].status == "failed"
    assert results[0].reason == "script hooks are not available in this build"
    assert results[0].exit_code is None


# --------------------------------------------------------------------------- #
# 层 4：共用判定语义
# --------------------------------------------------------------------------- #
async def test_http型在阻断事件上body决策照常生效() -> None:
    server = _Server()
    try:
        _Recorder.status = 200
        _Recorder.body = json.dumps({"decision": "deny", "reason": "http 拦下"}).encode("utf-8")
        runner = HookRunner(
            {"pre_tool_use": [HookSpec(kind="http", command=server.url, url=server.url)]}
        )
        results = await runner.run("pre_tool_use", "run_shell", {})
        assert results[0].decision == "deny"
        assert results[0].reason == "http 拦下"
        assert results[0].status == "ok", "决策来自 body，成败仍看 2xx"
    finally:
        server.stop()


async def test_prompt型runner抛错时是失败而不是静默放行() -> None:
    async def prompt_runner(prompt: str, payload: dict) -> str:
        raise RuntimeError("模型炸了")

    runner = HookRunner(
        {"pre_tool_use": [HookSpec(kind="prompt", command="检查", prompt="检查")]},
        prompt_runner=prompt_runner,
    )
    results = await runner.run("pre_tool_use", "", {})
    assert results[0].status == "failed"
    assert "RuntimeError" in results[0].reason


def test_from_env把两个runner透传进实例(monkeypatch) -> None:
    async def prompt_runner(prompt: str, payload: dict) -> str:
        return ""

    async def mcp_runner(server: str, tool: str, payload: dict) -> str:
        return ""

    monkeypatch.setenv("BLANK_HOOKS", json.dumps({"pre_tool_use": [{"type": "prompt", "prompt": "p"}]}))
    runner = HookRunner.from_env(prompt_runner=prompt_runner, mcp_runner=mcp_runner)
    assert runner.prompt_runner is prompt_runner
    assert runner.mcp_runner is mcp_runner
