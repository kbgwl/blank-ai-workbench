"""把应用真跑在多种数据库方言上（真 HTTP 端到端，同一驱动逐格对照）。

这不是 schema 检查（`tests/test_schema_agreement_v29.py` 量的是建库/迁移/两条建库路径对账），
问的是下一层：**应用自己发出的那些 SQL 在别的方言上跑不跑得动**。所以判据只认真实接口：
同一段驱动、同样的步数（见 `EXPECTED_STEPS`，改步骤必须同时改这句话）、同样的断言，
先在 SQLite 上跑一遍（对照），再到 MySQL / PostgreSQL 上跑（被测）。

为什么必须逐格同跑：某一步在 MySQL 上红了，可能不是方言缺陷，而是我这段 payload 写错。
两格一起红 = 装置错；只有被测格红 = 真分歧。与"新旧同跑"是同一条纪律，只是换了维度。

用法：
    uv run --extra mysql python evals/dialect_e2e.py                     # 没有 MySQL 时明确跳过
    set BLANK_E2E_MYSQL_URL=mysql+aiomysql://root@127.0.0.1:3307/kbgai_e2e
    uv run --extra mysql python evals/dialect_e2e.py --require-mysql     # CI：没库就是失败，不是跳过
    set BLANK_E2E_POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/kbgai_e2e
    python evals/dialect_e2e.py --only sqlite,postgres --require-postgres

环境纪律：
- 只连操作者给定的那台 MySQL，库名照 URL 里的走；绝不碰别人的实例；
- 实例的工作目录、settings、主密钥、上传目录全在临时目录下，跑完即可丢弃；
- 模型用一个本机 OpenAI 兼容桩（同进程线程），不打真供应商、不花钱；
- 服务一律**带令牌起**（`BLANK_API_TOKEN`），装置每个请求都带头：以前整条端到端只跑过
  "没配令牌 = 本机单用户"那一形态，带令牌那一路一次 HTTP 都没被穿过（#53 步骤 69-71）；
  同时把 `BLANK_MODEL_TIMEOUT_SECONDS` 调成 6 秒，好让"模型太慢"在一格里跑得完（步骤 74）；
- 模型服务不可用时那一格用三种确定形状点名（空流 / 明说 400 / 429 / 睡过超时），
  而不是等桩自己偶尔抖一下才被偶然踩到（步骤 62、72-74）；
- 主路径只读（list_files/read_file）+ `default` 审批模式；探针那几步会让桩改抛
  `apply_patch`（合成密钥 ⇒ 高风险 ⇒ 必挂审批）与 `request_user_input`（问题卡），
  写只写进临时工作区里的 `probe_approved.txt`，凭据是合成的、库里的 diff 必须已脱敏。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

API_DIR = Path(__file__).resolve().parents[1] / "services" / "api"
PY = Path(sys.executable)
SCRATCH = Path(os.environ.get("BLANK_E2E_SCRATCH") or Path(tempfile.gettempdir()) / "kbgai-dialect-e2e")
STUB_PORT = int(os.environ.get("BLANK_E2E_STUB_PORT", "8072"))
API_PORT = int(os.environ.get("BLANK_E2E_API_PORT", "8071"))
WS = SCRATCH / "ws"  # 给任务当工作区，只有两个小文件
STUB_LOG_BASE = Path(os.environ.get("BLANK_E2E_STUB_LOG") or SCRATCH / "stub.log")
STUB_LOG = STUB_LOG_BASE  # prepare() 每一格把它指到 stub-<engine>.log：上一批红了才发现轨迹被下一格删掉

# 装置必须自己钉住「这台机器此刻靠什么隔离」这个前提，而不是继承跑它的人的环境变量。
# 2026-10-26 实测：本机没有 docker，`boot()` 又没设这一项，实例就按裸 API 的产品默认
# （沙箱开、不许退回宿主）装配，`resolve_tier(strict=True)` 判 available=False，
# 于是 85 步里 26 步全红、任务一律停在 draft——红的全是装置前提，不是换库问题。
# 默认取与桌面壳同款的姿态（`desktop/main.cjs`：`BLANK_SANDBOX_ENABLED ?? "false"`）。
# 要专门验收紧那一格用装置自己的旋钮，别拿环境变量当漏勺：BLANK_E2E_SANDBOX_ENABLED=true
DEVICE_SANDBOX_ENABLED = os.environ.get("BLANK_E2E_SANDBOX_ENABLED", "false")

# 装置自己声明跑了多少步：`Drive.finish` 拿它对账，`tests/test_dialect_device_v32.py` 拿它当断言。
# 加一步就改这里，别让"全绿"这句话悄悄变成"跑过的那几步全绿"。
EXPECTED_STEPS = 85

# 上传用的最小合法 PNG 头（签名 + IHDR 前缀）；写出来的字节和读回来的字节必须是同一份。
PNG_B64 = "iVBORw0KGgoAAAANSUhEUg=="
PNG_BYTES = base64.b64decode(PNG_B64)


# --------------------------------------------------------------------------- #
# OpenAI 兼容桩：第一次要工具，看到工具结果后收尾
# --------------------------------------------------------------------------- #

_READ = {"name": "list_files", "arguments": json.dumps({"path": "."})}
# 压缩探针（#58）：两轮文件读取才能把第一轮挤出尾部预算——切点落在工具配对
# 前面才算真折叠；只读一次时 `_safe_cut` 会把切点退回段头，一条都不折。
_COMPACT_READS = (
    {"name": "read_file", "arguments": json.dumps({"path": "compact-a.txt"})},
    {"name": "read_file", "arguments": json.dumps({"path": "compact-b.txt"})},
)
_COMPACT_SUMMARY_JSON = json.dumps(
    {
        "goal": "读两份资料并一句话结论",
        "constraints": ["只读工作区"],
        "done": ["读取了 compact-a.txt", "读取了 compact-b.txt"],
        "failures": [],
        "open": ["给出结论"],
    },
    ensure_ascii=False,
)


def _is_summary_request(messages: list[dict]) -> bool:
    """摘要请求走的是同一个端点，只能靠系统提示认出它。

    认错的代价很实：主线那几轮若被当成摘要，桩会回一段五要素 JSON，
    任务看起来成功、工具却一条都没跑。
    """
    if not messages:
        return False
    first = messages[0] or {}
    return first.get("role") == "system" and "你在压缩一段 Agent 的执行历史" in str(
        first.get("content") or ""
    )


def _envelope(call: dict) -> dict:
    """把一个函数调用装进 OpenAI 的 `tool_calls` 信封。

    流式与非流式两条分支必须共用这一个构造函数：#52 第一批实测到，只给 `{"name",
    "arguments"}` 的裸形状在流式那一路没事，一旦撞上流式退路（桩的 SSE 偶发 ReadError）
    走非流式整包，产品读的是 `call["function"]["arguments"]`，裸形状读出空参数
    ——于是探针变成"缺少必填参数：patch"的 error 调用，风险退成 medium、按 risk:auto
    放行，卡一张都不挂。红的全是装置自己，与被测产品无关。
    """
    return {"id": "call_1", "type": "function", "function": call}

# 探针标记：任务 goal 会原样进模型消息，桩据此在这一轮换抛哪个工具调用。
# 选这种"产品提示词里绝不会出现"的字串——认错一次，整格就都在演审批。
PROBE_APPROVAL = "PROBE-APPROVAL-42"
PROBE_QUESTION = "PROBE-QUESTION-42"
# 流式退路探针：这一格的桩只回 SSE 心跳注释行，一帧 `data:` 都不给——"网关不认 stream"
# 的确定形状。产品该做的是记下退路、改用整包调用把活干完，而不是把空流当成"模型没说话"。
# 过去这条路只有桩自己抖的时候才被偶然踩到，红了分不清是谁；现在它是被点名跑的一步。
PROBE_STREAM_FLAKE = "PROBE-STREAMFLAKE-42"
# 退路的另两种形状（#53）：前一种该被退路救回来，后两种该红得清清楚楚。
# PROBE_STREAM_400：网关根本不接受 stream=true，流式那一路直接 400；整包调用是好的。
# 这与"流式返回空"是两回事——空流靠解析不出来才发现，400 是供应商明说的。
PROBE_STREAM_400 = "PROBE-STREAM400-42"
# PROBE_RATE_LIMIT：两种形状都 429。退路救不了它，产品必须把这一轮判成失败，
# 而不是"模型什么都没说"地静默收尾（静默空回复在界面上长得像成功）。
PROBE_RATE_LIMIT = "PROBE-RATELIMIT-42"
# PROBE_RATE_LIMIT_RETRY（#56）：桩只对第一次请求回 429，之后正常应答。守的是
# "限流说的是『现在别问』，退避后再问就该把活干完"——只断言"有一次重试事件"不够，
# 终局必须成功、且一次退路都没走（走了退路说明它把 429 当成问法问题重放了）。
PROBE_RATE_LIMIT_RETRY = "PROBE-RATELIMITRETRY-42"
# PROBE_RATE_LIMIT_AFTER（#57）：桩第一次回 429 并带 `Retry-After: 1`，之后正常应答。
# 守的是"供应商说等 1 秒就等 1 秒"——#56 的 0.5s 盲猜在这里会红在事件上（waited=500）。
PROBE_RATE_LIMIT_AFTER = "PROBE-RATELIMITAFTER-42"
# PROBE_COMPACT（#58）：两轮读大文件把窗口顶过水位，逼出一次真折叠；摘要那一笔
# 第一次回 429 + `Retry-After: 1`，把 #57 只在离线证明过的压缩退避搬到两种方言上。
PROBE_COMPACT = "PROBE-COMPACT-42"
# PROBE_CODE_SEQ（#54）：同一个任务里连抛两条内联解释器命令，正文不同。
# 第一条批准时选「本会话总是允许 python -c print(1) https://a.example」（#87 起规则带上整段尾巴），
# 第二条必须重新挂卡——
# 记忆的粒度若退回"裸 python"，第二条会被静默放行，这一格就红。
# 两条都带 https:// 是为了让 run_shell 的风险判成 high（走审批门）；正文本身只打印。
PROBE_CODE_SEQ = "PROBE-CODE-SEQ-42"
_CODE_FIRST = {"name": "run_shell", "arguments": json.dumps(
    {"command": 'python -c "print(1)" https://a.example'})}
_CODE_SECOND = {"name": "run_shell", "arguments": json.dumps(
    {"command": 'python -c "print(2)" https://b.example'})}
# PROBE_TOOL_GRANT（#82）：与 #54 同形状（同一任务连抛两条内联命令），但正文各不相同。
# 不复用 print(1)/print(2) 是因为 #68 会把「本会话总是允许 python -c print(1)」落盘成
# 跨任务的持久规则：同一实例里后跑的 84 步，第一张卡会被那条规则静默放行，
# 判据读到的就成了别人的账（实测红在"批准后没重新挂卡、status=succeeded"）。
PROBE_TOOL_GRANT = "PROBE-TOOLGRANT-82"
_GRANT_FIRST = {"name": "run_shell", "arguments": json.dumps(
    {"command": 'python -c "print(82)" https://grant-a.example'})}
_GRANT_SECOND = {"name": "run_shell", "arguments": json.dumps(
    {"command": 'python -c "print(182)" https://grant-b.example'})}
_GRANT_COMMANDS = ('python -c "print(82)" https://grant-a.example',
                   'python -c "print(182)" https://grant-b.example')
# PROBE_TAIL_SWAP / PROBE_TAIL_REPLAY（#87）：两条命令的**内联正文完全相同**，只有载荷后面
# 那段尾巴（`sys.argv`）不一样。旧口径把规则截成 `python -c print(87)`，第二段因此**搭了第一段
# 的顺风车被免问**——这正是"记的比执行的窄、匹配却比记的宽"那一格。用 87 是为了与 82/1/2 的
# 持久规则（#68 落盘跨任务）互不覆盖。
# REPLAY 那一支只抛同一条命令一次，钉的是反方向：批准过的那条命令下次不许再挂卡
# （只收紧匹配、不放宽规则会得到"批了没用"）。
PROBE_TAIL_SWAP = "PROBE-TAILSWAP-87"
PROBE_TAIL_REPLAY = "PROBE-TAILREPLAY-87"
_TAIL_FIRST = {"name": "run_shell", "arguments": json.dumps(
    {"command": 'python -c "print(87)" https://tail-a.example'})}
_TAIL_SECOND = {"name": "run_shell", "arguments": json.dumps(
    {"command": 'python -c "print(87)" https://tail-b.example'})}
_TAIL_COMMANDS = ('python -c "print(87)" https://tail-a.example',
                  'python -c "print(87)" https://tail-b.example')
# 卡片与账本上那一串：`render_rule` 只给带空白或 `;&|<>` 的 token 加引号，这两段都不带，
# 所以规则串是不加引号的四段——这条期望本身就是 #87 的判据（加错引号会渲染不回原串）。
_TAIL_RULE_FIRST = "python -c print(87) https://tail-a.example"
_TAIL_RULE_SECOND = "python -c print(87) https://tail-b.example"
# PROBE_SLOW：桩睡到超过模型超时。守的是两层——设置项真的接到适配器（超时不生效时
# 这一格永远绿），以及超时同样只退一次、退路也超时时要失败而不是无限等。
PROBE_SLOW = "PROBE-SLOWREPLY-42"
# 装置自带令牌：以前整条端到端只跑过"没配令牌"那一形态，带令牌那一路一次 HTTP 都没被穿过。
DEVICE_TOKEN = os.environ.get("BLANK_E2E_API_TOKEN", "dialect-e2e-token")
# 模型超时调小，好让"慢"这件事在一格里跑得完；桩正常应答都在几十毫秒内，6 秒是百倍余量。
DEVICE_MODEL_TIMEOUT = "6"
PROBE_PATH = "probe_approved.txt"
# 补丁里带一条合成密钥串（不是任何真凭据）：`apply_patch` 的 risk_for 会因此判成 high，
# 于是 `default` 模式下必然走审批门——这是让 /approval 那个写接口能被跑到的最短路径。
_PROBE_PATCH = (
    f"--- a/{PROBE_PATH}\n"
    f"+++ b/{PROBE_PATH}\n"
    "@@ -0,0 +1,2 @@\n"
    "+# 审批探针写出的文件（内容是合成的，别当凭据）\n"
    '+api_key = "SyntheticNotARealKey0001"\n'
)
_APPROVAL_CALL = {"name": "apply_patch", "arguments": json.dumps(
    {"path": PROBE_PATH, "patch": _PROBE_PATCH}, ensure_ascii=False)}
# 问题卡走 request_user_input：与审批同一条中断通道，只有 kind 和恢复值不同。
_QUESTION_CALL = {"name": "request_user_input", "arguments": json.dumps(
    {"question": "这份资料要放在工作区的哪个目录下？", "header": "落盘位置",
     "options": [{"label": "docs/", "description": "文档目录"}, {"label": "notes/", "description": "笔记目录"}]},
    ensure_ascii=False)}


def _next_call(haystack: str) -> dict:
    """这一轮抛哪个工具调用——桩唯一需要"看内容办事"的地方。"""
    if PROBE_QUESTION in haystack:
        return _QUESTION_CALL
    if PROBE_APPROVAL in haystack:
        return _APPROVAL_CALL
    return _READ



class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    served = 0
    broken = 0
    connections = 0
    # 按探针记命中的请求数：有的探针只该拒第一次（#56 限流退避），"拒几次"本身就是被测契约。
    # 命中数与拒绝数分开记：任务后续的每次模型调用都还带着探针串（goal 在历史里），
    # 把命中数当成拒绝数读，自检会把一条正常收尾的链读成"拒了 4 次"。
    probe_hits: dict[str, int] = {}
    probe_rejects: dict[str, int] = {}
    # 摘要请求不带 goal，认不出它属于哪一格；主线的 PROBE_COMPACT 先到，才允许拒第一笔摘要。
    compact_probe_armed = False
    # 压缩后旧工具结果会被折进摘要，按「历史里见得几条」计数会退回 1 并无限续读
    # （实测空转 200 轮）。这里按主线调用次数记，单调递进。
    compact_calls = 0

    def log_message(self, *args) -> None:  # 静音
        pass

    def _trace(self, note: str) -> None:
        # 装置自述：每一次模型调用都留一行。上一次"只有某一步红了"之所以只能归成"装置在抖"，
        # 就是因为桩只报了个总数，读不出"这一格到底有没有收到那个请求"。
        try:
            with STUB_LOG.open("a", encoding="utf-8") as fh:
                fh.write(f"{time.time():.3f} #{_Handler.served} {note}\n")
        except OSError:
            pass

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._respond()
        except (BrokenPipeError, ConnectionResetError) as error:
            # 装置自检：客户端提前断开算装置事件，必须计数并打出来，
            # 否则下一次"只有某一步红了"又会分不清是产品还是脚手架。
            _Handler.broken += 1
            print(f"[桩] 连接被截断：{type(error).__name__}: {error}")
            self._trace(f"截断 {type(error).__name__}")
        except Exception as error:  # noqa: BLE001 — 桩自己不记账就会静默消失（响应头已发出时尤其阴）
            _Handler.broken += 1
            self._trace(f"异常 {type(error).__name__}: {error}")
            raise

    def _respond(self) -> None:
        data = self._body()
        _Handler.served += 1
        messages = data.get("messages") or []
        tool_turns = sum(1 for m in messages if m.get("role") == "tool")
        used_tool = tool_turns > 0
        want_stream = bool(data.get("stream"))
        haystack = json.dumps(messages, ensure_ascii=False)
        if _is_summary_request(messages):
            self._answer_summary()
            return
        if PROBE_CODE_SEQ in haystack:
            # 两条内联命令依次抛出：看到 0 条工具结果抛第一条，看到 1 条抛第二条，之后收尾。
            call = (_CODE_FIRST, _CODE_SECOND)[tool_turns] if tool_turns < 2 else None
        elif PROBE_TOOL_GRANT in haystack:
            call = (_GRANT_FIRST, _GRANT_SECOND)[tool_turns] if tool_turns < 2 else None
        elif PROBE_TAIL_SWAP in haystack:
            call = (_TAIL_FIRST, _TAIL_SECOND)[tool_turns] if tool_turns < 2 else None
        elif PROBE_TAIL_REPLAY in haystack:
            # 只抛一次同一条命令，第二轮就收尾：这一支量的是"批准过的那条下次不再挂卡"。
            call = None if used_tool else _TAIL_FIRST
        elif PROBE_COMPACT in haystack:
            # 同一形状抛两次读取，第三轮收尾；不抛第二次就没有可折叠的历史。
            _Handler.compact_probe_armed = True
            _Handler.compact_calls += 1
            call = (_COMPACT_READS[_Handler.compact_calls - 1]
                    if _Handler.compact_calls <= len(_COMPACT_READS) else None)
        else:
            call = _READ if used_tool else _next_call(haystack)
        self._trace(f"stream={int(want_stream)} 已见工具结果={tool_turns} 消息数={len(messages)}"
                    f" 这轮抛={call['name'] if call else '收尾'}")
        if PROBE_RATE_LIMIT_RETRY in haystack:
            # 只拒第一次：这是"过一会儿就好了"的限流。产品若照旧直接判失败，
            # 这一步红在终态；若换个说法走整包，红在"不该有退路"。
            hit = _Handler.probe_hits.get(PROBE_RATE_LIMIT_RETRY, 0) + 1
            _Handler.probe_hits[PROBE_RATE_LIMIT_RETRY] = hit
            if hit == 1:
                _Handler.probe_rejects[PROBE_RATE_LIMIT_RETRY] = (
                    _Handler.probe_rejects.get(PROBE_RATE_LIMIT_RETRY, 0) + 1)
                self._error_probe(429, "rate_limit_error", "Too Many Requests (stub probe once)")
                return
        if PROBE_RATE_LIMIT_AFTER in haystack:
            # 同样只拒第一次，但明说「Retry-After: 1」。产品盲猜 0.5s 也"能过"，
            # 所以判据不能只看成功，必须看事件里记的等待是不是 1000ms。
            hit = _Handler.probe_hits.get(PROBE_RATE_LIMIT_AFTER, 0) + 1
            _Handler.probe_hits[PROBE_RATE_LIMIT_AFTER] = hit
            if hit == 1:
                _Handler.probe_rejects[PROBE_RATE_LIMIT_AFTER] = (
                    _Handler.probe_rejects.get(PROBE_RATE_LIMIT_AFTER, 0) + 1)
                self._error_probe(
                    429, "rate_limit_error", "Too Many Requests (stub probe retry-after)",
                    {"Retry-After": "1"},
                )
                return
        if PROBE_RATE_LIMIT in haystack:
            # 两种形状都拒：整包调用也一样 429，这一轮没有"退路能救"的余地。
            self._error_probe(429, "rate_limit_error", "Too Many Requests (stub probe)")
            return
        if PROBE_STREAM_400 in haystack and want_stream:
            self._error_probe(400, "invalid_request_error", "该网关不接受 stream=true (stub probe)")
            return
        if PROBE_SLOW in haystack:
            # 睡过 BLANK_MODEL_TIMEOUT_SECONDS（这一格起服务时调成了 6 秒）再正常应答。
            time.sleep(12)
        if want_stream and not used_tool and PROBE_STREAM_FLAKE in haystack:
            self._empty_stream_probe()
            return
        rid = f"resp-stub-{int(time.time() * 1000) % 100000}"
        if call is None or (
            used_tool and PROBE_CODE_SEQ not in haystack and PROBE_COMPACT not in haystack
            and PROBE_TOOL_GRANT not in haystack and PROBE_TAIL_SWAP not in haystack
        ):
            content, tool_calls, finish = "看清了：工作区里有 README.md 与 notes.txt。", [], "stop"
        else:
            content, tool_calls, finish = "", [_envelope(call)], "tool_calls"
        usage = {"prompt_tokens": 610, "completion_tokens": 42, "total_tokens": 652}
        if want_stream:
            frames = [{"choices": [{"index": 0, "delta": {"role": "assistant"}}]}]
            if tool_calls:
                frames.append(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            **_envelope(call),
                                        }
                                    ]
                                },
                            }
                        ]
                    }
                )
            else:
                frames.append({"choices": [{"index": 0, "delta": {"content": content}}]})
            frames.append({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
            frames.append({"choices": [], "usage": usage})
            for frame in frames:
                frame.update({"id": rid, "object": "chat.completion.chunk", "model": data.get("model", "stub")})
            body = b"".join(b"data: " + json.dumps(frame).encode() + b"\n\n" for frame in frames)
            body += b"data: [DONE]\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            # 报文体自己带长度。SSE 通常靠"连接关闭"给最后一个帧定界，而这在 Windows 上
            # 要等一次优雅 FIN：等不到，客户端就只读到半截然后 ReadError——产品那边读出来是
            # `model_unreachable`，任务停在 failed，红的却是脚手架。桩只有 8 帧，整包攒好
            # 再发不损失任何被测的东西：被测的是产品对 `data:` 帧的解析，不是分片时序。
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            self.close_connection = True
        else:
            message: dict = {"role": "assistant", "content": content or None}
            if tool_calls:
                message["tool_calls"] = tool_calls
            payload = {
                "id": rid,
                "object": "chat.completion",
                "model": data.get("model", "stub"),
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": usage,
            }
            raw = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            # 每个响应都收尾连接：keep-alive 会让 httpx 连接池复用一条"服务端已经关了"
            # 的旧连接，端到端就会随机读到 ReadError —— 那是脚手架的抖动，不是产品缺陷。
            self.send_header("Connection", "close")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    def _empty_stream_probe(self) -> None:
        """流式那一格只发心跳注释行：一个 `data:` 帧都没有，产品必须退到整包调用。

        200 是真的、`text/event-stream` 也是真的——像极了"中间层吃掉了 event-stream"。
        报文体自己带长度，所以这一格的红只可能是产品没走退路，不是连接在抖。
        """
        body = b": ping\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        self.close_connection = True

    def _answer_summary(self) -> None:
        """压缩那一笔：第一次 429+Retry-After，第二次回五要素 JSON。

        只拒第一次：这一步要同时证明「压缩退避听得懂表头」与「退避后摘要真的写进来」。
        若一直拒，产品会退回确定性截断，压缩记录还在但 `compaction_response_id`
        会是空——判据会红在最后一行，读起来像产品丢了指针，实际是桩没给过。
        """
        hit = _Handler.probe_hits.get(PROBE_COMPACT, 0) + 1
        _Handler.probe_hits[PROBE_COMPACT] = hit
        if _Handler.compact_probe_armed and hit == 1:
            _Handler.probe_rejects[PROBE_COMPACT] = (
                _Handler.probe_rejects.get(PROBE_COMPACT, 0) + 1)
            self._error_probe(
                429, "rate_limit_error", "Too Many Requests (stub summary probe)",
                {"Retry-After": "1"},
            )
            return
        rid = f"resp-stub-summary-{int(time.time() * 1000) % 100000}"
        payload = {
            "id": rid,
            "object": "chat.completion",
            "model": "stub-model",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": _COMPACT_SUMMARY_JSON},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 320, "completion_tokens": 24, "total_tokens": 344},
        }
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Connection", "close")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _error_probe(
        self, code: int, kind: str, message: str, headers: dict[str, str] | None = None
    ) -> None:
        """按 OpenAI 的错误体回一个状态码：供应商"明说不"的两种形状都在这里。

        与 `_empty_stream_probe` 一样带 Content-Length、由服务端优雅收尾，
        所以这一格红只可能是产品侧的读法问题，不是连接在抖。
        `headers` 用来带限流时供应商给的那句「多久后再来」（`Retry-After`）。
        """
        body = json.dumps({"error": {"message": message, "type": kind, "code": str(code)}}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Connection", "close")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        self.close_connection = True

    def setup(self) -> None:
        _Handler.connections += 1
        super().setup()

    def finish(self) -> None:
        """优雅收尾：先半关（FIN）把响应确定送达，再把接收缓冲读空，然后才 close()。

        每个响应都带 `Connection: close`，所以服务端正是在这里主动关连接。Windows 上
        `close()` 时若接收缓冲里还有客户端没被读走的字节，就会发 RST 代替 FIN，而 RST
        把服务端已经 flush、客户端还没读走的响应字节一起丢掉——端到端读出来是
        `ReadError`，产品侧记成 `model_unreachable`、任务停在 failed，红的还是脚手架。
        这一步之后 `broken` 计不上数（异常发生在内核里），所以只能靠"先优雅关"来断根。
        """
        try:
            self.wfile.flush()
        except OSError:
            pass
        try:
            self.connection.settimeout(1.0)
            self.connection.shutdown(socket.SHUT_WR)
            while self.connection.recv(65536):
                pass
        except OSError:
            pass
        super().finish()


class _StubServer(ThreadingHTTPServer):
    # 默认 backlog=5：任务运行与团队轮并发握手时，超出的连接会被系统直接拒了，
    # 应用侧就报 model_unreachable —— 红了的是脚手架。
    request_queue_size = 128
    daemon_threads = True
    allow_reuse_address = False  # Windows 上这个选项语义不同，靠端口空出来即可


def start_stub() -> ThreadingHTTPServer:
    server = _StubServer(("127.0.0.1", STUB_PORT), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="stub-openai").start()
    return server


# --------------------------------------------------------------------------- #
# 建库 + 起服务
# --------------------------------------------------------------------------- #


class MySQLMissing(RuntimeError):
    """没给 URL、没装驱动，或那台服务器连不上——这三种都属于"这一格今天没法判"。"""


class PostgresMissing(RuntimeError):
    """与 MySQLMissing 同义：PostgreSQL 这一格今天没法判。"""


class PostureMissing(RuntimeError):
    """装置的前提不成立（这台机器给不出任务要的那条边界）。

    它必须红在开场、并且判成「今天没法判」（rc 2），不许混进步骤红里算成产品缺陷：
    26 步红和 1 步红在日志里长得一样，但前者根本没走到被测的那一层。
    """


def _split_postgres_url(url: str):
    from sqlalchemy.engine import make_url

    if not url.strip():
        raise PostgresMissing(
            "没给 BLANK_E2E_POSTGRES_URL（形如 postgresql+asyncpg://postgres@127.0.0.1:5432/kbgai_e2e）")
    try:
        parsed = make_url(url)
    except Exception as error:  # noqa: BLE001 — URL 写坏属于"这一格没法判"
        raise PostgresMissing(f"BLANK_E2E_POSTGRES_URL 解析不了：{error}") from error
    if not parsed.drivername.startswith("postgresql"):
        raise PostgresMissing(f"BLANK_E2E_POSTGRES_URL 不是 postgresql 方言：{parsed.drivername}")
    database = (parsed.database or "").strip()
    # 同 MySQL：库名要拼进 DDL，标识符不能参数化，只认字面标识符。
    if not re.fullmatch(r"[A-Za-z0-9_]{1,60}", database):
        raise PostgresMissing(f"库名不适合当标识符使用：{database!r}")
    return parsed.render_as_string(hide_password=True), parsed.host or "127.0.0.1", int(parsed.port or 5432), (
        parsed.username or "postgres"), parsed.password or "", database


def postgres_admin(url: str, *statements: str) -> None:
    """用 asyncpg 连到 maintenance 库执行 DDL；连不上就抛 PostgresMissing。"""
    try:
        import asyncpg
    except ImportError as error:
        raise PostgresMissing(f"PostgreSQL 驱动没装（asyncpg）：{error}") from error
    import asyncio

    _, host, port, user, password, _ = _split_postgres_url(url)

    async def run() -> None:
        try:
            connection = await asyncpg.connect(
                host=host, port=port, user=user, password=password or None,
                database="postgres", timeout=10,
            )
        except Exception as error:  # noqa: BLE001
            raise PostgresMissing(f"{host}:{port} 连不上：{type(error).__name__}: {error}") from error
        try:
            for statement in statements:
                await connection.execute(statement)
        finally:
            await connection.close()

    asyncio.run(run())


def _split_mysql_url(url: str):
    from sqlalchemy.engine import make_url

    if not url.strip():
        raise MySQLMissing("没给 BLANK_E2E_MYSQL_URL（形如 mysql+aiomysql://user:pw@host:3306/kbgai_e2e）")
    try:
        parsed = make_url(url)
    except Exception as error:  # noqa: BLE001 — URL 写坏属于"这一格没法判"
        raise MySQLMissing(f"BLANK_E2E_MYSQL_URL 解析不了：{error}") from error
    if not parsed.drivername.startswith("mysql"):
        raise MySQLMissing(f"BLANK_E2E_MYSQL_URL 不是 mysql 方言：{parsed.drivername}")
    database = (parsed.database or "").strip()
    # 库名要拼进 DDL（标识符不能参数化），所以只认字面标识符，别的直接拒绝。
    if not re.fullmatch(r"[A-Za-z0-9_]{1,60}", database):
        raise MySQLMissing(f"库名不适合当标识符使用：{database!r}")
    return parsed.render_as_string(hide_password=True), parsed.host or "127.0.0.1", int(parsed.port or 3306), (
        parsed.username or "root"), parsed.password or "", database


def mysql_admin(url: str, *statements: str) -> None:
    """用同步驱动连上那台 MySQL 执行 DDL；连不上就抛 MySQLMissing，别把"没库"演成"库坏了"。"""
    try:
        import pymysql
    except ImportError as error:
        raise MySQLMissing(f"MySQL 驱动没装（uv sync --extra mysql）：{error}") from error
    _, host, port, user, password, _ = _split_mysql_url(url)
    try:
        connection = pymysql.connect(
            host=host, port=port, user=user, password=password,
            connect_timeout=10, read_timeout=120, write_timeout=120,
        )
    except Exception as error:  # noqa: BLE001
        raise MySQLMissing(f"{host}:{port} 连不上：{type(error).__name__}: {error}") from error
    try:
        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)
        connection.commit()
    finally:
        connection.close()


def prepare(engine: str, mysql_url: str, postgres_url: str = "") -> tuple[str, Path]:
    """建出干净的库并跑完整条历史；返回 (database_url, 实例目录)。"""
    instance = SCRATCH / f"instance-{engine}"
    if instance.exists():
        shutil.rmtree(instance)
    (instance / "data").mkdir(parents=True)
    global STUB_LOG
    STUB_LOG = STUB_LOG_BASE.with_name(f"{STUB_LOG_BASE.stem}-{engine}{STUB_LOG_BASE.suffix}")
    STUB_LOG.unlink(missing_ok=True)  # 每一格的轨迹要能单独数，别和上一格混在一份文件里
    shutil.rmtree(WS, ignore_errors=True)  # 每一格从空工作区开始：上一格审批探针写出的文件会顶掉这一格的新建补丁（新文件补丁只认空文件）
    WS.mkdir(parents=True, exist_ok=True)
    (WS / "README.md").write_text("# 桩工作区\n用于方言端到端驱动。\n", encoding="utf-8")
    (WS / "notes.txt").write_text("notes\n", encoding="utf-8")
    # 两份内容不同的大文件：同一份内容第二次读会被 microcompact 按指纹折叠，
    # 折叠后尾部预算吃得下，压缩就不会发生（探针会红成"没有压缩记录"）。
    (WS / "compact-a.txt").write_text(("甲" * 60 + "\n") * 100, encoding="utf-8")
    (WS / "compact-b.txt").write_text(("乙" * 60 + "\n") * 100, encoding="utf-8")
    if engine == "mysql":
        _, _, _, _, _, database = _split_mysql_url(mysql_url)
        mysql_admin(
            mysql_url,
            f"drop database if exists {database}",
            f"create database {database} character set utf8mb4",
        )
        url = mysql_url
    elif engine == "postgres":
        _, _, _, _, _, database = _split_postgres_url(postgres_url)
        postgres_admin(
            postgres_url,
            f"drop database if exists {database} with (force)",
            f"create database {database}",
        )
        url = postgres_url
    else:
        url = f"sqlite+aiosqlite:///{(instance / 'e2e.db').as_posix()}"
    env = dict(os.environ, BLANK_DATABASE_URL=url, PYTHONIOENCODING="utf-8")
    done = subprocess.run(
        [str(PY), "-X", "utf8", "-m", "alembic", "upgrade", "head"],
        cwd=str(API_DIR), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=1800,
    )
    if done.returncode:
        raise RuntimeError(f"alembic upgrade head 失败（{engine}）：\n{done.stdout[-2000:]}{done.stderr[-2000:]}")
    return url, instance


def boot(url: str, instance: Path) -> subprocess.Popen:
    env = dict(
        os.environ,
        BLANK_DATABASE_URL=url,
        BLANK_SETTINGS_FILE=str(instance / "data" / "settings.json"),
        BLANK_SECRET_KEY_FILE=str(instance / "data" / "blank_secret.key"),
        # 带令牌那一形态：护栏挂在路由层，装置以前从不设令牌，等于这一路一次都没被真 HTTP 穿过。
        BLANK_API_TOKEN=DEVICE_TOKEN,
        # 见 `PROBE_SLOW`：让"模型太慢"这一格在一轮里跑得完。
        BLANK_MODEL_TIMEOUT_SECONDS=DEVICE_MODEL_TIMEOUT,
        # 姿态由装置自己钉死（见 `DEVICE_SANDBOX_ENABLED` 那段）：这一格量的是换库，
        # 不是"这台机器的 docker 在不在"。
        BLANK_SANDBOX_ENABLED=DEVICE_SANDBOX_ENABLED,
        PYTHONIOENCODING="utf-8",
    )
    log = (instance / "api.log").open("wb")
    proc = subprocess.Popen(
        [str(PY), "-X", "utf8", "-m", "uvicorn", "blank_api.app:app",
         "--host", "127.0.0.1", "--port", str(API_PORT)],
        cwd=str(instance), env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{API_PORT}/api/v1"
    deadline = time.time() + 90
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"uvicorn 提前退出 rc={proc.returncode}：\n"
                               f"{(instance / 'api.log').read_text(encoding='utf-8', errors='replace')[-2500:]}")
        try:
            if httpx.get(f"{base}/health", timeout=3.0, trust_env=False).json().get("database") == "connected":
                return proc
        except Exception:
            time.sleep(0.5)
    proc.kill()
    raise RuntimeError(f"健康检查超时（90s 内 /health 没说 database=connected）；日志尾部：\n"
                       f"{(instance / 'api.log').read_text(encoding='utf-8', errors='replace')[-2500:]}")


# --------------------------------------------------------------------------- #
# 驱动：每一步都能失败
# --------------------------------------------------------------------------- #


class Drive:
    def __init__(self, base: str) -> None:
        # 令牌在这里，而不是逐步传：漏传一步只会红成"装置自己没带头"，
        # 让"每一条接口都要鉴权"这条判据没法被读成产品缺陷。
        self.c = httpx.Client(base_url=base, timeout=60.0, trust_env=False,
                              headers={"Authorization": f"Bearer {DEVICE_TOKEN}"})
        # 鉴权之外那一层（/openapi.json、/docs、/redoc）住在根上、不在 /api/v1 前缀里，
        # 而接口面枚举出来的路径又都带 /api/v1 前缀——所以根上的客户端要单独开两个：
        # 一个带头（读接口面、验"带令牌不该被挡"），一个匿名（验"匿名一律 401"）。
        root = base.rstrip("/").rsplit("/api/v1", 1)[0]
        self.root = httpx.Client(base_url=root, timeout=30.0, trust_env=False,
                                 headers={"Authorization": f"Bearer {DEVICE_TOKEN}"})
        self.anon = httpx.Client(base_url=root, timeout=30.0, trust_env=False)
        self.rows: list[tuple[str, str, str]] = []
        self.state: dict[str, str] = {}

    def step(self, name: str, fn) -> None:
        try:
            detail = fn() or ""
            self.rows.append((name, "PASS", str(detail)[:200]))
        except Exception as error:  # noqa: BLE001 — 驱动就是要接住一切，再按引擎对照
            # 红的那一步存全文：判据（尤其是"失败原因"）一句往往比一句长，
            # 在记录时就裁掉等于每轮都要重跑一遍才能看见下一句。
            self.rows.append((name, "FAIL", f"{type(error).__name__}: {error}"))

    def expect(self, resp: httpx.Response, codes: tuple[int, ...]) -> object:
        if resp.status_code not in codes:
            raise AssertionError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json() if resp.content else None

    def finish(self, engine: str) -> int:
        fails = [row for row in self.rows if row[1] == "FAIL"]
        if len(self.rows) != EXPECTED_STEPS:
            # 装置被改短（或某一步抛在 step 之外）时，"每一格全绿"这句话不能照发：
            # 全绿只代表跑过的这几步没红，不代表该跑的都跑过。
            print(f"[自检] {engine} 这一格步数 {len(self.rows)} ≠ 声明的 {EXPECTED_STEPS}")
            self.rows.append(("<步骤清单自检>", "FAIL", f"跑了 {len(self.rows)} 步，声明 {EXPECTED_STEPS} 步"))
            fails = [row for row in self.rows if row[1] == "FAIL"]
        print(f"\n===== 驱动结果（{engine}）：{len(self.rows)} 步，失败 {len(fails)} =====")
        for name, status, detail in self.rows:
            print(f"[{status}] {name:38s} {detail.splitlines()[0][:120] if detail else ''}")
            if status == "FAIL":
                # 红的那一步把判据整段打出来。以前表格里只留 400 字，"失败原因"恰好被
                # 裁在窗口外——每一轮都要重跑一次才能看见下一句话，比自己红更浪费。
                for line in textwrap.fill(detail, width=112, initial_indent=" " * 46,
                                          subsequent_indent=" " * 46).splitlines():
                    print(line)
        return len(fails)


def check_posture(d: Drive) -> dict:
    """开场先问实例「这台机器此刻靠什么隔离」，把装置的前提当成一条判定来守。

    读的是 `/settings/safety/runtime` 里那份**现场解析**的结果——任务起跑时装配层调用
    的同一个 `resolve_tier`，不是装置另算一遍（另算就退化成"我以为环境是这样"）。
    `available=False` 意味着每一条 `/run` 都会当场 409，85 步里有 26 步要红，
    而红的成因跟换库无关，所以这里判「今天没法判」。
    """
    body = d.expect(d.c.get("/settings/safety/runtime"), (200,))
    backend = body.get("backend") or {}
    print(f"[姿态] 旋钮 BLANK_E2E_SANDBOX_ENABLED={DEVICE_SANDBOX_ENABLED} · "
          f"backend={backend.get('backend')} isolation={backend.get('isolation')} "
          f"tier={backend.get('tier')} strength={backend.get('strength')} "
          f"docker={backend.get('docker_available')} available={backend.get('available')}")
    if not backend.get("available", False):
        raise PostureMissing(
            f"沙箱档位 available=False：reason={backend.get('reason')}，"
            f"notice={backend.get('notice')}。任务装配会当场拒绝每一次 /run，"
            f"这一格还没走到被测的那一层（装置旋钮 BLANK_E2E_SANDBOX_ENABLED={DEVICE_SANDBOX_ENABLED}）"
        )
    return backend


TRANSPORT_FLAKE_MARKERS = ("ReadError", "ConnectError", "ConnectTimeout", "PoolTimeout", "RemoteProtocolError")


def _is_transport_flake(resp: httpx.Response) -> bool:
    """模型服务"连不上/读到一半断了"属于脚手架层症状；产品缺陷不会长成这样。

    判据是错误里带的 httpx 异常名——真缺陷在这一格里长出来的是 500、MissingGreenlet、
    OperationalError 或任务停在 failed，都不会冒充这几个名字，所以这条重试不会把
    产品问题洗绿：重试到第三次还不干净就照红。
    """
    if resp.status_code not in {502, 503}:
        return False
    return any(marker in resp.text for marker in TRANSPORT_FLAKE_MARKERS)


def run_task_with_flake_guard(d: Drive, pid: str, *, task_key: str = "task",
                              goal: str = "列出工作区文件", title: str = "读工作区",
                              wanted: frozenset[str] = frozenset({"succeeded"})) -> str:
    """把任务跑到 `wanted` 里的状态；桩与网关之间的连接抖动换一个全新任务重来（最多两次）。

    抖动次数必须打进这一步的详情里（不藏）：#50 那批两边同跑时，两次三引擎同进程跑
    都在第 19 步撞上 `model_unreachable（ReadError）`，而单独跑四个回合全绿——
    分不清"只有 MySQL 红"是方言分歧还是脚手架抖，比红更糟。

    审批/提问那几步也走同一条守卫：它们同样要等一次模型调用，抖一次就整格假红。
    """
    provider = d.state.get("provider", "")
    task = d.state.get(task_key, "")
    retries = 0
    while True:
        resp = d.c.post(f"/tasks/{task}/run", json={}, timeout=300.0)
        if _is_transport_flake(resp) and retries < 2:
            retries += 1
            task = d.expect(d.c.post(
                f"/projects/{pid}/tasks",
                json={"title": f"{title}（重试 {retries}）", "goal": goal, "provider_id": provider},
            ), (201,))["id"]
            d.state[task_key] = task
            time.sleep(1.0)  # 让上一次失败调用留下的连接与状态先落地，立刻重打只会再撞一遍
            continue
        if resp.status_code >= 500:
            raise AssertionError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        if resp.status_code != 200:
            # 非 2xx 当场念出来。2026-10-26 那批 26 步全红，症状却写成「status=draft」：
            # `/run` 其实当场就 409（`sandbox_tier_unavailable`），被这条守卫咽掉以后，
            # 读日志的人只看得见现象、看不见成因，只能重跑一遍才知道。
            raise AssertionError(f"/run 没被受理：HTTP {resp.status_code}: {resp.text[:300]}")
        break
    deadline = time.time() + 180
    status = ""
    while time.time() < deadline:
        status = d.c.get(f"/tasks/{task}").json().get("status", "")
        if status in {"succeeded", "failed", "cancelled", "waiting_approval", "waiting_answer"}:
            break
        time.sleep(2)
    if status not in wanted:
        raise AssertionError(f"任务没跑到 {sorted(wanted)}（status={status}）{_why(d, task)}")
    detail = f"status={status}"
    return f"{detail}（脚手架抖动重试 {retries} 次）" if retries else detail


def _why(d: Drive, task: str) -> str:
    """红的时候把"图里到底走了哪几步"一并打出来。

    这一格反复红在同一个地方（同一个探针，有时挂卡、有时直接 succeeded），只报
    `status=succeeded` 等于下一轮从头猜：卡是没挂、工具是没执行、还是执行了没记账，
    三件事在证据包里是三个完全不同的读法。时间线 + 工具台账一次就分得开。
    取证本身不许再把这一步弄红，所以整段兜住异常，读不到就只说读不到。
    """
    try:
        evidence = d.expect(d.c.get(f"/tasks/{task}/evidence"), (200,))
        timeline = evidence.get("timeline") or []
        # 顺序是有讲究的：判据长的时候读的人只看得到开头，所以开头必须是"为什么红"。
        # 终态那一类事件的载荷里写着原因，光念时间线类型等于没念。
        reasons = [
            f"{row.get('type')}={json.dumps(row.get('payload') or {}, ensure_ascii=False)[:400]}"
            for row in timeline
            if row.get("type") in {"task.failed", "error", "node.failed", "task.paused",
                                   "validation.completed", "model.stream_fallback"}
        ]
        parts = []
        for row in timeline[-18:]:
            payload = row.get("payload") or {}
            if row.get("type") not in {"tool.requested", "tool.completed", "approval.required",
                                       "approval.granted", "approval.denied"}:
                parts.append(str(row.get("type")))
                continue
            head = f"{row['type']}[{payload.get('tool')}"
            arguments = payload.get("arguments") or {}
            if arguments:
                patch = str(arguments.get("patch") or "")
                head += f" 参数={sorted(arguments)} 补丁头={patch[:9]} 补丁长={len(patch)}"
            for key, label in (("status", "状态"), ("risk_level", "风险")):
                if payload.get(key) is not None:
                    head += f" {label}={payload.get(key)}"
            permission = payload.get("permission") or {}
            if permission:
                head += f" 判定={permission.get('rule') or permission}"
            if payload.get("summary"):
                head += f" 摘要={str(payload['summary'])[:90]}"
            parts.append(head + "]")
        tools = [f"{row.get('name')}={row.get('status')}" for row in evidence.get("tools") or []]
        # 审批与否取决于许可模式，而模式有两处来源（全局设置 + 这次运行的入参）。
        # 探针的运行体不带 permission_mode，所以全局那份是唯一变量——必须一起打出来。
        settings = d.expect(d.c.get("/settings/agent"), (200,))
        return ("｜终态原因 " + " ;; ".join(reasons[-3:])
                + f"｜工具台账 {tools}"
                + f"｜许可模式={settings.get('permission_mode')} 高风险自动放行={settings.get('approve_high_risk')}"
                + "｜时间线 " + " → ".join(parts))
    except Exception as exc:  # noqa: BLE001 - 取证通道坏了不该把判定改成另一个异常
        return f"｜取证失败：{type(exc).__name__}: {exc}"


def drive(d: Drive) -> None:
    d.step("01 项目创建", lambda: d.state.setdefault(
        "project", d.expect(d.c.post("/projects", json={"name": "e2e-dialect", "description": "描述文本列，MySQL 1101 的那一类"}), (201,))["id"]))
    pid = d.state.get("project", "")

    d.step("02 工作区注册", lambda: d.state.setdefault(
        "workspace", d.expect(d.c.post(f"/projects/{pid}/workspaces", json={"path": str(WS), "label": "ws"}), (200, 201))["id"]))
    d.step("03 激活工作区", lambda: d.expect(
        d.c.post(f"/projects/{pid}/workspaces/{d.state.get('workspace')}/activate", json={}), (200,)))
    d.step("04 第二个工作区注册", lambda: d.state.setdefault(
        "workspace2", d.expect(d.c.post(f"/projects/{pid}/workspaces", json={"path": str(SCRATCH), "label": "ws2"}), (200, 201))["id"]))
    d.step("05 工作区真实路径回读", lambda: d.expect(
        d.c.get(f"/projects/{pid}/workspaces/{d.state.get('workspace2')}/path"), (200,)))
    d.step("06 项目改名", lambda: d.expect(d.c.patch(f"/projects/{pid}", json={"name": "e2e-dialect-2"}), (200,)))
    d.step("07 项目列表", lambda: len(d.expect(d.c.get("/projects"), (200,))))
    d.step("08 文件树", lambda: d.expect(d.c.get(f"/projects/{pid}/tree"), (200,)))
    d.step("09 文件读取", lambda: d.expect(d.c.get(f"/projects/{pid}/file?path=README.md"), (200,)))

    d.step("10 供应商创建（桩）", lambda: d.state.setdefault("provider", d.expect(d.c.post(
        "/providers",
        json={"name": "stub", "kind": "openai_compatible", "base_url": f"http://127.0.0.1:{STUB_PORT}/v1",
              "api_key": "test-key", "model": "stub-model", "supports_tools": True, "max_tokens": 512,
              "context_window": 8000},
    ), (201,))["id"]))
    d.step("11 供应商改默认模型", lambda: d.expect(
        d.c.patch(f"/providers/{d.state.get('provider')}", json={"model": "stub-model"}), (200,)))
    d.step("12 供应商连通测试", lambda: d.expect(d.c.post(f"/providers/{d.state.get('provider')}/test", json={}), (200,)))
    d.step("13 供应商用量代理调用", lambda: d.expect(d.c.post(
        f"/providers/{d.state.get('provider')}/chat", json={"message": "你好"}), (200,)))

    d.step("14 附件上传", lambda: _create_upload(d, pid))
    d.step("15 附件回读", lambda: _assert_upload_bytes(d, pid))

    d.step("16 任务创建", lambda: d.state.setdefault("task", d.expect(d.c.post(
        f"/projects/{pid}/tasks",
        json={"title": "读工作区", "goal": "列出工作区文件", "provider_id": d.state.get("provider")},
    ), (201,))["id"]))
    d.step("17 草稿任务创建（待取消）", lambda: d.state.setdefault("draft_task", d.expect(d.c.post(
        f"/projects/{pid}/tasks", json={"title": "取消我", "goal": "g", "provider_id": d.state.get("provider")}),
        (201,))["id"]))
    d.step("18 取消草稿任务（UPDATE+TaskOut）", lambda: _assert_cancelled(d))
    d.step("19 任务运行到终态", lambda: run_task_with_flake_guard(d, pid))
    tid = d.state.get("task", "")
    d.step("20 事件列表含工具调用", lambda: _assert_events(d, tid))
    d.step("21 证据包", lambda: len(json.dumps(d.expect(d.c.get(f"/tasks/{tid}/evidence"), (200,)), ensure_ascii=False)))
    d.step("22 快照列表", lambda: len(d.expect(d.c.get(f"/tasks/{tid}/snapshots"), (200,))))
    d.step("23 diff", lambda: d.expect(d.c.get(f"/tasks/{tid}/diff"), (200,)))
    d.step("24 已结束任务取消被拒(409)", lambda: _assert_cancel_rejected(d, tid))
    d.step("25 任务列表", lambda: len(d.expect(d.c.get("/tasks"), (200,))))

    d.step("26 用量汇总", lambda: _assert_usage(d))
    d.step("27 可观测汇总", lambda: _assert_keys(d.c.get("/observability/summary"), ("model_calls", "roles", "runs", "tools")))
    d.step("28 路由查询", lambda: d.expect(d.c.get("/observability/routing"), (200,)))

    d.step("29 会话创建", lambda: d.state.setdefault("thread", d.expect(
        d.c.post(f"/projects/{pid}/threads", json={"title": "会话一"}), (201,))["id"]))
    d.step("30 会话开一轮", lambda: d.state.setdefault("thread_task", d.expect(d.c.post(
        f"/threads/{d.state.get('thread')}/turns",
        json={"title": "第二轮", "goal": "接着聊", "provider_id": d.state.get("provider")},
    ), (201,))["id"]))
    d.step("31 会话详情", lambda: d.expect(d.c.get(f"/threads/{d.state.get('thread')}"), (200,)))
    d.step("32 会话分叉", lambda: d.expect(d.c.post(f"/threads/{d.state.get('thread')}/fork", json={"title": "分叉"}), (201,)))
    d.step("33 会话归档", lambda: d.expect(d.c.delete(f"/threads/{d.state.get('thread')}"), (204,)))

    d.step("34 团队 bootstrap", lambda: d.expect(d.c.get("/team/bootstrap"), (200,)))
    d.step("35 Bot 创建", lambda: d.state.setdefault("bot", d.expect(d.c.post(
        "/team/bots", json={"name": "小查", "role": "检索", "instructions": "只读文件并汇报",
                            "provider_id": d.state.get("provider"), "permission_mode": "default"},
    ), (201,))["id"]))
    d.step("36 Bot 更新", lambda: d.expect(d.c.patch(f"/team/bots/{d.state.get('bot')}", json={"role": "检索与摘要"}), (200,)))
    d.step("37 群创建", lambda: d.state.setdefault("group", d.expect(d.c.post(
        "/team/groups", json={"project_id": pid, "name": "e2e 群", "topic": "换库验证",
                              "member_ids": [d.state.get("bot", "")]},
    ), (201,))["group"]["id"]))
    gid = d.state.get("group", "")
    d.step("38 群消息（触发一轮）", lambda: d.state.setdefault("round", d.expect(d.c.post(
        f"/team/groups/{gid}/messages", json={"content": "请大家各说一句：这个工作区里有什么？"}), (201,))))
    d.step("39 群状态", lambda: _assert_group_state(d, gid))
    d.step("40 群消息轮询", lambda: len(d.expect(d.c.get(f"/team/groups/{gid}/state"), (200,)).get("messages", [])))
    d.step("41 全文搜索", lambda: _assert_search(d, pid))
    d.step("42 群停止", lambda: d.expect(d.c.post(f"/team/groups/{gid}/stop", json={}), (200,)))

    d.step("43 记忆候选创建（接受）", lambda: d.state.setdefault("candidate", d.expect(d.c.post(
        "/skills/memory-candidates", json={"text": "工作区里有两个文件", "source": "e2e"}), (201,))["id"]))
    d.step("44 记忆候选接受", lambda: d.state.setdefault("skill", d.expect(d.c.post(
        f"/skills/memory-candidates/{d.state.get('candidate')}/accept", json={}), (200, 201))["id"]))
    d.step("45 记忆候选创建（拒绝）", lambda: d.state.setdefault("candidate2", d.expect(d.c.post(
        "/skills/memory-candidates", json={"text": "要被拒掉的那条", "source": "e2e"}), (201,))["id"]))
    d.step("46 记忆候选拒绝", lambda: d.expect(
        d.c.post(f"/skills/memory-candidates/{d.state.get('candidate2')}/reject", json={}), (200,)))
    d.step("47 技能列表", lambda: len(d.expect(d.c.get("/skills"), (200,))))
    d.step("48 技能改写（产生历史）", lambda: d.expect(d.c.patch(
        f"/skills/{d.state.get('skill')}", json={"instructions": "先列目录，再读关键文件，最后一句话结论。"}), (200,)))
    d.step("49 技能回滚", lambda: d.expect(d.c.post(f"/skills/{d.state.get('skill')}/rollback", json={}), (200,)))
    d.step("50 技能停用", lambda: d.expect(d.c.post(f"/skills/{d.state.get('skill')}/disable", json={}), (200,)))
    d.step("51 技能启用", lambda: d.expect(d.c.post(f"/skills/{d.state.get('skill')}/enable", json={}), (200,)))

    d.step("52 运行时设置读取", lambda: d.expect(d.c.get("/settings/agent"), (200,)))
    d.step("53 特性开关读取", lambda: d.expect(d.c.get("/settings/features"), (200,)))

    # ---- 审批与答题那两格（#52）：中断→落库→HTTP 决定→恢复→终态，两方言各跑一遍 ----
    d.step("54 高风险写入挂起成审批卡", lambda: _assert_approval_card(d, pid))
    d.step("55 审批卡内容可核对", lambda: _assert_card_shape(d, d.state.get("approval_task", "")))
    d.step("56 审批卡走答题口报 409", lambda: _assert_wrong_channel(d, d.state.get("approval_task", "")))
    d.step("57 批准后跑到终态且文件真写出", lambda: _approve_and_finish(d))
    d.step("58 明文密钥不落进库里的 diff", lambda: _assert_diff_redacted(d))
    d.step("59 驳回后不执行且留痕", lambda: _assert_denied(d, pid))
    d.step("60 提问卡挂起并可答", lambda: _assert_question_card(d, pid))
    d.step("61 答题后答案进了上下文", lambda: _answer_and_finish(d))

    # ---- 流式退路（#52 补的一格）：网关不认 stream 时要把活干完，不是静默变空回复 ----
    d.step("62 流式空帧要退回整包调用", lambda: _assert_stream_fallback(d, pid))

    # ---- 群聊路径的审批（#55）：成员/主理人的卡也要挂得出、批得动、拒得掉 ----
    # 前面 54–62 走的是 `/tasks/{id}/approval` 单任务入口；团队群聊走的是
    # `/team/messages/{id}/approval`，中间隔着编排器、消息状态与同一轮预算。
    d.step("63 群聊审批卡挂起", lambda: _assert_team_approval_card(d, gid))
    d.step("64 群聊审批走错入口报 409", lambda: _assert_team_wrong_channel(d, gid))
    d.step("65 主理人批准后成员继续并写出文件", lambda: _assert_team_admin_approval(d, gid))
    d.step("66 成员驳回后不过期执行、轮能收尾", lambda: _assert_team_member_denied(d, gid))

    d.step("67 删除供应商后账本还在", lambda: _assert_ledger_survives(d))

    d.step("68 工作区解绑", lambda: d.expect(
        d.c.delete(f"/projects/{pid}/workspaces/{d.state.get('workspace2')}"), (204,)))
    d.step("69 群删除", lambda: d.expect(d.c.delete(f"/team/groups/{gid}"), (204,)))
    d.step("70 Bot 删除", lambda: d.expect(d.c.delete(f"/team/bots/{d.state.get('bot')}"), (204,)))
    d.step("71 任务删除", lambda: d.expect(d.c.delete(f"/tasks/{tid}"), (204,)))
    d.step("72 项目删除", lambda: d.expect(d.c.delete(f"/projects/{pid}"), (204,)))

    # ---- 鉴权那一形态（#53）：装置以前从不设令牌，这一路一次 HTTP 都没被穿过 ----
    d.step("73 匿名打每一条接口都被拒", lambda: _assert_anonymous_denies_every_route(d))
    d.step("74 接口面不匿名可读", lambda: _assert_doc_surface_guarded(d))
    d.step("75 带令牌不应当被鉴权挡住", lambda: _assert_token_admits_every_route(d))

    # ---- 退路的另外两种形状（#53）：一种该被救回来，两种该红得有名有姓 ----
    d.step("76 网关拒 stream 时退回整包并把活干完", lambda: _assert_stream_400_recovers(d))
    d.step("77 两种形状都 429 时不许静默成功", lambda: _assert_rate_limit_fails_loudly(d))
    d.step("78 模型太慢要按超时失败", lambda: _assert_model_timeout(d))

    # ---- 批准记忆的粒度（#54）：卡片建议与账本规则必须是同一串 ----
    d.step("79 解释器命令的建议前缀带正文", lambda: _assert_memo_card(d))
    d.step("80 换一段正文必须重新挂卡", lambda: _assert_memo_granularity(d))

    # ---- 限流的另一面（#56）：只拒一次时，退避后必须把活干完 ----
    d.step("81 429 退避一次后把活干完", lambda: _assert_rate_limit_retry_recovers(d))
    # ---- 供应商明说等多久（#57）：等待要听 Retry-After，不是自己猜 ----
    d.step("82 429 的 Retry-After 要算进等待", lambda: _assert_retry_after_honored(d))
    # ---- 压缩那一笔也要真发生（#58）：窗口链、替换引用、摘要退避都要在两种方言上亮出来 ----
    d.step("83 压缩真发生且摘要那笔 429 听 Retry-After", lambda: _assert_compaction_fold(d))
    # ---- 免问范围不许由客户端参数放大（#82）：卡上没有的档位，账上也不记 ----
    d.step("84 scope=tool 批准后下一条高风险命令仍要问", lambda: _assert_tool_scope_not_recorded(d))
    # ---- 记下的规则必须「不多不少正好是执行的那一条」（#87）：两个方向同跑 ----
    d.step("85 只换载荷尾巴要重新挂卡且批准过的那条不再挂卡",
           lambda: _assert_tail_swap_and_replay(d))


def _assert_cancelled(d: Drive) -> str:
    body = d.expect(d.c.post(f"/tasks/{d.state.get('draft_task')}/cancel", json={}), (200,))
    if body.get("status") != "cancelled":
        raise AssertionError(f"取消后状态不是 cancelled：{body.get('status')}")
    return f"cancelled，updated_at={body.get('updated_at')}"


def _assert_cancel_rejected(d: Drive, tid: str) -> str:
    resp = d.c.post(f"/tasks/{tid}/cancel", json={})
    if resp.status_code != 409:
        raise AssertionError(f"已结束的任务应该 409，实际 {resp.status_code}: {resp.text[:200]}")
    return resp.json()["error"]["code"]


def _create_upload(d: Drive, pid: str) -> str:
    body = d.expect(d.c.post(
        f"/projects/{pid}/uploads",
        json={"name": "logo.png", "mime": "image/png", "data": PNG_B64}), (201,))
    if body["size"] != len(PNG_BYTES):
        raise AssertionError(f"上传回执里的 size 不对：{body['size']} != {len(PNG_BYTES)}")
    d.state["upload"] = body["id"]
    d.state["upload_kind"] = body["kind"]
    return body["id"]


def _assert_upload_bytes(d: Drive, pid: str) -> int:
    if d.state.get("upload_kind") != "image":
        raise AssertionError(f"附件类别不是 image：{d.state.get('upload_kind')}")
    resp = d.c.get(f"/projects/{pid}/uploads/{d.state.get('upload')}")
    if resp.status_code != 200:
        raise AssertionError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    if resp.content != PNG_BYTES:
        raise AssertionError(f"附件内容不对：{resp.content!r} != {PNG_BYTES!r}")
    return len(resp.content)


def _assert_events(d: Drive, tid: str) -> int:
    events = d.expect(d.c.get(f"/tasks/{tid}/events"), (200,))
    kinds = {e.get("kind") or e.get("type") for e in events}
    if not any("tool" in str(k) for k in kinds):
        raise AssertionError(f"事件里没有工具调用形状：{sorted(str(k) for k in kinds)[:8]}")
    return len(events)


def _assert_usage(d: Drive) -> int:
    body = d.expect(d.c.get("/usage/summary"), (200,))
    rows = body.get("providers") or []
    if not rows:
        raise AssertionError(f"用量汇总里没有供应商行：{json.dumps(body, ensure_ascii=False)[:200]}")
    prompt = sum(int(row.get("prompt_tokens") or 0) for row in rows)
    completion = sum(int(row.get("completion_tokens") or 0) for row in rows)
    if prompt <= 0 or completion <= 0:
        raise AssertionError(f"桩应当产生的 token 没落账：prompt={prompt} completion={completion}")
    if body.get("total_calls") is None:
        raise AssertionError(f"用量汇总缺 total_calls：{sorted(body)}")
    return prompt + completion


def _assert_keys(resp, keys: tuple[str, ...]) -> int:
    body = resp.json()
    if resp.status_code != 200:
        raise AssertionError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    missing = [k for k in keys if k not in json.dumps(body, ensure_ascii=False)]
    if missing:
        raise AssertionError(f"响应里缺键 {missing}")
    return len(json.dumps(body))


def _assert_group_state(d: Drive, gid: str) -> str:
    """群状态返回的是单个 `round`（当前/最近那一轮），不是 `rounds` 列表。"""
    deadline = time.time() + 240
    last: dict = {}
    while time.time() < deadline:
        last = d.expect(d.c.get(f"/team/groups/{gid}/state"), (200,))
        row = last.get("round") or {}
        if (row.get("status") or "").lower() in {"done", "completed", "finished", "failed", "cancelled", "stopped"}:
            break
        time.sleep(3)
    if not last.get("round"):
        raise AssertionError(f"群状态里没有轮次：{sorted(last)}")
    row = last["round"]
    if not last.get("messages"):
        raise AssertionError("群里一条消息都没有")
    if not last.get("members"):
        raise AssertionError("群成员列表是空的")
    return (f"轮 {row.get('id')} 状态 {row.get('status')}，发言 {len(row.get('spoken') or [])} 人，"
            f"消息 {len(last['messages'])} 条，本轮 tokens={row.get('tokens')}")


def _assert_search(d: Drive, pid: str) -> int:
    body = d.expect(d.c.get(f"/team/search?q=工作区&project_id={pid}"), (200,))
    hits = body.get("results") if isinstance(body, dict) else body
    if not hits:
        raise AssertionError(f"搜「工作区」应有命中，实际 {json.dumps(body, ensure_ascii=False)[:200]}")
    return len(hits)


# --------------------------------------------------------------------------- #
# 群聊路径的审批（#55）
# --------------------------------------------------------------------------- #
# #52 把单任务的 `/tasks/{id}/approval` 跑成了实物，但团队群聊走的是另一条路：
# 编排器先建成员任务，挂起后分别写进 TeamMessage / TeamRound 两个状态，再由
# `/team/messages/{id}/approval` 唤醒同一个编排器。这条路上有一堆只有端到端才看得见的
# 东西：消息状态、轮状态、同轮预算、恢复后的队列。这一格就是把它走一遍。
TEAM_APPROVAL_TIMEOUT = 300.0


def _team_state(d: Drive, gid: str) -> dict:
    state = d.expect(d.c.get(f"/team/groups/{gid}/state"), (200,))
    if not isinstance(state, dict):
        raise AssertionError("群状态不是对象")
    return state


def _team_message(state: dict, message_id: int) -> dict:
    for row in state.get("messages") or []:
        if int(row.get("id") or 0) == int(message_id):
            return row
    raise AssertionError(
        f"群状态里找不到消息 {message_id}（现有 {[row.get('id') for row in state.get('messages') or []]}）"
    )


def _team_pending(state: dict) -> list[dict]:
    return [row for row in state.get("pending") or [] if row.get("approvals")]


def _team_card(entry: dict) -> dict:
    cards = list(entry.get("approvals") or [])
    if len(cards) != 1:
        raise AssertionError(f"这条群消息应该恰好挂着一张审批卡，实际 {len(cards)} 张")
    card = cards[0]
    if card.get("tool") != "apply_patch":
        raise AssertionError(f"群聊审批卡指向错了工具：{card.get('tool')}")
    if card.get("risk_level") not in ("high", "critical"):
        raise AssertionError(f"群聊高风险写入的风险等级不对：{card.get('risk_level')}")
    if card.get("expired"):
        raise AssertionError("刚挂起的群聊审批卡就被判成过期了")
    return card


def _team_wait_pending(d: Drive, gid: str, *, after: int = 0, where: str) -> tuple[dict, dict]:
    deadline = time.time() + TEAM_APPROVAL_TIMEOUT
    last: dict = {}
    while time.time() < deadline:
        last = _team_state(d, gid)
        rows = [row for row in _team_pending(last) if int(row.get("message_id") or 0) != after]
        if rows:
            return last, rows[0]
        round_status = str((last.get("round") or {}).get("status") or "").lower()
        if round_status in {"completed", "failed", "cancelled", "stopped"}:
            break
        time.sleep(2)
    raise AssertionError(
        f"{where}没等到审批卡（轮状态={(last.get('round') or {}).get('status')}，"
        f"pending={json.dumps(last.get('pending') or [], ensure_ascii=False)[:300]}）"
    )


def _assert_team_approval_card(d: Drive, gid: str) -> str:
    """群聊里第一张卡必须挂得出来，而且要在消息与轮两处同时如实标成等待。

    把 `max_speakers` 收到 2 是有意的：这一轮排队的是「主理人先答 → 成员 @小查」，
    到这就够走完批准与驳回两条路，不会再多出一轮主理人收尾把卡的数量搅浑。
    """
    d.expect(d.c.patch(f"/team/groups/{gid}", json={"max_speakers": 2}), (200,))
    target = WS / PROBE_PATH
    # 上一格（单任务批准）已经写过同名文件；先删掉，这一步才能证"群聊批准真的写出了"。
    target.unlink(missing_ok=True)
    d.expect(d.c.post(
        f"/team/groups/{gid}/messages",
        json={"content": f"请在工作区写一份配置片段（{PROBE_APPROVAL}）@小查"},
    ), (201,))
    state, entry = _team_wait_pending(d, gid, where="群聊首张审批卡")
    card = _team_card(entry)
    message = _team_message(state, int(entry["message_id"]))
    if message.get("status") != "waiting_approval":
        raise AssertionError(f"群消息挂着卡但状态不是 waiting_approval：{message.get('status')}")
    if str((state.get("round") or {}).get("status")) != "waiting_approval":
        raise AssertionError(f"群消息挂着卡但轮状态不是 waiting_approval：{(state.get('round') or {}).get('status')}")
    if entry.get("bot_name") != "小白管家":
        raise AssertionError(f"这一轮应当由主理人先发言，实际挂卡的是 {entry.get('bot_name')}")
    d.state["team_admin_mid"] = int(entry["message_id"])
    d.state["team_admin_task"] = str(entry.get("task_id") or "")
    return (f"消息 {entry['message_id']}（{entry.get('bot_name')}）挂起，"
            f"工具={card.get('tool')} 风险={card.get('risk_level')}")


def _assert_team_wrong_channel(d: Drive, gid: str) -> str:
    """群聊的审批卡与问题卡也必须各走各的口，409 的错误码要分得开。"""
    mid = int(d.state.get("team_admin_mid") or 0)
    wrong = d.c.post(f"/team/messages/{mid}/answer", json={"answers": {"q1": "docs/"}})
    if wrong.status_code != 409 or "no_pending_question" not in wrong.text:
        raise AssertionError(
            f"群聊审批卡走答题口应该 409 no_pending_question，实际 {wrong.status_code}: {wrong.text[:160]}"
        )
    state = _team_state(d, gid)
    users = [
        row for row in state.get("messages") or []
        if row.get("role") == "user" and PROBE_APPROVAL in str(row.get("content") or "")
    ]
    if not users:
        raise AssertionError("群状态里找不到本轮那条用户消息")
    not_waiting = d.c.post(f"/team/messages/{int(users[-1]['id'])}/approval", json={"approve": True})
    if not_waiting.status_code != 409 or "team_message_not_waiting" not in not_waiting.text:
        raise AssertionError(
            f"给非等待中的群消息求批准应该 409 team_message_not_waiting，"
            f"实际 {not_waiting.status_code}: {not_waiting.text[:160]}"
        )
    return "两码各归各位（no_pending_question / team_message_not_waiting）"


def _assert_team_admin_approval(d: Drive, gid: str) -> str:
    """主理人批准后要真写出文件，并且队列继续走到成员——成员那张卡是新的一张。"""
    mid = int(d.state.get("team_admin_mid") or 0)
    task = str(d.state.get("team_admin_task") or "")
    d.expect(d.c.post(
        f"/team/messages/{mid}/approval", json={"approve": True, "scope": "once"}, timeout=300.0,
    ), (202,))
    deadline = time.time() + TEAM_APPROVAL_TIMEOUT
    last: dict = {}
    member: dict | None = None
    while time.time() < deadline:
        last = _team_state(d, gid)
        rows = [row for row in _team_pending(last) if int(row.get("message_id") or 0) != mid]
        if rows:
            member = rows[0]
            break
        if str((last.get("round") or {}).get("status") or "").lower() in {
            "completed", "failed", "cancelled", "stopped"
        }:
            break
        time.sleep(2)
    target = WS / PROBE_PATH
    if not target.exists():
        raise AssertionError(f"主理人批准后工作区里没有文件：{target}")
    if "SyntheticNotARealKey0001" not in target.read_text(encoding="utf-8"):
        raise AssertionError("主理人批准后写出的文件内容不对")
    evidence = d.expect(d.c.get(f"/tasks/{task}/evidence"), (200,))
    types = {row.get("type") for row in evidence.get("timeline") or []}
    for needed in ("approval.required", "approval.granted"):
        if needed not in types:
            raise AssertionError(
                f"主理人的审批轨迹缺了 {needed}（现有：{sorted(t for t in types if t.startswith('approval'))}）"
            )
    files = (evidence.get("changes") or {}).get("files") or []
    if PROBE_PATH not in files:
        raise AssertionError(f"主理人批准后补丁没落进变更账：{files}")
    if member is None:
        raise AssertionError(
            f"主理人批准后队列没走到成员（轮状态={(last.get('round') or {}).get('status')}）"
        )
    card = _team_card(member)
    if member.get("bot_name") != "小查":
        raise AssertionError(f"第二张卡应当来自被点名的成员，实际 {member.get('bot_name')}")
    d.state["team_member_mid"] = int(member["message_id"])
    d.state["team_member_task"] = str(member.get("task_id") or "")
    return (f"主理人批准后文件写出；队列继续到成员「{member.get('bot_name')}」并挂卡，"
            f"工具={card.get('tool')}")


def _assert_team_member_denied(d: Drive, gid: str) -> str:
    """成员这张卡驳回后：不执行、留痕、文件不变、整轮还能收尾。

    最后一条最容易漏：驳回在单任务里是 paused，在群聊里若只看消息状态，
    很容易漏掉 `round.status` 还卡在 running——界面就会永远转圈。
    """
    mid = int(d.state.get("team_member_mid") or 0)
    task = str(d.state.get("team_member_task") or "")
    target = WS / PROBE_PATH
    before = target.read_bytes() if target.exists() else b""
    d.expect(d.c.post(
        f"/team/messages/{mid}/approval", json={"approve": False, "scope": "once"}, timeout=300.0,
    ), (202,))
    deadline = time.time() + TEAM_APPROVAL_TIMEOUT
    last: dict = {}
    message: dict = {}
    while time.time() < deadline:
        last = _team_state(d, gid)
        message = _team_message(last, mid)
        round_status = str((last.get("round") or {}).get("status") or "").lower()
        if message.get("status") != "waiting_approval" and round_status in {
            "completed", "failed", "cancelled", "stopped"
        }:
            break
        time.sleep(2)
    if message.get("status") != "paused":
        raise AssertionError(f"成员驳回后消息状态应当 paused，实际 {message.get('status')}")
    round_status = str((last.get("round") or {}).get("status") or "")
    if round_status != "completed":
        raise AssertionError(f"成员驳回后整轮没有收尾：round.status={round_status}")
    after = target.read_bytes() if target.exists() else b""
    if after != before:
        raise AssertionError("驳回了却还是改动了工作区文件")
    evidence = d.expect(d.c.get(f"/tasks/{task}/evidence"), (200,))
    types = {row.get("type") for row in evidence.get("timeline") or []}
    if "approval.denied" not in types:
        raise AssertionError(
            f"成员驳回没有留痕（现有：{sorted(t for t in types if t.startswith('approval'))}）"
        )
    count = (evidence.get("changes") or {}).get("count")
    if count:
        raise AssertionError(f"成员驳回却落盘了：{(evidence.get('changes') or {}).get('files')}")
    return f"成员消息 paused、round completed、文件未变（{len(after)} 字节）、驳回留痕"


def _assert_ledger_survives(d: Drive) -> str:
    """#48 那条果实必须在第二种方言上真的成立：删供应商不许把账本一起带走。"""
    before = d.expect(d.c.get("/usage/summary"), (200,))
    provider = d.state.get("provider", "")
    d.expect(d.c.delete(f"/providers/{provider}"), (204,))
    after = d.expect(d.c.get("/usage/summary"), (200,))
    if json.dumps(after, ensure_ascii=False) == "{}" or not json.dumps(after):
        raise AssertionError("删供应商后用量汇总整个空了")
    total_before = _sum_tokens(before)
    total_after = _sum_tokens(after)
    if total_after < total_before:
        raise AssertionError(f"账本被抹掉了：tokens {total_before} -> {total_after}")
    d.state["provider"] = ""
    return f"tokens {total_before} -> {total_after}（必须不减）"


# --------------------------------------------------------------------------- #
# 审批 / 答题那两格（#52）
# --------------------------------------------------------------------------- #
# 为什么要单独一格：`/tasks/{id}/approval` 与 `/tasks/{id}/answer` 是这套系统里唯一
# "由人写回图"的两个接口，它们背后是同一条 LangGraph 中断——中断值要落进检查点（MySQL
# 那边是 BLOB + aiomysql 转义那条老路），恢复又要重放整节。只读取证的驱动永远碰不到它，
# 于是"换库还能不能审批"这句话在过去只是我说的，没有实物。


def _probe_task(d: Drive, pid: str, key: str, title: str, goal: str) -> str:
    task = d.expect(d.c.post(
        f"/projects/{pid}/tasks",
        json={"title": title, "goal": goal, "provider_id": d.state.get("provider")},
    ), (201,))["id"]
    d.state[key] = task
    return task


def _pending(d: Drive, task: str) -> dict:
    evidence = d.expect(d.c.get(f"/tasks/{task}/evidence"), (200,))
    if not isinstance(evidence, dict):
        raise AssertionError("证据包不是对象")
    return evidence


def _ttl_minutes(expires_at: str) -> float:
    """剩余 TTL（分钟）。这一步要量的正是"这个时间戳穿过库之后还算不算数"——
    方言之差最容易在 DATETIME 的精度/时区上露头：读侧说没过期、写侧 409，界面就废了。"""
    from datetime import datetime, timedelta, timezone

    expires = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    if expires.tzinfo is None:  # MySQL 的 DATETIME 不带时区；按写侧口径当成 UTC
        expires = expires.replace(tzinfo=timezone.utc)
    return (expires - datetime.now(timezone.utc)) / timedelta(minutes=1)


# 批准后要等到的那组终态。`paused` 不在里面是故意的：批准那条路停在 paused
# 就等于没放行，等着它只会白等满 180 秒再红在装置自己的等待上。
RESUME_TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


def _resume_with_guard(d: Drive, path: str, payload: dict,
                       terminal: frozenset[str] = RESUME_TERMINAL,
                       *, wait_seconds: float = 180.0, poll_seconds: float = 2.0) -> tuple[str, int]:
    """把决定写回图，等到终态；返回 (终态, 脚手架抖动次数)。

    恢复是同步跑完的（接口里 await AgentRuntime.resume），所以这一发 HTTP 自己就可能
    撞上桩的抖动。抖了不能换新任务——决定已经记在原来那个任务上；照同一个接口重打，
    若第二次说"不在待审批状态"，那恰恰说明第一次已经放行，按事实接受。

    `terminal` 必须能带上 `paused`：驳回那条路是**当场停轮**（graph 里 `if denied: status="paused"`，
    模型不会再拿到那次被拒的工具结果），不是跑到 succeeded。把它当非终态等下去，
    这一步只会白等 180 秒再红在装置自己的等待上。
    """
    retries = 0
    while True:
        resp = d.c.post(path, json=payload, timeout=300.0)
        if _is_transport_flake(resp) and retries < 2:
            retries += 1
            time.sleep(1.0)
            continue
        if resp.status_code == 409 and "task_not_waiting" in resp.text:
            break  # 前一次抖在响应回来的路上，图其实已经恢复了
        d.expect(resp, (200,))
        break
    task_id = path.split("/")[2]
    deadline = time.time() + wait_seconds
    status = ""
    while time.time() < deadline:
        status = d.expect(d.c.get(f"/tasks/{task_id}"), (200,)).get("status", "")
        if status in terminal:
            break
        time.sleep(poll_seconds)
    if status not in terminal:
        raise AssertionError(
            f"放行/作答后没跑到终态 {sorted(terminal)}（status={status}）{_why(d, task_id)}")
    return status, retries


def _assert_approval_card(d: Drive, pid: str) -> str:
    task = _probe_task(d, pid, "approval_task", "写一份带合成密钥的资料",
                       f"往工作区写一个配置片段（{PROBE_APPROVAL}）")
    detail = run_task_with_flake_guard(
        d, pid, task_key="approval_task", title="写一份带合成密钥的资料",
        goal=f"往工作区写一个配置片段（{PROBE_APPROVAL}）",
        wanted=frozenset({"waiting_approval"}))
    pending = _pending(d, task).get("pending_approvals") or []
    if len(pending) != 1:
        raise AssertionError(f"应该恰好挂着一张审批卡，实际 {len(pending)} 张{_why(d, task)}")
    card = pending[0]
    if card.get("tool") != "apply_patch":
        raise AssertionError(f"审批卡指向错了工具：{card.get('tool')}")
    return f"{detail}，卡片工具={card.get('tool')} 风险={card.get('risk_level')}"


def _assert_card_shape(d: Drive, task: str) -> str:
    card = (_pending(d, task).get("pending_approvals") or [{}])[0]
    arguments = card.get("arguments")
    if not isinstance(arguments, dict) or "patch" not in arguments:
        raise AssertionError(f"审批卡没带得出补丁参数：{arguments}")
    if card.get("risk_level") not in ("high", "critical"):
        raise AssertionError(f"高风险写入的卡片风险等级不对：{card.get('risk_level')}")
    if card.get("expired"):
        raise AssertionError("刚挂起的卡就被判成过期了（读写两侧口径不一致）")
    left = _ttl_minutes(str(card.get("expires_at") or ""))
    if not 4.0 < left <= 5.5:
        raise AssertionError(f"审批 TTL 穿过库之后剩 {left:.2f} 分钟，应在 5 分钟上下")
    status = d.expect(d.c.get(f"/tasks/{task}"), (200,)).get("status")
    if status != "waiting_approval":
        raise AssertionError(f"挂着卡但任务状态不是 waiting_approval：{status}")
    return (f"风险={card.get('risk_level')} 状态={card.get('approval_state')} "
            f"规则={card.get('permission_rule')} 剩余 TTL={left:.1f} 分钟")


def _assert_wrong_channel(d: Drive, task: str) -> str:
    """审批卡用答题口回答、草稿任务求审批：两次都必须是 409，且错误码分得开。

    这一条守的是"界面不会给出一个必然失败的按钮"——同一套 409 如果被写成一种，
    前端就只能显示"操作失败"。
    """
    wrong = d.c.post(f"/tasks/{task}/answer", json={"answers": {"q1": "docs/"}})
    if wrong.status_code != 409 or "no_pending_question" not in wrong.text:
        raise AssertionError(f"审批卡走答题口应该 409 no_pending_question，实际 {wrong.status_code}: {wrong.text[:160]}")
    draft = d.state.get("draft_task", "")
    not_waiting = d.c.post(f"/tasks/{draft}/approval", json={"approve": True})
    if not_waiting.status_code != 409 or "task_not_waiting_approval" not in not_waiting.text:
        raise AssertionError(f"给非待审批任务求批准应该 409 task_not_waiting_approval，"
                             f"实际 {not_waiting.status_code}: {not_waiting.text[:160]}")
    return "两码各归各位（no_pending_question / task_not_waiting_approval）"


def _approve_and_finish(d: Drive) -> str:
    task = d.state.get("approval_task", "")
    status, retries = _resume_with_guard(d, f"/tasks/{task}/approval", {"approve": True, "scope": "once"})
    if status != "succeeded":
        raise AssertionError(f"批准后没跑到 succeeded：{status}{_why(d, task)}")
    evidence = _pending(d, task)
    files = (evidence.get("changes") or {}).get("files") or []
    if PROBE_PATH not in files:
        raise AssertionError(f"批准后补丁没落进变更账：{files}")
    written = (WS / PROBE_PATH).read_text(encoding="utf-8")
    if "SyntheticNotARealKey0001" not in written:
        raise AssertionError("批准写入的文件内容不对（工作区里没落上补丁）")
    types = {row.get("type") for row in evidence.get("timeline") or []}
    for needed in ("approval.required", "approval.granted", "approval.decided"):
        if needed not in types:
            raise AssertionError(f"审批轨迹缺了 {needed}（现有：{sorted(t for t in types if t.startswith('approval'))}）")
    detail = f"文件已写出，审批轨迹 {sorted(t for t in types if t.startswith('approval'))}"
    return f"{detail}（脚手架抖动重试 {retries} 次）" if retries else detail


def _assert_diff_redacted(d: Drive) -> str:
    """产品口径"明文密钥只留在用户批准的文件里"必须能在第二种方言上核对。

    断言分两头：库里那份 diff 不能有密钥、工作区那份文件必须有——只查一头的话，
    "两头都抹掉"这种假绿也能过关。
    """
    task = d.state.get("approval_task", "")
    evidence = _pending(d, task)
    diff = (evidence.get("changes") or {}).get("diff") or ""
    if not diff:
        raise AssertionError("变更账里没有 diff")
    secret = "SyntheticNotARealKey0001"
    blob = json.dumps(evidence, ensure_ascii=False)
    if secret in diff:
        raise AssertionError("库里的 FileChange.diff 还带着明文密钥")
    if secret in blob:
        raise AssertionError(f"证据包某处还带着明文密钥（diff 脱敏了但 {sorted({row.get('type') for row in evidence.get('timeline') or [] if secret in json.dumps(row, ensure_ascii=False)})} 没脱敏）")
    if "****" not in diff:
        raise AssertionError(f"diff 里看不到脱敏标记，可能是整行被抹了：{diff[:160]}")
    if secret not in (WS / PROBE_PATH).read_text(encoding="utf-8"):
        raise AssertionError("工作区里那份文件反而没了密钥——脱敏脱到了用户的东西上")
    return f"库里 {diff.count('****')} 处打码，工作区原件保留密钥（{len(diff)} 字 diff）"


def _assert_denied(d: Drive, pid: str) -> str:
    """驳回必须真的没执行：变更账为空、审批轨迹里有 approval.denied，且这一轮当场停住。

    `paused` 是被测产品自己的口径（graph 里 `if denied: status="paused"` + `task.paused` 事件）：
    驳回不是失败，也不再给模型一次"换个写法照样写"的机会。这里把它钉成断言，
    哪天这条规则悄悄改成"继续跑"，这一格会红，而不是让界面继续按 paused 渲染。
    """
    goal = f"往工作区写一份配置（{PROBE_APPROVAL}）"
    task = _probe_task(d, pid, "denied_task", "驳回这份写入", goal)
    run_task_with_flake_guard(d, pid, task_key="denied_task", title="驳回这份写入",
                              goal=goal, wanted=frozenset({"waiting_approval"}))
    status, _ = _resume_with_guard(d, f"/tasks/{task}/approval", {"approve": False, "scope": "once"},
                                   terminal=frozenset({"succeeded", "failed", "cancelled", "paused"}))
    if status != "paused":
        raise AssertionError(f"驳回后的停轮口径变了：期望 paused，实际 {status}")
    evidence = _pending(d, task)
    count = (evidence.get("changes") or {}).get("count")
    if count:
        raise AssertionError(f"驳回了却还是落盘了：{count} 处变更 {(evidence.get('changes') or {}).get('files')}")
    types = {row.get("type") for row in evidence.get("timeline") or []}
    if "approval.denied" not in types:
        raise AssertionError(f"驳回没有留下审计：{sorted(t for t in types if t.startswith('approval'))}")
    if "approval.granted" in types:
        raise AssertionError("驳回的审批卡上还挂着一条 granted，审计自相矛盾")
    if "task.paused" not in types:
        raise AssertionError(f"停在 paused 却没发 task.paused，前端只会看到一个没有理由的停轮：{sorted(types)[:8]}")
    return f"status={status}，变更 0 处，approval.denied + task.paused 在册"


def _assert_question_card(d: Drive, pid: str) -> str:
    goal = f"我不确定该放哪，先问我（{PROBE_QUESTION}）"
    task = _probe_task(d, pid, "question_task", "先问再写", goal)
    detail = run_task_with_flake_guard(d, pid, task_key="question_task", title="先问再写",
                                       goal=goal, wanted=frozenset({"waiting_approval"}))
    evidence = _pending(d, task)
    questions = evidence.get("pending_questions") or []
    if len(questions) != 1:
        raise AssertionError(f"应该挂着一张问题卡，实际 {len(questions)} 张")
    if evidence.get("pending_approvals"):
        raise AssertionError("问题卡被同时报成待审批——两张卡共用中断通道，混了就点不动")
    if evidence.get("question_expired"):
        raise AssertionError("刚挂起的问题被判成已过期")
    if not str(questions[0].get("id") or ""):
        raise AssertionError(f"问题卡没有 id，前端答不回去：{questions[0]}")
    left = _ttl_minutes(str(evidence.get("question_expires_at") or ""))
    if not 28.0 < left <= 31.0:
        raise AssertionError(f"问题 TTL 穿过库之后剩 {left:.1f} 分钟，应在 30 分钟上下")
    empty = d.c.post(f"/tasks/{task}/answer", json={"answers": {}})
    if empty.status_code != 422 or "answer_empty" not in empty.text:
        raise AssertionError(f"空答案应该 422 answer_empty，实际 {empty.status_code}: {empty.text[:160]}")
    d.state["question_id"] = str(questions[0]["id"])
    return f"{detail}，问题=「{questions[0].get('question', '')[:24]}」TTL 剩 {left:.1f} 分钟"


def _answer_and_finish(d: Drive) -> str:
    task = d.state.get("question_task", "")
    qid = d.state.get("question_id", "")
    status, retries = _resume_with_guard(d, f"/tasks/{task}/answer",
                                         {"answers": {qid: "docs/"}, "answer": ""})
    if status != "succeeded":
        raise AssertionError(f"作答后没跑到 succeeded：{status}{_why(d, task)}")
    evidence = _pending(d, task)
    types = {row.get("type") for row in evidence.get("timeline") or []}
    for needed in ("question.required", "question.asked", "question.answered", "question.decided"):
        if needed not in types:
            raise AssertionError(f"问答轨迹缺了 {needed}（现有：{sorted(t for t in types if t.startswith('question'))}）")
    rendered = json.dumps([row for row in evidence.get("timeline") or []
                           if row.get("type") in {"tool.completed", "tool.executed"}], ensure_ascii=False)
    if "【用户回答】" not in rendered or "docs/" not in rendered:
        raise AssertionError(f"答案没有回到模型上下文：{rendered[:200]}{_why(d, task)}")
    detail = f"答案进了上下文，轨迹 {sorted(t for t in types if t.startswith('question'))}"
    return f"{detail}（脚手架抖动重试 {retries} 次）" if retries else detail


def _assert_stream_fallback(d: Drive, pid: str) -> str:
    """流式一帧都没解析出来时要退到整包调用把活干完，而不是静默变成"模型什么都没说"。

    这一格的桩只发 SSE 心跳注释行（见 `_empty_stream_probe`），形状就是"网关不认 stream"。
    报文体带 Content-Length、连接由服务端优雅收尾，所以这一格红只可能是产品侧的：
    过去空流只在桩自己抖的时候偶然出现，红了分不清是被测产品还是脚手架——#52 就是
    在这样的偶然里才发现流式退路读的是另一套工具调用信封。
    """
    goal = f"{PROBE_STREAM_FLAKE} 列出工作区文件并一句话结论"
    _probe_task(d, pid, "flake_task", "流式退路探针", goal)
    detail = run_task_with_flake_guard(d, pid, task_key="flake_task", goal=goal, title="流式退路探针")
    task = d.state.get("flake_task", "")
    timeline = _pending(d, task).get("timeline") or []
    fallbacks = [row for row in timeline if row.get("type") == "model.stream_fallback"]
    if len(fallbacks) != 1:
        # 恰好一次：第一次流式空、退路把活干完，之后的调用要恢复正常流式。
        # 多于一次说明探针把不该打断的调用也打断了（装置自己漏了），少于一次说明退路没走。
        raise AssertionError(f"退路应恰好走 1 次，实际 {len(fallbacks)} 次{_why(d, task)}")
    streamed = [row for row in timeline if row.get("type") == "model.streamed"]
    if not streamed:
        raise AssertionError(f"退路之后再也没有一次正常流式——那一次失败把开关整个关掉了{_why(d, task)}")
    codes = sorted({str((row.get("payload") or {}).get("code")) for row in fallbacks})
    return f"{detail}｜退路 {len(fallbacks)} 次 code={codes}｜之后正常流式 {len(streamed)} 次"


# --------------------------------------------------------------------------- #
# #53：鉴权那一形态，与退路的另两种形状
# --------------------------------------------------------------------------- #

PUBLIC_ROUTE = "/api/v1/health"
DOC_ROUTES = ("/openapi.json", "/docs", "/redoc")
HTTP_METHODS = ("get", "post", "put", "patch", "delete")


def _concrete(path: str) -> str:
    while "{" in path:
        start = path.index("{")
        end = path.index("}", start)
        path = path[:start] + "probe-id" + path[end + 1 :]
    return path


def _enumerated(d: Drive) -> list[tuple[str, str]]:
    """从接口面枚举 (method, path)，空清单一律判装置失效。

    以前这里踩过坑：FastAPI 0.143 的 `app.routes` 只暴露一个 `_IncludedRouter`
    （path 为 None），照路由表结构扫描会枚举出 0 条，于是"0 条漏网"看起来像全绿。
    """
    spec = d.expect(d.root.get("/openapi.json"), (200,))
    ops = [(method, path)
           for path, item in (spec.get("paths") or {}).items()
           for method in HTTP_METHODS if method in item]
    if not ops:
        raise AssertionError("接口面里一条 operation 都没有，枚举本身失效")
    stray = {method for item in (spec.get("paths") or {}).values() for method in item
             if method not in HTTP_METHODS}
    if stray:
        raise AssertionError(f"枚举漏掉了这些方法：{sorted(stray)}")
    return ops


def _assert_anonymous_denies_every_route(d: Drive) -> str:
    operations = _enumerated(d)
    holes = []
    for method, path in operations:
        if path == PUBLIC_ROUTE:
            continue
        resp = d.anon.request(method, _concrete(path))
        if resp.status_code != 401:
            holes.append(f"{method.upper()} {path} -> {resp.status_code}")
    if holes:
        raise AssertionError(f"{len(holes)}/{len(operations)} 条匿名可达：{holes[:8]}")
    if d.anon.get(PUBLIC_ROUTE).status_code != 200:
        raise AssertionError("健康检查应当匿名可读（部署探针要用）")
    return f"匿名全量 {len(operations) - 1} 条都 401，公共面只有 {PUBLIC_ROUTE}"


def _assert_doc_surface_guarded(d: Drive) -> str:
    anonymous = {path: d.anon.get(path).status_code for path in DOC_ROUTES}
    if anonymous != {path: 401 for path in DOC_ROUTES}:
        raise AssertionError(f"接口面/文档匿名可读：{anonymous}")
    readable = {path: d.root.get(path).status_code for path in DOC_ROUTES}
    if readable != {path: 200 for path in DOC_ROUTES}:
        raise AssertionError(f"带令牌却读不到文档：{readable}")
    return f"匿名 {anonymous} / 带令牌 {readable}"


def _assert_token_admits_every_route(d: Drive) -> str:
    """反向那一半：鉴权不能把合法调用也挡掉。

    只测"匿名被拒"会漏掉另一种坏法——某个路由挂错了依赖，带令牌也 401，
    产品就变成"只有自己人能用"。这里逐条看有没有 401/403。
    """
    refused = []
    for method, path in _enumerated(d):
        resp = d.root.request(method, _concrete(path))
        if resp.status_code in {401, 403}:
            refused.append(f"{method.upper()} {path} -> {resp.status_code}")
    if refused:
        raise AssertionError(f"带令牌仍被鉴权挡住：{refused[:8]}")
    wrong = d.anon.get("/api/v1/projects", headers={"Authorization": "Bearer wrong-token"})
    if wrong.status_code != 401:
        raise AssertionError(f"错令牌应当 401，实际 {wrong.status_code}")
    return "带令牌 0 条被鉴权挡住；错令牌 401"


def _probe_env(d: Drive, key: str, *, context_window: int = 8000) -> tuple[str, str]:
    """前面 63/68 已经删掉了供应商与项目，探针自带一套全新的依赖。"""
    project = d.expect(d.c.post("/projects", json={"name": f"e2e-fallback-{key}"}), (201,))["id"]
    d.expect(d.c.post(f"/projects/{project}/workspaces", json={"path": str(WS), "label": "ws"}), (200, 201))
    provider = d.expect(d.c.post(
        "/providers",
        json={"name": f"stub-{key}", "kind": "openai_compatible",
              "base_url": f"http://127.0.0.1:{STUB_PORT}/v1",
              "api_key": "test-key", "model": "stub-model", "supports_tools": True,
              "max_tokens": 512, "context_window": context_window},
    ), (201,))["id"]
    d.state["provider"] = provider
    return project, provider


def _events(d: Drive, task: str, kind: str) -> list[dict]:
    timeline = _pending(d, task).get("timeline") or []
    return [row for row in timeline if row.get("type") == kind]


def _run_until_provider_error(d: Drive, task: str, want_code: str) -> tuple[dict, str]:
    """同步运行入口要把供应商的失败如实念成 502 + 原因码，而不是"跑完了但没答案"。

    这里不走 `run_task_with_flake_guard`：那条守卫是给"脚手架在抖"准备的，
    而这一格的 502 正是被测物本身——放过它就等于把判据改成"随便怎么红都行"。
    """
    resp = d.c.post(f"/tasks/{task}/run", json={}, timeout=300.0)
    if resp.status_code < 500:
        raise AssertionError(f"供应商失败应当以 5xx 念出来，实际 HTTP {resp.status_code}：{resp.text[:200]}")
    try:
        error = (resp.json() or {}).get("error") or {}
    except ValueError:
        raise AssertionError(f"5xx 回执不是可核对的错误体：{resp.text[:200]}")
    if error.get("code") != want_code:
        raise AssertionError(f"5xx 的原因码是 {error.get('code')}，应当是 {want_code}：{error}")
    deadline = time.time() + 60
    status = ""
    while time.time() < deadline:
        status = d.expect(d.c.get(f"/tasks/{task}"), (200,)).get("status", "")
        if status in {"succeeded", "failed", "cancelled"}:
            break
        time.sleep(1)
    return error, status


def _assert_stream_400_recovers(d: Drive) -> str:
    """网关明说"不接受 stream"时，退路要把活干完——这一格的终局必须是成功。

    与步骤 62（空流）分开跑是刻意的：空流靠"一行都解析不出来"才发现，
    400 是供应商直接告诉你它不认，两条路的错误码与可读出的原因都不同。
    退路次数不钉死：一个任务里有几个模型节点就会退几次，钉死数字等于把图的结构
    写进判据；钉的是"每一次退都带着 400"与"全程一次都没真的流起来"。
    """
    project, _ = _probe_env(d, "400")
    goal = f"{PROBE_STREAM_400} 列出工作区文件并一句话结论"
    task = _probe_task(d, project, "http400_task", "流式 400 退路探针", goal)
    detail = run_task_with_flake_guard(d, project, task_key="http400_task", goal=goal, title="流式 400 退路探针")
    fallbacks = _events(d, task, "model.stream_fallback")
    skipped = _events(d, task, "model.stream_fallback_skipped")
    if not fallbacks:
        raise AssertionError(f"整包调用一次都没退回过，任务却成功了——那答案不是模型给的{_why(d, task)}")
    bad = [row.get("payload") for row in fallbacks
           if (row.get("payload") or {}).get("code") != "model_http_error"
           or (row.get("payload") or {}).get("status") != 400]
    if bad:
        raise AssertionError(f"退路记的不是供应商 400：{bad[:2]}{_why(d, task)}")
    if skipped:
        raise AssertionError(f"400 是「问法被拒」，正是该退的形状，不该记成有意不退：{len(skipped)} 次{_why(d, task)}")
    tools = [row for row in _pending(d, task).get("tools") or [] if row.get("status") == "ok"]
    if not tools:
        raise AssertionError(f"退路走完了却没执行任何工具，等于白退一次{_why(d, task)}")
    return (f"{detail}｜退路 {len(fallbacks)} 次都带 400｜跳过 {len(skipped)} 次"
            f"｜工具成功 {len(tools)} 个｜正常流式 {len(_events(d, task, 'model.streamed'))} 次")


def _assert_rate_limit_fails_loudly(d: Drive) -> str:
    """429 要说"现在别问"：换整包再问一遍不改答案，只会让请求翻倍。

    这一格守三件事：终态红得有名有姓（原因里读得出 429）、退路一次都没走、
    以及"有意不退"这件事本身在证据包里留了行——没有那一行，"没退"与"没实现"读起来一样。
    """
    project, _ = _probe_env(d, "429")
    goal = f"{PROBE_RATE_LIMIT} 列出工作区文件并一句话结论"
    task = _probe_task(d, project, "ratelimit_task", "限流探针", goal)
    error, status = _run_until_provider_error(d, task, "model_http_error")
    body = d.expect(d.c.get(f"/tasks/{task}"), (200,))
    reason = str(body.get("failure_reason") or error.get("message") or "")
    if "429" not in reason:
        raise AssertionError(f"终态原因里读不出供应商的 429：{reason[:200]}{_why(d, task)}")
    fallbacks = _events(d, task, "model.stream_fallback")
    skipped = _events(d, task, "model.stream_fallback_skipped")
    if fallbacks:
        raise AssertionError(f"限流被退路重放了一次（请求翻倍、用户多等一轮）：{len(fallbacks)} 次{_why(d, task)}")
    if not skipped:
        raise AssertionError(f"一次都没记「有意不退」，读不出这是决定还是没实现{_why(d, task)}")
    return (f"status={status}｜HTTP 502 code={error.get('code')}｜不退路 {len(fallbacks)} 次"
            f"｜跳过记录 {len(skipped)} 次｜原因={reason[:60]}")


def _assert_model_timeout(d: Drive) -> str:
    """模型超时：不重放、不静默，一轮超时就要把「超时、可重试」交回调用方。

    两件事一起守：`BLANK_MODEL_TIMEOUT_SECONDS` 真的接到了适配器（不生效时桩睡 12 秒
    也读不出超时，这一格会一直绿），以及超时不再被退路放大成"等两轮"——
    耗时上界因此是可判的：一轮超时 + 收尾，而不是两倍超时。
    """
    project, _ = _probe_env(d, "slow")
    goal = f"{PROBE_SLOW} 列出工作区文件并一句话结论"
    task = _probe_task(d, project, "slow_task", "超时探针", goal)
    started = time.time()
    error, status = _run_until_provider_error(d, task, "model_timeout")
    elapsed = round(time.time() - started, 1)
    limit = float(DEVICE_MODEL_TIMEOUT) * 1.5
    if elapsed > limit:
        raise AssertionError(f"超时 {DEVICE_MODEL_TIMEOUT}s 却等了 {elapsed}s——退路把等待翻倍了")
    body = d.expect(d.c.get(f"/tasks/{task}"), (200,))
    reason = str(body.get("failure_reason") or error.get("message") or "")
    if "超时" not in reason:
        raise AssertionError(f"终态原因里读不出超时：{reason[:200]}{_why(d, task)}")
    fallbacks = _events(d, task, "model.stream_fallback")
    skipped = _events(d, task, "model.stream_fallback_skipped")
    if fallbacks or not skipped:
        raise AssertionError(f"超时不该重放、且要留下「有意不退」的记录：退路 {len(fallbacks)} 次/跳过 {len(skipped)} 次")
    return f"status={status}｜耗时 {elapsed}s（上界 {limit}s）｜原因={reason[:50]}｜跳过记录 {len(skipped)} 次"


def _assert_rate_limit_retry_recovers(d: Drive) -> str:
    """429 第一次出现时退避再问，第二次答上了，这一轮就该把活干完（#56）。

    与步骤 77 是一对：77 守"问几次都说不行时，失败要红得有名有姓"，81 守
    "只拒一次时，不许把用户的一轮白扔"。两个数必须一起钉：
    重试事件恰好一条（多一条说明它在对同一个 429 连续重试超过一次），
    退路 0 次（走了整包说明 429 被当成问法问题重放，正是 #53 拒绝的形状）。
    """
    project, _ = _probe_env(d, "429retry")
    goal = f"{PROBE_RATE_LIMIT_RETRY} 列出工作区文件并一句话结论"
    task = _probe_task(d, project, "retry429_task", "限流退避探针", goal)
    detail = run_task_with_flake_guard(d, project, task_key="retry429_task", goal=goal, title="限流退避探针")
    retries = _events(d, task, "model.rate_limit_retry")
    fallbacks = _events(d, task, "model.stream_fallback")
    skipped = _events(d, task, "model.stream_fallback_skipped")
    if len(retries) != 1:
        raise AssertionError(f"桩只拒了第一次，重试记录应当恰好 1 条，实际 {len(retries)} 条{_why(d, task)}")
    payload = retries[0].get("payload") or {}
    want = {"attempt": 1, "status": 429, "wait_ms": 500, "max_retries": 2}
    got = {key: payload.get(key) for key in want}
    if got != want:
        raise AssertionError(f"重试记录不是第一次退避 500ms：{got}，应当 {want}{_why(d, task)}")
    if fallbacks or skipped:
        raise AssertionError(
            f"只拒一次的限流被当成问法问题：退路 {len(fallbacks)} 次/有意不退 {len(skipped)} 次{_why(d, task)}")
    rejects = _Handler.probe_rejects.get(PROBE_RATE_LIMIT_RETRY, 0)
    if rejects != 1:
        raise AssertionError(f"装置自检：桩应只拒第一次，实际拒了 {rejects} 次（探针标记串了？）")
    tools = [row for row in _pending(d, task).get("tools") or [] if row.get("status") == "ok"]
    if not tools:
        raise AssertionError(f"重试后任务成功了，却没有任何工具执行——答案是哪来的{_why(d, task)}")
    return (f"{detail}｜重试 1 次（waited={payload.get('wait_ms')}ms）｜工具成功 {len(tools)} 个"
            f"｜退路 {len(fallbacks)} 次/跳过 {len(skipped)} 次")


def _assert_compaction_fold(d: Drive) -> str:
    """#58：压缩那一笔在两种方言上真发生，且它自己的 429 也听 Retry-After。

    触发条件是算出来的，不是碰上的：窗口 1200 → 水位 984 tokens；两轮各读 6KB 文件，
    第三轮的工具结果把最早那轮挤出尾部预算，`_safe_cut` 才肯把切点落在工具配对之外。
    只读一轮时切点会退回段头，一条都不折——这一格会红成"没有压缩记录"。
    摘要请求第一次被桩拒 429+`Retry-After: 1`，于是同时量到窗口链、替换引用、
    摘要响应 id 与压缩路径的退避等待。
    """
    project, _ = _probe_env(d, "compact", context_window=1200)
    goal = f"{PROBE_COMPACT} 依次读两份资料，最后用一句话结论"
    task = _probe_task(d, project, "compact_task", "压缩探针", goal)
    detail = run_task_with_flake_guard(
        d, project, task_key="compact_task", goal=goal, title="压缩探针")
    compacted = _events(d, task, "context.compacted")
    if len(compacted) < 1:
        raise AssertionError(f"消息顶过窗口水位却没有压缩记录{_why(d, task)}")
    payloads = [row.get("payload") or {} for row in compacted]
    # 一次任务可能在多个节点上各折一次（每次折叠后窗口号才加一），所以不钉次数；
    # 钉的是"每次折叠都必须把边界往前推"——原地重折同一条历史等于窗口在空转。
    boundaries = [item.get("boundary") for item in payloads]
    if any(bound is None for bound in boundaries) or any(
            later <= earlier for earlier, later in zip(boundaries, boundaries[1:])):
        raise AssertionError(
            f"压缩边界没有逐窗前进：{boundaries}"
            f"（相等=把同一条历史折了两次；倒退=covered 被重置）{_why(d, task)}")
    chain = [item.get("window_number") for item in payloads]
    if chain != list(range(1, len(compacted) + 1)):
        raise AssertionError(f"窗口链不是从 1 起逐窗递增：{chain}{_why(d, task)}")
    last = payloads[-1]
    if int(last.get("folded") or 0) < 1:
        raise AssertionError(f"压缩记录说一条都没折叠：{last}{_why(d, task)}")
    if int(last.get("summary_chars") or 0) < 1:
        raise AssertionError(f"折叠发生了却没有摘要正文：{last}{_why(d, task)}")
    rid = str(last.get("compaction_response_id") or "")
    if not rid.startswith("resp-stub-summary-"):
        raise AssertionError(f"压缩记录没指认那一笔摘要响应：{rid!r}{_why(d, task)}")
    replacement = last.get("replacement") or {}
    dropped = replacement.get("dropped") or []
    kept = replacement.get("items") or []
    if not dropped or not kept:
        raise AssertionError(
            f"替换记录不完整：dropped={len(dropped)} items={len(kept)}{_why(d, task)}")
    retries = [row for row in _events(d, task, "model.rate_limit_retry")
               if (row.get("payload") or {}).get("role") == "summarizer"]
    if len(retries) != 1:
        raise AssertionError(f"摘要那笔应当恰好退避一次，实际 {len(retries)} 条{_why(d, task)}")
    retry = retries[0].get("payload") or {}
    want = {"attempt": 1, "status": 429, "wait_ms": 1000,
            "retry_after_ms": 1000, "max_retries": 2}
    got = {key: retry.get(key) for key in want}
    if got != want:
        raise AssertionError(
            f"摘要那笔没听 Retry-After：{got}，应当 {want}{_why(d, task)}")
    rejects = _Handler.probe_rejects.get(PROBE_COMPACT, 0)
    if rejects != 1:
        raise AssertionError(f"装置自检：桩应只拒摘要第一次，实际拒了 {rejects} 次（探针标记串了？）")
    tools = [row for row in _pending(d, task).get("tools") or [] if row.get("status") == "ok"]
    if len(tools) != 2:
        raise AssertionError(
            f"这一格应当恰好两次成功读取，实际 {len(tools)} 次——"
            f"压缩后历史被折叠，按旧条数续抛会在这里露出来{_why(d, task)}")
    return (f"{detail}｜压缩 {len(compacted)} 窗，末窗折叠 {last.get('folded')} 条"
            f"｜替换 dropped={len(dropped)} items={len(kept)}｜摘要 {rid}"
            f"｜摘要退避 waited={retry.get('wait_ms')}ms"
            f"｜成功工具 {len(tools)} 个")


def _assert_retry_after_honored(d: Drive) -> str:
    """供应商明说 `Retry-After: 1` 时，等待就不能还是盲猜的 500ms（#57）。

    与步骤 81 的区别只在"怎么等"：81 的桩不给表头，产品按自己的指数退避；
    82 的桩给了表头，事件里的 waited 必须是 1000ms，且 `retry_after_ms` 留着
    供应商的原值。终态仍要成功、仍不许走退路——听表头不等于换形状。
    """
    project, _ = _probe_env(d, "429after")
    goal = f"{PROBE_RATE_LIMIT_AFTER} 列出工作区文件并一句话结论"
    task = _probe_task(d, project, "retry429after_task", "Retry-After 探针", goal)
    detail = run_task_with_flake_guard(
        d, project, task_key="retry429after_task", goal=goal, title="Retry-After 探针")
    retries = _events(d, task, "model.rate_limit_retry")
    fallbacks = _events(d, task, "model.stream_fallback")
    skipped = _events(d, task, "model.stream_fallback_skipped")
    if len(retries) != 1:
        raise AssertionError(f"桩只拒了第一次，重试记录应当恰好 1 条，实际 {len(retries)} 条{_why(d, task)}")
    payload = retries[0].get("payload") or {}
    want = {"attempt": 1, "status": 429, "wait_ms": 1000, "retry_after_ms": 1000, "max_retries": 2}
    got = {key: payload.get(key) for key in want}
    if got != want:
        raise AssertionError(
            f"等待没听供应商的 Retry-After：{got}，应当 {want}"
            f"（waited=500 说明还在盲猜指数退避）{_why(d, task)}")
    if fallbacks or skipped:
        raise AssertionError(
            f"只拒一次的限流被当成问法问题：退路 {len(fallbacks)} 次/有意不退 {len(skipped)} 次{_why(d, task)}")
    rejects = _Handler.probe_rejects.get(PROBE_RATE_LIMIT_AFTER, 0)
    if rejects != 1:
        raise AssertionError(f"装置自检：桩应只拒第一次，实际拒了 {rejects} 次（探针标记串了？）")
    tools = [row for row in _pending(d, task).get("tools") or [] if row.get("status") == "ok"]
    if not tools:
        raise AssertionError(f"重试后任务成功了，却没有任何工具执行——答案是哪来的{_why(d, task)}")
    return (f"{detail}｜重试 1 次（waited={payload.get('wait_ms')}ms，"
            f"retry_after={payload.get('retry_after_ms')}ms）｜工具成功 {len(tools)} 个"
            f"｜退路 {len(fallbacks)} 次/跳过 {len(skipped)} 次")


def _assert_memo_card(d: Drive) -> str:
    """#54 第一格：解释器命令的「总是允许 X」必须写到正文那一段。

    只拿命令首段的旧口径会给出「本会话总是允许 python」——用户以为批的是一条脚本，
    账本却把整个解释器放手了（实测逃逸见 E:/tmp/m74/probe-out.log）。

    这一格必须先把这个**临时实例**摆进"允许执行命令"的形态：装置默认
    ``shell_enabled=false``，第一次跑时 75/76 都红在 ``capability:shell_disabled``——
    闸门在生成审批卡之前就把工具拒了，测的根本不是批准记忆。联网也要打开，因为
    两条命令都带 ``https://``（run_shell 的 NETWORK 判定），否则同样被能力边界拦下。
    """
    normalized = d.expect(d.c.put(
        "/settings/agent",
        json={"safety": {"shell_enabled": True, "network_enabled": True}},
    ), (200,))
    safety = normalized.get("safety") or {}
    if not (safety.get("shell_enabled") and safety.get("network_enabled")):
        raise AssertionError(f"探针实例没摆成允许执行的形态：{safety}")
    project, _ = _probe_env(d, "code")
    goal = f"{PROBE_CODE_SEQ} 依次跑两段内联脚本"
    task = _probe_task(d, project, "code_task", "内联脚本记忆探针", goal)
    detail = run_task_with_flake_guard(
        d, project, task_key="code_task", goal=goal, title="内联脚本记忆探针",
        wanted=frozenset({"waiting_approval"}))
    card = (_pending(d, task).get("pending_approvals") or [{}])[0]
    if card.get("command_prefix") != "python":
        raise AssertionError(f"命令前缀应为 python，实际 {card.get('command_prefix')}")
    if card.get("suggested_prefix") != "python -c print(1) https://a.example":
        raise AssertionError(
            f"建议前缀没有带到真正被执行的那一段：{card.get('suggested_prefix')!r}"
            "（旧口径停在载荷、把尾巴截掉，会得到 'python -c print(1)'——换了尾巴也免问）")
    options = card.get("approval_options") or []
    scopes = [item.get("scope") for item in options]
    # #82：命令类工具的卡上没有工具档——「总是允许整个 shell」等于把整会话的任意代码执行交出去。
    if scopes != ["once", "prefix"]:
        raise AssertionError(f"解释器命令的作用域档位不对：{scopes}")
    label = next(item.get("label") for item in options if item.get("scope") == "prefix")
    if label != "本会话总是允许 python -c print(1) https://a.example":
        raise AssertionError(f"卡片文案与账本规则不一致：{label!r}")
    return f"{detail}｜前缀={card.get('command_prefix')}｜建议={card.get('suggested_prefix')}｜{label}"


def _assert_memo_granularity(d: Drive) -> str:
    """#54 第二格：记住第一段正文之后，第二段（正文不同）必须重新挂卡。

    这是把「前缀」真正收窄到"这一条命令"的唯一证据：同一任务、同一工具、同一模块，
    唯一变量是内联正文。若账本退回裸 ``python``，第二段会被静默放行，这一格变红。
    """
    task = d.state.get("code_task", "")
    status, _retries = _resume_with_guard(
        d, f"/tasks/{task}/approval", {"approve": True, "scope": "prefix"},
        terminal=frozenset({"waiting_approval"}))
    if status != "waiting_approval":
        raise AssertionError(f"第二段正文不同却没重新挂卡（status={status}）{_why(d, task)}")
    card = (_pending(d, task).get("pending_approvals") or [{}])[0]
    second = 'python -c "print(2)" https://b.example'
    if (card.get("arguments") or {}).get("command") != second:
        raise AssertionError(f"第二张卡指向的命令不对：{(card.get('arguments') or {}).get('command')!r}")
    if card.get("suggested_prefix") != "python -c print(2) https://b.example":
        raise AssertionError(f"第二张卡的建议前缀没跟着正文与尾巴走：{card.get('suggested_prefix')!r}")

    granted = _events(d, task, "approval.granted")
    if len(granted) != 1:
        raise AssertionError(f"应当恰好一条 approval.granted，实际 {len(granted)} 条")
    payload = granted[0].get("payload") or {}
    if payload.get("scope") != "prefix" or payload.get("prefix") != "python -c print(1) https://a.example":
        raise AssertionError(f"批准留痕与账本规则不一致：scope={payload.get('scope')} prefix={payload.get('prefix')!r}")

    status2, retries2 = _resume_with_guard(
        d, f"/tasks/{task}/approval", {"approve": True, "scope": "once"})
    if status2 != "succeeded":
        raise AssertionError(f"第二次批准后没跑到 succeeded：{status2}{_why(d, task)}")
    completed = [row for row in _events(d, task, "tool.completed")
                 if (row.get("payload") or {}).get("tool") == "run_shell"]
    rules = [((row.get("payload") or {}).get("permission") or {}).get("rule") for row in completed]
    if rules != ["action:approved", "action:approved"]:
        raise AssertionError(f"两次执行都不是靠「仅本次」放行：{rules}")
    return (f"第二段重新挂卡｜留痕 prefix={payload.get('prefix')}｜规则={rules}"
            + (f"（脚手架抖动重试 {retries2} 次）" if retries2 else ""))


def _assert_tool_scope_not_recorded(d: Drive) -> str:
    """#82 在真 HTTP 上的那一格：老客户端硬传 `scope=tool`，宽档不许入账。

    缺陷口径下（工具档照旧入账 `(档位, run_shell, "")`）第二段会被静默放行、任务直接
    跑完，这一格红在"没重新挂卡"；留痕那一条也红——批准的人有权知道这条决定其实
    什么都没记下。收尾用"仅本次"把第二段放行，任务必须真跑到 succeeded。
    """
    project, _provider = _probe_env(d, "grant")
    goal = f"{PROBE_TOOL_GRANT} 依次跑两段内联脚本（工具档不入账）"
    task = _probe_task(d, project, "grant_task", "工具档不入账探针", goal)
    detail = run_task_with_flake_guard(
        d, project, task_key="grant_task", goal=goal, title="工具档不入账探针",
        wanted=frozenset({"waiting_approval"}))
    card = (_pending(d, task).get("pending_approvals") or [{}])[0]
    # 批准前先钉住"这张卡指的是哪条命令"：卡是被静默放行后新挂的那一张时，
    # 后面的 scope=tool 批准与留痕断言全在读别人的账。
    if (card.get("arguments") or {}).get("command") != _GRANT_COMMANDS[0]:
        raise AssertionError(
            f"第一张卡指向的不是本探针的第一段命令（前序探针的持久规则泄漏进这一格）："
            f"{(card.get('arguments') or {}).get('command')!r}")
    scopes = [item.get("scope") for item in (card.get("approval_options") or [])]
    if scopes != ["once", "prefix"]:
        raise AssertionError(f"命令类工具的卡上按不下去的钮还在：{scopes}")

    status, retries = _resume_with_guard(
        d, f"/tasks/{task}/approval", {"approve": True, "scope": "tool"},
        terminal=frozenset({"waiting_approval"}))
    if status != "waiting_approval":
        raise AssertionError(f"scope=tool 批准后第二段没重新挂卡（status={status}）{_why(d, task)}")
    second = (_pending(d, task).get("pending_approvals") or [{}])[0]
    expected = _GRANT_COMMANDS[1]
    if (second.get("arguments") or {}).get("command") != expected:
        raise AssertionError(
            f"第二张卡指向的命令不对：{(second.get('arguments') or {}).get('command')!r}")

    granted = _events(d, task, "approval.granted")
    if len(granted) != 1:
        raise AssertionError(f"应当恰好一条 approval.granted，实际 {len(granted)} 条")
    payload = granted[0].get("payload") or {}
    if payload.get("scope") != "tool" or payload.get("tool_grant_recorded") is not False:
        raise AssertionError(
            f"留痕没有如实说出宽档没入账：scope={payload.get('scope')} "
            f"recorded={payload.get('tool_grant_recorded')}"
        )

    snapshots = [
        ((row.get("payload") or {}).get("permissions") or {}).get("approved_tools") or []
        for row in _events(d, task, "turn.context")
    ]
    if not snapshots:
        raise AssertionError("没有权限快照事件，最后一条判定成了空断言")
    bare = [item for group in snapshots for item in group if str(item).endswith(":run_shell:.")]
    if bare:
        raise AssertionError(f"权限快照里出现了裸 run_shell 宽档：{bare}")

    status2, retries2 = _resume_with_guard(
        d, f"/tasks/{task}/approval", {"approve": True, "scope": "once"})
    if status2 != "succeeded":
        raise AssertionError(f"第二段按「仅本次」放行后没跑到 succeeded：{status2}{_why(d, task)}")
    return (f"{detail}｜scope=tool 批准后重新挂卡｜recorded=False｜快照={snapshots[-1]}"
            + (f"（脚手架抖动重试 {retries + retries2} 次）" if retries or retries2 else ""))


def _assert_tail_swap_and_replay(d: Drive) -> str:
    """#87 在真 HTTP 上的那一格：记下的规则**正好**是执行的那一条，两个方向同跑。

    旧口径把解释器规则截成 ``python -c print(87)``（停在载荷），第二段只换了载荷后面的尾巴
    （``sys.argv`` 里那个 URL）就搭上了第一段免问的顺风车 ⇒ 这一格红在"批准后没重新挂卡"。
    只把匹配收紧成等长会得到反方向的缺陷：批准过的那条命令换个任务重跑照样挂卡，
    对用户就是"批了没用"——所以同一格再用 REPLAY 探针把正方向钉一次。
    两处期望（``suggested_prefix`` 与卡片 label）都取自同一串规则，不同源就红。
    """
    project, _provider = _probe_env(d, "tail")
    goal = f"{PROBE_TAIL_SWAP} 依次跑两段尾巴不同的内联脚本"
    task = _probe_task(d, project, "tail_task", "尾巴换参探针", goal)
    detail = run_task_with_flake_guard(
        d, project, task_key="tail_task", goal=goal, title="尾巴换参探针",
        wanted=frozenset({"waiting_approval"}))
    card = (_pending(d, task).get("pending_approvals") or [{}])[0]
    # 批准前先钉住"这张卡指哪条命令"：读到别的探针泄漏进来的持久规则时，后面全在读别人的账。
    if (card.get("arguments") or {}).get("command") != _TAIL_COMMANDS[0]:
        raise AssertionError(
            f"第一张卡指向的不是本探针的第一段命令（前序探针的持久规则泄漏进这一格）："
            f"{(card.get('arguments') or {}).get('command')!r}")
    if card.get("suggested_prefix") != _TAIL_RULE_FIRST:
        raise AssertionError(
            f"规则串没带上真正被执行的那段尾巴：{card.get('suggested_prefix')!r}")
    label = next((item.get("label") for item in (card.get("approval_options") or [])
                  if item.get("scope") == "prefix"), "")
    if label != f"本会话总是允许 {_TAIL_RULE_FIRST}":
        raise AssertionError(f"卡片文案与账本规则不同源：{label!r}")

    status, retries = _resume_with_guard(
        d, f"/tasks/{task}/approval", {"approve": True, "scope": "prefix"},
        terminal=frozenset({"waiting_approval"}))
    if status != "waiting_approval":
        raise AssertionError(f"第二段只换了尾巴就被免问（status={status}）{_why(d, task)}")
    second = (_pending(d, task).get("pending_approvals") or [{}])[0]
    if (second.get("arguments") or {}).get("command") != _TAIL_COMMANDS[1]:
        raise AssertionError(
            f"第二张卡指向的命令不对：{(second.get('arguments') or {}).get('command')!r}")
    if second.get("suggested_prefix") != _TAIL_RULE_SECOND:
        raise AssertionError(f"第二张卡的规则串没跟着自己的尾巴走：{second.get('suggested_prefix')!r}")

    granted = _events(d, task, "approval.granted")
    if len(granted) != 1:
        raise AssertionError(f"应当恰好一条 approval.granted，实际 {len(granted)} 条")
    payload = granted[0].get("payload") or {}
    if payload.get("scope") != "prefix" or payload.get("prefix") != _TAIL_RULE_FIRST:
        raise AssertionError(
            f"批准留痕与账本规则不一致：scope={payload.get('scope')} prefix={payload.get('prefix')!r}")

    status2, retries2 = _resume_with_guard(
        d, f"/tasks/{task}/approval", {"approve": True, "scope": "once"})
    if status2 != "succeeded":
        raise AssertionError(f"第二段按「仅本次」放行后没跑到 succeeded：{status2}{_why(d, task)}")

    # 反方向：批准过的那条命令，换一条全新任务重跑也不许再挂卡。
    replay_goal = f"{PROBE_TAIL_REPLAY} 重跑批准过的那条内联脚本"
    replay_task = _probe_task(d, project, "tail_replay_task", "批准过不再挂卡", replay_goal)
    replay_detail = run_task_with_flake_guard(
        d, project, task_key="tail_replay_task", goal=replay_goal, title="批准过不再挂卡",
        wanted=frozenset({"succeeded"}))
    replay = _pending(d, replay_task)
    if replay.get("pending_approvals"):
        raise AssertionError(
            f"批准过的那条命令换了任务还要重新挂卡——记下的规则比执行的窄，用户会觉得批了没用"
            f"{_why(d, replay_task)}")
    tools = [row for row in _events(d, replay_task, "tool.completed")
             if (row.get("payload") or {}).get("tool") == "run_shell"]
    if len(tools) != 1:
        raise AssertionError(f"重放那一格应当恰好执行一条 run_shell，实际 {len(tools)} 条")
    rules = [((row.get("payload") or {}).get("permission") or {}).get("rule") for row in tools]
    # 重放走的是一条全新任务：会话规则 `approved_prefixes` 每次运行都从请求带进来（见
    # `runtime.py` 的 `approved_prefixes=request.approved_command_prefixes`），新任务天然为空，
    # 所以跨任务重放**只能**命中落盘那一支，念成 `prefix:persistent:`。
    # 这里以前定期望 `prefix:{规则}`（会话支的串）——那是个永远绿不了的死判据：
    # 2026-10-26 姿态修好、这一格第一次被真 HTTP 跑到，红在 `['prefix:persistent:python -c ...']`。
    # 契约以仓库单测 `test_approval_persist_v43.py` 为准（同一支、同一个串）。
    if rules != [f"prefix:persistent:{_TAIL_RULE_FIRST}"]:
        raise AssertionError(f"重放那条不是靠落盘免问规则放行的：{rules}")
    return (f"{detail}｜换尾巴重新挂卡｜留痕 prefix={payload.get('prefix')}｜"
            f"{replay_detail}｜重放免问 rule={rules[0]}"
            + (f"（脚手架抖动重试 {retries + retries2} 次）" if retries or retries2 else ""))


def _sum_tokens(body: object) -> int:
    found = []

    def walk(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key.endswith("tokens") and isinstance(value, (int, float)) and not isinstance(value, bool):
                    found.append(int(value))
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(body)
    return sum(found)


def checkpoint_witness(engine: str, url: str, instance: Path) -> str:
    """实物见证：LangGraph 检查点真的往二进制列里写了东西。

    这一行不是为了断言（步骤 19/20 已经在守），它守的是另一件事：#50 量出的缺陷 C
    （aiomysql 在 PyMySQL ≥1.2 上绑不了 bytes）如果复发，这两个数会当场变成 0，
    而 HTTP 那侧只有"任务红"这一种表现，读不出"是字节没写进去"。
    """
    query = "select count(*), coalesce(sum(length(value)), 0) from agent_checkpoint_writes"
    try:
        if engine == "mysql":
            try:
                import pymysql
            except ImportError as error:
                return f"跳过（MySQL 驱动没装：{error}）"
            _, host, port, user, password, database = _split_mysql_url(url)
            connection = pymysql.connect(host=host, port=port, user=user, password=password,
                                         database=database, connect_timeout=10)
            try:
                with connection.cursor() as cursor:
                    cursor.execute(query)
                    rows, size = cursor.fetchone()
            finally:
                connection.close()
        elif engine == "postgres":
            import asyncio

            try:
                import asyncpg
            except ImportError as error:
                return f"跳过（PostgreSQL 驱动没装：{error}）"
            _, host, port, user, password, database = _split_postgres_url(url)

            async def read():
                connection = await asyncpg.connect(
                    host=host, port=port, user=user, password=password or None,
                    database=database, timeout=10,
                )
                try:
                    return await connection.fetchrow(query)
                finally:
                    await connection.close()

            rows, size = asyncio.run(read())
        else:
            import sqlite3

            with sqlite3.connect((instance / "e2e.db").as_posix()) as connection:
                rows, size = connection.execute(query).fetchone()
        return f"agent_checkpoint_writes 行数={rows} 字节={size}"
    except Exception as error:  # noqa: BLE001 — 见证行读不到不影响判定，如实写出来
        return f"读取失败：{type(error).__name__}: {error}"


CONTROL = "sqlite"  # 对照格：它红了，两方言的对照结论就整条作废


def verdict(totals: dict[str, dict[str, str]], skipped: dict[str, str] | None = None) -> tuple[int, list[str]]:
    """红了该算谁的——判据要求**每一格都绿**才成立。

    写成纯函数是有原因的：本机实测撞过"对照格红、被测格全绿"（`E:/tmp/m54/drive.log`），
    而老代码只在被测格红时置非 0——那种情况下退出码是 0，CI 会绿，
    而实际上这次连对照都没过不去，任何"换库没问题"的结论都不许从这次跑里念出来。
    """
    if CONTROL not in totals:
        # 对照格不在场（今天没法判）时，被测格再绿也支撑不了"换库没问题"这句话：
        # 判据是"每格都绿"，而这一格压根没跑。红在开场，不许退化成 0。
        why = (skipped or {}).get(CONTROL, "这一格没进 totals（没跑、跑一半退出，或开场姿态自检没过）")
        return 2, [f"对照格（{CONTROL}）没跑成：{why}"
                   "——判据要求每一格都在场且全绿，这一格今天缺席"]
    red = {engine: sorted(step for step, status in rows.items() if status == "FAIL")
           for engine, rows in totals.items()}
    red = {engine: steps for engine, steps in red.items() if steps}
    if not red:
        if len(totals) < 2:
            return 0, [f"跑过的格子（{sorted(totals)}）全绿，但只有一格——两方言对照的判据不成立，"
                       "这一份不能当成「换库没问题」的证据"]
        return 0, ["每一格全绿：对照判据成立"]
    lines = [f"红了的是：{red}"]
    control_steps = set(red.get(CONTROL, []))
    labels = {"mysql": "MySQL", "postgres": "PostgreSQL"}
    tested_cells = [engine for engine in red if engine != CONTROL]
    if len(tested_cells) == 1:
        engine = tested_cells[0]
        steps = set(red[engine])
        label = labels.get(engine, engine)
        if steps - control_steps:
            lines.append(f"只有 {label} 红（=真方言分歧候选）：{sorted(steps - control_steps)}")
        if steps & control_steps:
            lines.append(f"两格一起红（=装置错，不是换库问题）：{sorted(steps & control_steps)}")
    elif tested_cells:
        # 三格同跑时两个被测格一起红：再说"只有 MySQL/PostgreSQL 红"就是假话。
        differing = {engine: sorted(set(red[engine]) - control_steps)
                     for engine in tested_cells if set(red[engine]) - control_steps}
        if differing:
            lines.append(f"非对照格与对照不同的步骤（=真方言分歧候选）：{differing}")
        shared = set().union(*(set(red[engine]) for engine in tested_cells)) & control_steps
        if shared:
            lines.append(f"两格一起红（=装置错，不是换库问题）：{sorted(shared)}")
    tested_union = set().union(*(set(red[engine]) for engine in tested_cells)) if tested_cells else set()
    if control_steps - tested_union:
        lines.append(f"只有对照格（{CONTROL}）红（=判据不成立）：{sorted(control_steps - tested_union)}"
                     "——连不做换库的那一格都过不去，红的那一步就不能算到方言头上")
    if len(totals) < 2:
        lines.append("只跑了一格：对照判据不成立，上面这份只是单格清单")
    return 1, lines


def main() -> int:
    parser = argparse.ArgumentParser(description="两方言真 HTTP 端到端对照驱动")
    parser.add_argument("--only", default="sqlite,mysql")
    parser.add_argument("--require-mysql", action="store_true",
                        help="CI 用：没有可连的 MySQL 就判失败（退出码 2），而不是静默跳过")
    parser.add_argument("--require-postgres", action="store_true",
                        help="CI 用：没有可连的 PostgreSQL 就判失败（退出码 2），而不是静默跳过")
    args = parser.parse_args()

    engines = [part.strip() for part in args.only.split(",") if part.strip()]
    mysql_url = os.environ.get("BLANK_E2E_MYSQL_URL", "").strip()
    postgres_url = os.environ.get("BLANK_E2E_POSTGRES_URL", "").strip()
    start_stub()
    totals: dict[str, dict[str, str]] = {}
    skipped: dict[str, str] = {}
    for engine in engines:
        if engine not in {"sqlite", "mysql", "postgres"}:
            print(f"[ABORT] 不认识这一格：{engine}")
            return 2
        if engine == "mysql":
            try:
                _split_mysql_url(mysql_url)
            except MySQLMissing as error:
                if args.require_mysql:
                    print(f"[ABORT] CI 要求 MySQL 这一格必须跑，但起不来：{error}")
                    return 2
                print(f"[SKIP] MySQL 这一格今天没法判：{error}")
                skipped["mysql"] = str(error)
                continue
        if engine == "postgres":
            try:
                _split_postgres_url(postgres_url)
            except PostgresMissing as error:
                if args.require_postgres:
                    print(f"[ABORT] CI 要求 PostgreSQL 这一格必须跑，但起不来：{error}")
                    return 2
                print(f"[SKIP] PostgreSQL 这一格今天没法判：{error}")
                skipped["postgres"] = str(error)
                continue
        try:
            url, instance = prepare(engine, mysql_url, postgres_url)
        except (MySQLMissing, PostgresMissing) as error:
            required = ((engine == "mysql" and args.require_mysql)
                        or (engine == "postgres" and args.require_postgres))
            if required:
                print(f"[ABORT] CI 要求 {engine} 这一格必须跑，但那台库起不来：{error}")
                return 2
            print(f"[SKIP] {engine} 这一格今天没法判：{error}")
            skipped[engine] = str(error)
            continue
        proc = boot(url, instance)
        try:
            d = Drive(f"http://127.0.0.1:{API_PORT}/api/v1")
            try:
                check_posture(d)
            except PostureMissing as error:
                # 前提不成立 ≠ 产品红：这一格整个不算进对照，退出码走「没法判」。
                print(f"[SKIP] {engine} 这一格今天没法判：{error}")
                skipped[engine] = str(error)
                continue
            drive(d)
            d.finish(engine)
            totals[engine] = {name: status for name, status, _ in d.rows}
            text = (instance / "api.log").read_text(encoding="utf-8", errors="replace")
            sqlish = [line for line in text.splitlines()
                      if any(marker in line for marker in ("OperationalError", "ProgrammingError",
                                                           "IntegrityError", "Traceback"))]
            print(f"服务日志里与 SQL 有关的行数={len(sqlish)}；样例：{sqlish[:2]}")
            print(f"检查点见证：{checkpoint_witness(engine, url, instance)}")
            # 三个数各管一件事：连接=客户端拨了多少次，服务=桩读到并答了的请求数，
            # 截断=答到一半客户端跑了的次数。"连接 > 服务"是第三种抖——请求根本没被读到
            # （异常发生在内核，`broken` 计不上数），以前只报总数时这类抖动是隐形的。
            print(f"桩自检：本轮连接 {_Handler.connections} 条 / 服务 {_Handler.served} 次 / "
                  f"截断 {_Handler.broken} 次（连接>服务 = 有请求没被读到；任一非零 = 装置在抖，红了不算产品缺陷）")
            print(f"桩逐次轨迹：{STUB_LOG}（一行一次模型调用；红了又分不清是谁的时候读它）")
            _Handler.served = 0
            _Handler.broken = 0
            _Handler.connections = 0
            _Handler.probe_hits.clear()
            _Handler.probe_rejects.clear()
            _Handler.compact_probe_armed = False
            _Handler.compact_calls = 0
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
    rc, lines = verdict(totals, skipped)
    print("\n===== 方言对照判定 =====")
    for line in lines:
        print(line)
    print(f"\n退出码={rc}（0=每一格全绿；1=有格红，归属见上面；2=这一格今天没法判）")
    return rc


if __name__ == "__main__":
    sys.exit(main())
