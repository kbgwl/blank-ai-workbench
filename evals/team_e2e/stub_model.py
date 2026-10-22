"""OpenAI-compatible stub for the team end-to-end gate.

不是语言模型：按 system 里的身份（主理人 / 后端）和 scenario.json 的场景开关，
给出确定性的工具调用或回答，用来驱动真实运行链路。所有产物落在 TEAM_E2E_WORK 下。
"""

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WORK = Path(os.environ.get("TEAM_E2E_WORK") or ".").resolve()
LOG = WORK / "stub-requests.log"
SYSTEM_DIR = WORK / "system"
SCENARIO = WORK / "scenario.json"
CURATED_HEADER = "上下文管家为本次任务挑选"


def _scenario() -> dict:
    try:
        return json.loads(SCENARIO.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write(data: dict) -> None:
    SCENARIO.write_text(json.dumps(data), encoding="utf-8")


def _step(name: str) -> bool:
    """建 Bot → 派活这条链按「还没调过哪个」推进，走完自动收旗。"""
    data = _scenario()
    if not data.get("create_assign"):
        return False
    done = data.get("done") or []
    if name in done:
        return False
    done.append(name)
    data["done"] = done
    if len(done) >= 2:
        data["create_assign"] = False
    _write(data)
    return True


def _once(name: str) -> bool:
    """场景开关吃一次就熄灭：否则收尾轮会拿同一句话再请求一次工具，越跑越像死循环。"""
    data = _scenario()
    if not data.get(name):
        return False
    data[name] = False
    _write(data)
    return True


def _dump_system(tag: str, system: str) -> None:
    SYSTEM_DIR.mkdir(parents=True, exist_ok=True)
    (SYSTEM_DIR / f"{tag}-{int(time.time() * 1000)}.txt").write_text(system, encoding="utf-8")


def _last_user(messages: list) -> str:
    """续跑时运行时会替换最后的 user 提示，所以触发词要看全部 user 消息拼起来的那段。"""
    parts = []
    for item in messages:
        if item.get("role") != "user":
            continue
        content = item.get("content")
        if isinstance(content, list):
            parts.append(" ".join(str(part.get("text") or "") for part in content))
        else:
            parts.append(str(content or ""))
    return " ".join(parts)


def _call(name: str, arguments: dict) -> dict:
    return {
        "content": f"先调用 {name}。",
        "tool_calls": [
            {
                "id": f"call_{int(time.time() * 1000)}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
            }
        ],
    }


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            payload = {}
        messages = payload.get("messages") or []
        system = "\n".join(str(item.get("content") or "") for item in messages if item.get("role") == "system")
        has_tool_result = any(item.get("role") == "tool" for item in messages)
        prompt = _last_user(messages)

        if "你的身份：小白管家" in system:
            # 意图判定那一步不带工具表，给它工具调用只会污染分类；这里只在有工具时才会出手
            if not payload.get("tools"):
                message = {"content": '{"intent": "act", "note": "把前端拉进群", "steps": [], "answer": ""}'}
                finish = "stop"
            elif _step("team_create_bot"):
                message = _call(
                    "team_create_bot",
                    {
                        "name": "测试",
                        "role": "测试与回归",
                        "provider_id": str(_scenario().get("provider_id") or ""),
                    },
                )
                finish = "tool_calls"
            elif _step("team_assign_task"):
                message = _call("team_assign_task", {"bot": "测试", "task": "补一条登录回归测试"})
                finish = "tool_calls"
            elif "把前端拉进群" in prompt and _once("add_member"):
                message = _call("team_add_member", {"bot": "前端"})
                finish = "tool_calls"
            elif "不存在的成员" in prompt and _once("add_missing"):
                message = _call("team_add_member", {"bot": "不存在的成员"})
                finish = "tool_calls"
            elif has_tool_result:
                message = {"content": "@后端 请汇报登录接口状态。"}
                finish = "stop"
            else:
                message = _call("team_status", {})
                finish = "tool_calls"
            _dump_system(f"admin-{finish}", system)
        elif "你的身份：后端" in system:
            # 只有铺数据那一轮带唯一标记：后续轮次会重复同一句结论，
            # 搜索点验需要一个「只此一条、且在默认窗口之外」的目标。
            message = {
                "content": "登录接口已修复，结论是 token 解析少了一个 await。这条已归档为种子结论。"
                if _once("seed_reply")
                else "登录接口已修复，结论是 token 解析少了一个 await。"
            }
            finish = "stop"
        else:
            message = {"content": "[PASS]"}
            finish = "stop"

        with LOG.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "system_chars": len(system),
                        "has_curated": CURATED_HEADER in system,
                        "prompt": prompt[:200],
                        "finish": finish,
                        "content": message.get("content"),
                        "tools": [call["function"]["name"] for call in (message.get("tool_calls") or [])],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        data = json.dumps(
            {
                "choices": [{"message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                "model": "stub-team",
            },
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args) -> None:
        pass


if __name__ == "__main__":
    port = int(os.environ.get("TEAM_E2E_STUB_PORT") or "8097")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
