"""Run one eval sample through the live API and collect measurable evidence.

This is the bridge the optimizer probes through. It owns everything around the
agent — building a fresh fixture workspace, installing a candidate harness,
creating the project/task, auto-approving, waiting, and then *checking the work
itself*. The agent's own claim that tests pass is never trusted; the verify
command is run here, after the agent exits.

The workspace is rebuilt from the fixture for every run, so a candidate cannot
benefit from a previous candidate's leftovers.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol

import httpx

from blank_api.services.harness import spec

TERMINAL = {"succeeded", "failed", "cancelled", "paused"}
IGNORED_DIRS = {
    "node_modules",
    ".git",
    ".blank_backups",
    ".blank-backups",
    ".pytest_cache",
    "__pycache__",
    ".venv",
    "dist",
    ".mypy_cache",
}
MAX_SNAPSHOT_BYTES = 2_000_000


class ApiError(RuntimeError):
    def __init__(self, method: str, path: str, status: int, body: str) -> None:
        super().__init__(f"{method} {path} → {status} {body[:300]}")
        self.status = status
        self.body = body


class Api(Protocol):
    async def request(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> Any: ...

    async def close(self) -> None: ...


class HttpApi:
    """Thin async client for the Blank API used by the CLI and optimizer."""

    def __init__(self, base_url: str, *, timeout: float = 600.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout, connect=30.0, write=120.0, pool=30.0),
        )

    async def request(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> Any:
        response = await self._client.request(method, path, json=dict(body) if body is not None else None)
        if response.status_code >= 400:
            raise ApiError(method, path, response.status_code, response.text)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def close(self) -> None:
        await self._client.aclose()


@dataclass
class RunRecord:
    sample_id: str
    status: str = "error"
    verify_ok: bool = False
    verify_output: str = ""
    changed: list[str] = field(default_factory=list)
    self_verified: bool = False
    tokens: int = 0
    turns: int = 0
    model: str = ""
    transcript: str = ""
    trajectory: str = ""
    events: list[dict] = field(default_factory=list)
    harness_dirty: bool = False
    harness_note: str = ""
    baseline_ok: bool | None = None
    wall_ms: int = 0
    project_id: str = ""
    workspace_id: str = ""
    task_id: str = ""
    error: str = ""
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "sample_id": self.sample_id,
            "status": self.status,
            "verify_ok": self.verify_ok,
            "changed": list(self.changed),
            "self_verified": self.self_verified,
            "tokens": self.tokens,
            "turns": self.turns,
            "model": self.model,
            "harness_dirty": self.harness_dirty,
            "harness_note": self.harness_note,
            "baseline_ok": self.baseline_ok,
            "wall_ms": self.wall_ms,
            "project_id": self.project_id,
            "workspace_id": self.workspace_id,
            "task_id": self.task_id,
            "transcript": self.transcript[:400],
            "verify_output": self.verify_output[-600:],
            "error": self.error,
            "warnings": list(self.warnings),
        }


class BlankEvalAdapter:
    def __init__(
        self,
        api: Api,
        *,
        root: str | Path,
        provider_id: str = "",
        python_executable: str = "",
        node_executable: str = "",
        poll_interval: float = 1.5,
        timeout: float = 420.0,
        verify_timeout: float = 180.0,
        cleanup: bool = False,
        keep_workspace: bool = False,
        baseline_verify: bool = False,
    ) -> None:
        self.api = api
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.provider_id = provider_id
        self.python_executable = python_executable
        self.node_executable = node_executable
        self.poll_interval = max(0.1, poll_interval)
        self.timeout = timeout
        self.verify_timeout = verify_timeout
        self.cleanup = cleanup
        self.keep_workspace = keep_workspace
        self.baseline_verify = baseline_verify

    async def run(self, sample: Mapping[str, Any], tree: Mapping[str, str]) -> RunRecord:
        sample_id = str(sample.get("id") or "task")
        record = RunRecord(sample_id=sample_id)
        started = time.monotonic()
        workspace = self._workspace_path(sample_id)
        try:
            await self._build_workspace(sample, workspace)
            self.install_harness(workspace, tree)
        except Exception as exc:
            record.error = f"准备工作区失败：{exc.__class__.__name__}: {exc}"
            record.wall_ms = int((time.monotonic() - started) * 1000)
            return record

        installed = spec.read_tree(workspace / spec.HARNESS_DIR) if tree else {}
        if self.baseline_verify:
            record.baseline_ok, _ = await self._verify(sample, workspace)
        before = self._snapshot(workspace)
        project: dict | None = None

        try:
            project = await self.api.request(
                "POST",
                "/projects",
                {"name": f"harness-{sample_id}", "description": str(sample.get("title") or "")},
            )
            record.project_id = str((project or {}).get("id") or "")
            workspace_record = await self.api.request(
                "POST",
                f"/projects/{record.project_id}/workspaces",
                {"path": str(workspace), "label": sample_id},
            )
            record.workspace_id = str((workspace_record or {}).get("id") or "")
            task = await self.api.request(
                "POST",
                f"/projects/{record.project_id}/tasks",
                {
                    "title": str(sample.get("title") or sample_id),
                    "goal": str(sample.get("prompt") or ""),
                    "acceptance_criteria": list(sample.get("acceptance") or []),
                    "provider_id": self.provider_id or None,
                },
            )
            record.task_id = str((task or {}).get("id") or "")
            await self.api.request(
                "POST",
                f"/tasks/{record.task_id}/start",
                {"provider_id": self.provider_id or None, "approve_high_risk": True},
            )
            record.status = await self._wait_for_terminal(record.task_id)
            evidence = await self.api.request("GET", f"/tasks/{record.task_id}/evidence")
            self._absorb_evidence(record, evidence or {}, sample)
            record.events = await self._events(record.task_id)
            record.trajectory = render_trajectory(record.events)
        except Exception as exc:
            record.error = f"{exc.__class__.__name__}: {exc}"
            record.status = record.status if record.status in TERMINAL else "error"
            if record.task_id and record.status not in TERMINAL:
                try:
                    await self.api.request("POST", f"/tasks/{record.task_id}/cancel")
                except Exception:
                    pass
        finally:
            after = self._snapshot(workspace)
            record.changed = changed_files(before, after)
            record.harness_dirty, record.harness_note = _harness_changed(workspace, installed)
            if not record.error:
                record.verify_ok, record.verify_output = await self._verify(sample, workspace)
            if self.cleanup and record.project_id:
                try:
                    await self.api.request("DELETE", f"/projects/{record.project_id}")
                except Exception as exc:
                    record.warnings.append(f"清理项目失败：{exc.__class__.__name__}")
            if not self.keep_workspace:
                try:
                    await self._remove(workspace)
                except OSError as exc:
                    record.warnings.append(f"清理工作区失败：{exc}")
            record.wall_ms = int((time.monotonic() - started) * 1000)
        return record

    # ── workspace ────────────────────────────────────────────────────

    def _workspace_path(self, sample_id: str) -> Path:
        safe = re.sub(r'[<>:"|?*\x00-\x1f]', "_", sample_id.replace("/", "__")).strip(" .") or "task"
        return self.root / safe

    async def _build_workspace(self, sample: Mapping[str, Any], workspace: Path) -> None:
        files = sample.get("files")
        if not isinstance(files, Mapping):
            fixture = sample.get("fixture")
            if isinstance(fixture, Mapping):
                files = fixture
            else:
                raise ValueError("样例没有 files/fixture 文件表；CLI 需要先把 fixture 名解析成文件")
        await self._remove(workspace)
        workspace.mkdir(parents=True, exist_ok=True)
        for rel, content in files.items():
            relative = spec.normalize_rel(str(rel))
            target = workspace / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(content), encoding="utf-8")

    def install_harness(self, workspace: str | Path, tree: Mapping[str, str]) -> None:
        directory = Path(workspace) / spec.HARNESS_DIR
        if directory.exists():
            shutil.rmtree(directory, ignore_errors=True)
        if tree:
            spec.write_tree(directory, tree)

    def _snapshot(self, workspace: Path) -> dict[str, bytes]:
        out: dict[str, bytes] = {}
        harness = (workspace / spec.HARNESS_DIR).resolve()
        for path in sorted(workspace.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            rel = path.relative_to(workspace).as_posix()
            if any(part in IGNORED_DIRS for part in path.relative_to(workspace).parts[:-1]):
                continue
            try:
                if harness in path.resolve().parents or path.resolve() == harness:
                    continue
            except OSError:
                continue
            try:
                if path.stat().st_size > MAX_SNAPSHOT_BYTES:
                    continue
                out[rel] = path.read_bytes()
            except OSError:
                continue
        return out

    async def _remove(self, target: Path) -> None:
        if not target.exists():
            return
        for _ in range(4):
            try:
                shutil.rmtree(target)
                return
            except OSError:
                await asyncio.sleep(0.25)
        if target.exists():
            raise OSError(f"无法清理目录：{target}")

    # ── API run ──────────────────────────────────────────────────────

    async def _wait_for_terminal(self, task_id: str) -> str:
        deadline = time.monotonic() + self.timeout
        status = "running"
        while True:
            evidence = await self.api.request("GET", f"/tasks/{task_id}/evidence")
            status = str(((evidence or {}).get("task") or {}).get("status") or "running")
            if status == "waiting_approval":
                try:
                    await self.api.request(
                        "POST",
                        f"/tasks/{task_id}/approval",
                        {"approve": True, "provider_id": self.provider_id or None},
                    )
                except ApiError:
                    # 没有待审批项时接口会 409（例如两个审批同时排队），继续轮询即可
                    pass
                continue
            if status in TERMINAL:
                return status
            if time.monotonic() > deadline:
                # 先取消，再等当前步骤收尾。否则下面的 verify 会与仍在写文件的
                # agent 并发跑，测出来的工作区是半成品。
                try:
                    await self.api.request("POST", f"/tasks/{task_id}/cancel")
                except Exception:
                    pass
                grace = time.monotonic() + 10.0
                while time.monotonic() < grace:
                    probe = await self.api.request("GET", f"/tasks/{task_id}/evidence")
                    current = str(((probe or {}).get("task") or {}).get("status") or "")
                    if current in TERMINAL:
                        return current
                    await asyncio.sleep(0.5)
                return "timeout"
            await asyncio.sleep(self.poll_interval)

    async def _events(self, task_id: str) -> list[dict]:
        events: list[dict] = []
        cursor = 0
        while True:
            batch = await self.api.request(
                "GET", f"/tasks/{task_id}/events?after_seq={cursor}&limit=2000"
            )
            if not batch:
                return events
            events.extend(batch)
            cursor = int(batch[-1].get("seq") or cursor)
            if len(batch) < 2000:
                return events

    def _absorb_evidence(self, record: RunRecord, evidence: Mapping[str, Any], sample: Mapping[str, Any]) -> None:
        task = evidence.get("task") or {}
        usage = evidence.get("usage") or {}
        tools = evidence.get("tools") or []
        record.status = str(task.get("status") or record.status)
        record.transcript = str(task.get("result_summary") or "")
        record.tokens = int(
            usage.get("total_tokens")
            or (int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0))
        )
        record.turns = int(usage.get("calls") or len(tools))
        record.model = ",".join(usage.get("models") or []) or "-"

        commands = [str((tool.get("arguments") or {}).get("command") or "") for tool in tools]
        verify = str(sample.get("verify") or "")
        head = verify.split(" ")[0] if verify.strip() else ""
        record.self_verified = any(
            tool.get("name") == "run_tests" and tool.get("status") == "ok" for tool in tools
        ) or bool(head and any(head in command for command in commands))

    async def _verify(self, sample: Mapping[str, Any], workspace: Path) -> tuple[bool, str]:
        command = self._verify_command(str(sample.get("verify") or ""))
        if not command:
            return False, "未配置验证命令"
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=str(workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.verify_timeout
            )
            output = (stdout or b"").decode("utf-8", errors="replace") + (
                stderr or b""
            ).decode("utf-8", errors="replace")
            return process.returncode == 0, output[-2_000:]
        except asyncio.TimeoutError:
            try:
                process.kill()
            except Exception:
                pass
            return False, f"验证命令超时（{self.verify_timeout:.0f}s）"
        except OSError as exc:
            return False, f"验证命令无法执行：{exc}"

    def _verify_command(self, command: str) -> str:
        text = command.strip()
        if not text:
            return ""
        if self.python_executable and re.match(r"^python(?:3(?:\.\d+)?)?\b", text):
            text = re.sub(
                r"^python(?:3(?:\.\d+)?)?\b",
                lambda _match: f'"{self.python_executable}"',
                text,
                count=1,
            )
        elif self.node_executable and re.match(r"^node\b", text):
            text = re.sub(
                r"^node\b", lambda _match: f'"{self.node_executable}"', text, count=1
            )
        return text


def changed_files(before: Mapping[str, bytes], after: Mapping[str, bytes]) -> list[str]:
    changed: list[str] = []
    for rel, content in after.items():
        old = before.get(rel)
        if old is None or old != content:
            changed.append(rel)
    for rel in before:
        if rel not in after:
            changed.append(rel)
    return sorted(changed)


def _harness_changed(workspace: Path, installed: Mapping[str, str]) -> tuple[bool, str]:
    directory = workspace / spec.HARNESS_DIR
    if not directory.exists():
        return bool(installed), "harness 目录在运行中被删除" if installed else ""
    try:
        observed = spec.read_tree(directory)
    except (spec.HarnessError, OSError) as exc:
        return True, f"harness 无法读取：{exc}"
    extras = sorted(
        path.relative_to(directory).as_posix()
        for path in directory.rglob("*")
        if path.is_file()
    )
    extras = [rel for rel in extras if rel not in observed and rel not in ("README.md", ".gitkeep")]
    if observed != {str(rel): str(body) for rel, body in installed.items()}:
        return True, "harness 内容在运行中被修改"
    if extras:
        return True, f"harness 中新增了文件：{', '.join(extras[:3])}"
    return False, ""


def render_trajectory(events: list[dict], *, limit: int = 20_000) -> str:
    """A compact [ACTION]/[OBSERVATION]/[ERROR] transcript of the run."""
    lines: list[str] = []
    for event in events:
        kind = str(event.get("type") or "")
        payload = event.get("payload") or {}
        if kind == "tool.requested":
            name = str(payload.get("tool") or "?")
            arguments = json.dumps(payload.get("arguments") or {}, ensure_ascii=False, default=str)
            lines.append(f"[ACTION] {name} {arguments}"[:600])
        elif kind == "tool.completed":
            name = str(payload.get("tool") or "?")
            status = str(payload.get("status") or "")
            summary = str(payload.get("summary") or payload.get("message") or "").replace("\n", " ")
            lines.append(f"[OBSERVATION] {name} {status} {summary}"[:600])
        elif kind in ("task.failed", "approval.denied", "tool.error"):
            message = str(payload.get("error") or payload.get("reason") or payload.get("message") or kind)
            lines.append(f"[ERROR] {message}"[:600])
        elif kind == "intent.classified":
            lines.append(f"[INTENT] {payload.get('intent') or payload.get('kind') or ''}"[:200])
    text = "\n".join(lines)
    if len(text) > limit:
        text = "…（前段已截断）\n" + text[-limit:]
    return text
