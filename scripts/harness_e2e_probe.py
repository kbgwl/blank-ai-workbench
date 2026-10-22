"""End-to-end probe: real app + real agent graph + harness adapter.

No model credentials: a scripted MockModel is injected through
app.state.model_client_factory, the same seam the test suite uses. Everything
else is the production path — task creation, LangGraph run, tool gateway,
validation, evidence, and the adapter's own verify.

Positive run, then two negative controls (they must fail the way they claim):

    uv run --project services/api python scripts/harness_e2e_probe.py
    PROBE_MODE=no-harness uv run --project services/api python scripts/harness_e2e_probe.py
    PROBE_MODE=no-write   uv run --project services/api python scripts/harness_e2e_probe.py

Exit code 0 means the expected signature was observed. Set KEEP_PROBE=1 to keep
the temporary workspace for inspection.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="blank-harness-probe-"))

os.environ["BLANK_DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["BLANK_SANDBOX_ENABLED"] = "0"
os.environ["BLANK_NOTES_DIR"] = str(TMP / "notes")
os.environ["BLANK_MEMORY_DIR"] = str(TMP / "memory")
os.environ["BLANK_WORKTREE_ROOT"] = str(TMP / "worktrees")
os.environ["BLANK_SETTINGS_FILE"] = str(TMP / "settings.json")
(TMP / "notes").mkdir(parents=True, exist_ok=True)
(TMP / "memory").mkdir(parents=True, exist_ok=True)
(TMP / "settings.json").write_text(
    json.dumps(
        {
            "safety": {
                "shell_enabled": True,
                "network_enabled": True,
                "confirm_all_commands": False,
                "restrict_to_workspace": True,
            }
        }
    ),
    encoding="utf-8",
)

import httpx  # noqa: E402

from blank_api.app import create_app  # noqa: E402
from blank_api.db import Base, get_engine  # noqa: E402
from blank_api.services.harness.adapter import ApiError, BlankEvalAdapter  # noqa: E402
from blank_api.services.harness.proposer import ProposalRequest, RuleProposer  # noqa: E402

API = "/api/v1"
MARKER = "PROBE-HARNESS-MARKER-7F3A"
PATCH = "--- /dev/null\n+++ b/done.txt\n@@ -0,0 +1,1 @@\n+ok\n"
MODE = os.environ.get("PROBE_MODE", "")


class MockModel:
    def __init__(self) -> None:
        self.steps: list[dict] = []
        self.index = 0
        self.requests: list[dict] = []

    def reset(self, steps: list[dict]) -> None:
        self.steps = steps
        self.index = 0

    def factory(self, **kwargs) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handler))

    def _handler(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.requests.append(payload)
        step = self.steps[self.index] if self.index < len(self.steps) else {"content": "已完成"}
        self.index += 1
        message: dict = {"content": step.get("content", "")}
        if step.get("tool_calls"):
            message["tool_calls"] = step["tool_calls"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": message,
                        "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                "model": "mock-coder",
            },
        )


class AsgiApi:
    """The adapter only needs request(); this points it at the in-process app."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def request(self, method: str, path: str, body=None):
        response = await self.client.request(
            method, f"{API}{path}", json=dict(body) if body is not None else None
        )
        if response.status_code >= 400:
            raise ApiError(method, path, response.status_code, response.text)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def close(self) -> None:
        return None


