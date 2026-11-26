"""工作区应用预览：只认 node start、只绑回环、沙箱档位不让就拒绝。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from blank_api.services.preview import PreviewError, PreviewManager
from blank_api.services.sandbox.policy import SandboxPolicy
from blank_api.services.tools.browser import node_exe


def _write_app(root: Path, start: str = "node server.js") -> None:
    (root / "package.json").write_text(
        json.dumps({"name": "preview-fixture", "scripts": {"start": start}}),
        encoding="utf-8",
    )
    (root / "server.js").write_text(
        "const http = require('http');\n"
        "const port = Number(process.env.PORT || 0);\n"
        "http.createServer((req, res) => {\n"
        "  res.writeHead(200, {'content-type': 'text/html'});\n"
        "  res.end('<h1 id=\"preview-ok\">ok</h1>');\n"
        "}).listen(port, '127.0.0.1');\n",
        encoding="utf-8",
    )


def test_rejects_when_deployment_requires_container(tmp_path: Path):
    _write_app(tmp_path)
    manager = PreviewManager(SandboxPolicy(allow_local=False))

    async def run():
        with pytest.raises(PreviewError) as caught:
            await manager.start("p-container", tmp_path)
        assert caught.value.code == "preview_isolation_unavailable"
        assert caught.value.status == 409

    asyncio.run(run())


def test_rejects_non_node_start(tmp_path: Path):
    _write_app(tmp_path, start="npm start")
    manager = PreviewManager()

    async def run():
        with pytest.raises(PreviewError) as caught:
            await manager.start("p-npm", tmp_path)
        assert caught.value.code == "preview_start_not_supported"

    asyncio.run(run())


def test_rejects_start_script_escaping_workspace(tmp_path: Path):
    _write_app(tmp_path, start="node ../outside.js")
    manager = PreviewManager()

    async def run():
        with pytest.raises(PreviewError) as caught:
            await manager.start("p-escape", tmp_path)
        assert caught.value.code == "preview_start_not_supported"

    asyncio.run(run())


def test_status_without_session_is_stopped():
    manager = PreviewManager()

    async def run():
        assert await manager.status("missing") == {"status": "stopped"}
        assert await manager.stop("missing") == {"status": "stopped"}

    asyncio.run(run())


@pytest.mark.skipif(not node_exe(), reason="本机没有 node，无法起预览子进程")
def test_starts_reuses_and_stops_real_node_server(tmp_path: Path):
    _write_app(tmp_path)
    manager = PreviewManager(SandboxPolicy(allow_local=True))

    async def run():
        first = await manager.start("p-real", tmp_path)
        try:
            assert first["status"] == "running"
            assert first["url"].startswith("http://127.0.0.1:")
            assert first["isolation"] in {"local", "local-job"}

            again = await manager.start("p-real", tmp_path)
            assert again["url"] == first["url"], "同项目重复启动应复用现有会话"

            status = await manager.status("p-real")
            assert status["status"] == "running"
            assert status["expires_at"]
        finally:
            stopped = await manager.stop("p-real")
        assert stopped["status"] == "stopped"
        assert (await manager.status("p-real"))["status"] == "stopped"

    asyncio.run(run())
