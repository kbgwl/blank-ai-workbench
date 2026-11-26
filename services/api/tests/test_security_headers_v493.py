"""API 自身的最小安全基线：安全头 + 可执行文件降级为纯文本。

本机服务不等于没有攻击面：浏览器可能被诱导直接打开 `/api/...` 下的附件或工作区文件。
没有 `nosniff`、且按 `text/html` inline 返回时，一份上传的 HTML 就能在 API 源上执行
脚本。这里钉两件事：每个响应都有安全头；能在浏览器里执行的类型一律按纯文本回。
"""

from __future__ import annotations

import base64

EXPECTED = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "cross-origin-resource-policy": "same-origin",
    "content-security-policy": "frame-ancestors 'none'",
}


def _assert_headers(response) -> None:
    for name, value in EXPECTED.items():
        assert response.headers.get(name) == value, f"{name}={response.headers.get(name)!r}"


async def test_api_responses_carry_security_headers(client):
    _assert_headers(await client.get("/api/v1/health"))
    # 错误响应也走同一层，不能只在 200 上加。
    _assert_headers(await client.get("/api/v1/definitely-not-a-route"))
    # 配了 token 的部署里 /docs 是 401，但安全头仍要在。
    docs = await client.get("/docs")
    assert docs.status_code in (200, 401)
    _assert_headers(docs)


async def _project(client) -> str:
    response = await client.post("/api/v1/projects", json={"name": "安全头", "description": ""})
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def test_html_upload_is_served_as_plain_text(client):
    pid = await _project(client)
    html = b"<html><script>document.title='pwned'</script></html>"
    created = await client.post(
        f"/api/v1/projects/{pid}/uploads",
        json={
            "name": "evil.html",
            "mime": "text/html",
            "data": base64.b64encode(html).decode("ascii"),
        },
    )
    assert created.status_code == 201, created.text

    read = await client.get(f"/api/v1/projects/{pid}/uploads/{created.json()['id']}")

    assert read.status_code == 200
    assert read.headers["content-type"].startswith("text/plain")
    assert read.content == html
    _assert_headers(read)


async def test_workspace_html_raw_is_served_as_plain_text(client, workspace):
    pid = await _project(client)
    bound = await client.post(
        f"/api/v1/projects/{pid}/workspaces", json={"path": str(workspace)}
    )
    assert bound.status_code == 201, bound.text
    page = workspace / "page.html"
    page.write_text("<html><script>alert(1)</script></html>", encoding="utf-8")

    read = await client.get(
        f"/api/v1/projects/{pid}/file/raw", params={"path": "page.html"}
    )

    assert read.status_code == 200
    assert read.headers["content-type"].startswith("text/plain")
    assert b"alert(1)" in read.content


async def test_validation_errors_do_not_echo_submitted_secrets(client):
    secret = "sk-" + "x" * 300
    response = await client.post("/api/v1/projects", json={"name": secret})

    assert response.status_code == 422
    assert secret not in response.text
    assert "validation_error" in response.text


async def test_provider_base_url_only_allows_http_without_credentials(client):
    for base_url in ("file:///etc/passwd", "javascript:alert(1)", "https://user:pass@example.com/v1"):
        response = await client.post(
            "/api/v1/providers",
            json={"name": "坏地址", "base_url": base_url, "model": "m"},
        )
        assert response.status_code == 422, f"{base_url} 应被拒绝：{response.text}"


async def test_png_raw_keeps_its_image_mime(client, workspace):
    pid = await _project(client)
    await client.post(f"/api/v1/projects/{pid}/workspaces", json={"path": str(workspace)})

    read = await client.get(
        f"/api/v1/projects/{pid}/file/raw", params={"path": "logo.png"}
    )

    assert read.status_code == 200
    assert read.headers["content-type"].startswith("image/png")