def call(name: str, arguments: dict) -> dict:
    return {
        "id": f"call_{uuid.uuid4().hex[:8]}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def build_sample() -> dict:
    return {
        "id": "probe-create-done",
        "title": "创建 done.txt",
        "prompt": "在工作区创建 done.txt，内容写 ok；然后用 run_tests 验证原有测试仍然通过。",
        "acceptance": ["done.txt 存在且内容为 ok", "原有测试仍然通过"],
        "files": {
            "pytest.ini": "[pytest]\n",
            "tests/test_demo.py": "def test_demo():\n    assert True\n",
        },
        "verify": (
            "python -c \"import pathlib,sys; "
            "sys.exit(0 if pathlib.Path('done.txt').read_text(encoding='utf-8').strip() == 'ok' else 1)\""
        ),
    }


async def main() -> int:
    app = create_app()
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    mock = MockModel()
    app.state.model_client_factory = mock.factory
    tool_step: dict = {
        "content": "创建文件",
        "tool_calls": [call("apply_patch", {"path": "done.txt", "patch": PATCH})],
    }
    if MODE == "no-write":
        # 负对照：只声称完成、不落盘。验证必须判失败，改动必须为空。
        tool_step = {"content": "已完成"}
    mock.reset(
        [
            {"content": "执行计划：\n1. 创建 done.txt\n2. 运行测试"},
            tool_step,
            {"content": "文件已创建"},
            {
                "content": json.dumps(
                    {"approved": True, "findings": [], "summary": "实现与验收条件一致"},
                    ensure_ascii=False,
                )
            },
            {
                "content": json.dumps(
                    {
                        "summary": "已创建 done.txt 并通过测试",
                        "acceptance": [
                            {"criterion": "done.txt 存在且内容为 ok", "covered": True, "evidence": "apply_patch 成功"},
                            {"criterion": "原有测试仍然通过", "covered": True, "evidence": "run_tests 通过"},
                        ],
                    },
                    ensure_ascii=False,
                )
            },
        ]
    )

    failures: list[str] = []
    checks: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        checks.append(f"{'PASS' if condition else 'FAIL'} {name}{(' — ' + detail) if detail else ''}")
        if not condition:
            failures.append(name)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://probe"
    ) as client:
        provider = await client.post(
            f"{API}/providers",
            json={
                "name": "probe-vendor",
                "kind": "openai_compatible",
                "base_url": "https://mock.example/v1",
                "model": "mock-coder",
                "api_key": "sk-probesecret123456",
                "supports_tools": True,
            },
        )
        provider.raise_for_status()
        provider_id = provider.json()["id"]

        tree = (
            {}
            if MODE == "no-harness"
            else {
                "systemprompt.md": f"回答前先确认：{MARKER}。\n",
                "guardrails.md": "只改验收条件要求的文件。\n",
            }
        )
        sample = build_sample()
        root = TMP / "runs"
        adapter = BlankEvalAdapter(
            AsgiApi(client),
            root=root,
            provider_id=provider_id,
            python_executable=sys.executable,
            poll_interval=0.2,
            timeout=120.0,
            keep_workspace=True,
        )
        record = await adapter.run(sample, tree)
        workspace = root / "probe-create-done"

        print("record:", json.dumps(record.as_dict(), ensure_ascii=False, indent=2))
        payload_text = "\n".join(
            json.dumps(item, ensure_ascii=False) for item in mock.requests
        )
        system_text = ""
        for item in mock.requests:
            for message in item.get("messages") or []:
                if message.get("role") == "system":
                    system_text += str(message.get("content") or "")

        if MODE == "no-harness":
            # 负对照：不安装 harness 时，标记必须进不了系统提示。
            check("negative: marker absent from prompts", MARKER not in payload_text)
            check("negative: marker absent from system role", MARKER not in system_text)
            check("negative: no harness dir", not (workspace / ".blank" / "harness").exists())
            check("negative: task still verifies", record.verify_ok)
            print("\n".join(checks))
            print(f"FAILURES: {failures}" if failures else "NEGATIVE CONTROL OK (no-harness)")
            if not failures and os.environ.get("KEEP_PROBE") != "1":
                shutil.rmtree(TMP, ignore_errors=True)
            return 0 if not failures else 1

        if MODE == "no-write":
            # 负对照：agent 只声称完成、没有落盘时，adapter 的验证必须失败。
            check("negative: verify fails without write", not record.verify_ok)
            check("negative: no changed files", record.changed == [], str(record.changed))
            check("negative: agent did not self-verify", not record.self_verified)
            check("negative: harness untouched", not record.harness_dirty)
            print("\n".join(checks))
            print(f"FAILURES: {failures}" if failures else "NEGATIVE CONTROL OK (no-write)")
            if not failures and os.environ.get("KEEP_PROBE") != "1":
                shutil.rmtree(TMP, ignore_errors=True)
            return 0 if not failures else 1

        check("status succeeded", record.status == "succeeded", record.status)
        check("adapter verify ok", record.verify_ok, record.verify_output[-300:])
        check("changed contains done.txt", "done.txt" in record.changed, str(record.changed))
        check("workspace file ok", (workspace / "done.txt").read_text(encoding="utf-8").strip() == "ok")
        check("harness not dirty", not record.harness_dirty, record.harness_note)
        check("agent self-verified via run_tests", record.self_verified)
        check("harness marker reached real system prompt", MARKER in payload_text)
        check("marker in system role", MARKER in system_text)
        check(
            "guardrails also composed",
            "只改验收条件要求的文件" in system_text,
        )

        evidence = await client.get(f"{API}/tasks/{record.task_id}/evidence")
        evidence.raise_for_status()
        body = evidence.json()
        check(
            "evidence validation passed",
            bool((body.get("validation") or {}).get("passed")),
            str(body.get("validation")),
        )
        ran_in_validation = any(
            tool.get("name") == "run_tests"
            and tool.get("node") == "run_validation"
            and tool.get("status") == "ok"
            for tool in body.get("tools") or []
        )
        check("validation ran in run_validation node", ran_in_validation)
        if not ran_in_validation:
            print("tools:", json.dumps(body.get("tools"), ensure_ascii=False))
        check(
            "evidence change recorded",
            "done.txt" in (body.get("changes") or {}).get("files", []),
        )

        # RuleProposer 也必须能在同一条真实样例上产出可探测的候选，且带上起始树。
        candidates = await RuleProposer().propose(
            ProposalRequest(base=tree, domain="整理文件", title=sample["title"], count=1)
        )
        candidate = candidates[0]
        check("proposer candidate is whole tree", candidate.tree["systemprompt.md"].startswith("回答前"))
        check("proposer keeps new content", candidate.tree != tree)

    print("\n".join(checks))
    print(f"requests={len(mock.requests)} tokens={record.tokens} turns={record.turns}")
    print(f"tmp={TMP}")
    if failures:
        print(f"FAILURES: {failures}")
        return 1
    print("E2E PROBE OK")
    if os.environ.get("KEEP_PROBE") != "1":
        shutil.rmtree(TMP, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
