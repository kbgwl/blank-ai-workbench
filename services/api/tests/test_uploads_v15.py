"""图片附件：上传、回读、落库，以及"进模型"的那一段转换。

重点在最后一段。上传能跑通不代表模型能看到图——四家供应商对图片的表示方式
各不相同（OpenAI 是 part 数组、Anthropic 是 image block、Ollama 是消息上的一个
字段、Responses 是 input_image），任何一家漏了，用户看到的现象都是
"传了图，它却说没看到"，而不是一个能定位的报错。所以这里逐家断言请求体。
"""

import base64
import uuid

from blank_api.services.agent.graph import initial_messages
from blank_api.services.models.anthropic import AnthropicAdapter
from blank_api.services.models.base import AdapterConfig, ChatMessage, openai_content
from blank_api.services.models.ollama import _ollama_message
from blank_api.services.models.openai_responses import OpenAIResponsesAdapter

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


async def _project(client, name="带图的项目"):
    resp = await client.post("/api/v1/projects", json={"name": name, "description": ""})
    assert resp.status_code == 201
    return resp.json()["id"]


def _b64(data: bytes = PNG) -> str:
    return base64.b64encode(data).decode("ascii")


async def test_upload_roundtrip_and_read(client):
    pid = await _project(client)
    created = await client.post(
        f"/api/v1/projects/{pid}/uploads",
        json={"name": "截图.png", "mime": "image/png", "data": _b64()},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["mime"] == "image/png"
    assert body["size"] == len(PNG)
    # 原始文件名保留给界面显示，但落盘用的是 id，不拿用户的名字去拼路径。
    assert body["name"] == "截图.png"

    read = await client.get(f"/api/v1/projects/{pid}/uploads/{body['id']}")
    assert read.status_code == 200
    assert read.headers["content-type"].startswith("image/png")
    assert read.content == PNG


async def test_upload_rejects_what_models_cannot_read(client):
    pid = await _project(client)
    svg = await client.post(
        f"/api/v1/projects/{pid}/uploads",
        json={"name": "a.svg", "mime": "image/svg+xml", "data": _b64(b"<svg/>")},
    )
    assert svg.status_code == 422
    assert svg.json()["error"]["code"] == "upload_invalid"

    broken = await client.post(
        f"/api/v1/projects/{pid}/uploads",
        json={"name": "a.png", "mime": "image/png", "data": "not-base64!!"},
    )
    assert broken.status_code == 422

    too_big = await client.post(
        f"/api/v1/projects/{pid}/uploads",
        json={"name": "big.png", "mime": "image/png", "data": _b64(b"\x00" * (8 * 1024 * 1024 + 1))},
    )
    assert too_big.status_code == 422
    assert "8MB" in too_big.json()["error"]["message"]


async def test_upload_path_traversal_is_not_reachable(client):
    pid = await _project(client)
    for bad_id in ("../../etc/passwd", "..%2f..%2fsecret", "abc/def"):
        resp = await client.get(f"/api/v1/projects/{pid}/uploads/{bad_id}")
        assert resp.status_code in (404, 422)


async def test_task_keeps_attachments_and_drops_unknown_ids(client):
    pid = await _project(client)
    ref = (
        await client.post(
            f"/api/v1/projects/{pid}/uploads",
            json={"name": "before.png", "mime": "image/png", "data": _b64()},
        )
    ).json()

    task = await client.post(
        f"/api/v1/projects/{pid}/tasks",
        json={
            "title": "照这张图改样式",
            "goal": "照这张图改样式",
            "attachments": [{"id": ref["id"], "name": "改前.png"}, {"id": "deadbeef" * 4}],
        },
    )
    assert task.status_code == 201, task.text
    saved = task.json()["attachments"]
    # 只留下真实存在的那一张，名字用前端给的（用户改过的文件名要保留）。
    assert [item["id"] for item in saved] == [ref["id"]]
    assert saved[0]["name"] == "改前.png"


def _adapter(cls, **overrides):
    config = AdapterConfig(name="t", kind="k", base_url="http://x", api_key="k", model="m", **overrides)
    return cls(config)


def test_each_vendor_maps_images_its_own_way():
    message = ChatMessage(role="user", content="看这张图", images=[{"media_type": "image/png", "data": "AAA"}])

    # OpenAI 兼容：part 数组，图片是 image_url + data URL。
    parts = openai_content(message)
    assert parts[0] == {"type": "text", "text": "看这张图"}
    assert parts[1]["image_url"]["url"] == "data:image/png;base64,AAA"

    # 没图时必须还是纯字符串：国内几家的兼容层对数组形式的纯文本支持不全。
    assert openai_content(ChatMessage(role="user", content="只有字")) == "只有字"

    anthropic = _adapter(AnthropicAdapter)._message_payload([message], cache_last=False)
    assert anthropic[0]["content"][0]["type"] == "image"
    assert anthropic[0]["content"][0]["source"]["data"] == "AAA"

    ollama = _ollama_message(message)
    assert ollama["images"] == ["AAA"]  # Ollama 要裸 base64，不带 data URL 前缀

    responses = _adapter(OpenAIResponsesAdapter)._input_items([message])
    assert responses[0]["content"][0] == {"type": "input_image", "image_url": "data:image/png;base64,AAA"}


def test_initial_messages_attach_images_only_when_model_can_see():
    images = [{"name": "a.png", "media_type": "image/png", "data": "AAA"}]

    # 图片本身不进 state：state 会被写进检查点，base64 会让它涨十几兆。
    # state 里只该有"附了几张图"这句文本，真图由 session.vision_images 在出站前挂上。
    seen = initial_messages("改样式", [], images=images, supports_vision=True)
    assert "images" not in seen[-1]
    assert "2 张图片" not in seen[-1]["content"] and "1 张图片" in seen[-1]["content"]

    # 不支持视觉：明确说看不到，并要求它不要猜——否则它会编一段"我看到图里……"。
    blind = initial_messages("改样式", [], images=images, supports_vision=False)
    assert "images" not in blind[-1]
    assert "不支持看图" in blind[-1]["content"]
    assert "不要猜测" in blind[-1]["content"]


def test_images_are_attached_at_send_time_not_in_state():
    """出站的那份 messages 里要有图；state 里那份不能有。"""
    from blank_api.services.agent.graph import attach_vision_images

    images = [{"name": "a.png", "media_type": "image/png", "data": "AAA"}]
    state_messages = initial_messages("改样式", [], images=images, supports_vision=True)
    assert all("images" not in item for item in state_messages)

    chat = [
        ChatMessage(role="system", content="sys"),
        ChatMessage(role="user", content="上一轮的问题"),
        ChatMessage(role="assistant", content="上一轮的回答"),
        ChatMessage(role="user", content="这一轮：看这张图"),
    ]
    attach_vision_images(chat, images)
    # 只挂最后一条 user：挂到第一条会变成"拿今天的图去答昨天的问"。
    assert chat[3].images == images
    assert chat[1].images == []
    assert chat[0].images == []

    # 没图时什么都不做，别给所有请求平白加一层循环。
    blank = [ChatMessage(role="user", content="x")]
    attach_vision_images(blank, None)
    assert blank[0].images == []



def test_load_images_skips_missing_and_stops_at_total_cap(tmp_path):
    """一张图被清理掉，代价应该是"少一张"，不是"这条消息发不出去"。"""
    from blank_api.services.uploads import UploadStore, load_images_for

    store = UploadStore(tmp_path)
    first = store.save("p1", "a.png", "image/png", PNG)
    second = store.save("p1", "b.png", "image/png", PNG)

    loaded = load_images_for(
        "p1",
        [{"id": first.id, "name": "改前.png"}, {"id": "ffffffff"}, {"id": second.id, "name": "b.png"}],
        root=tmp_path,
    )
    assert [item["name"] for item in loaded] == ["改前.png", "b.png"]
    assert loaded[0]["media_type"] == "image/png"

    # 总量封顶：第一张就超预算时直接一张都不读，而不是读半张坏图。
    capped = load_images_for(
        "p1",
        [{"id": first.id}, {"id": second.id}],
        max_total_bytes=len(PNG),
        root=tmp_path,
    )
    assert len(capped) == 1


# ---------------------------------------------------------------------------
# 文本类与 PDF
# ---------------------------------------------------------------------------


def _make_pdf(text: str) -> bytes:
    """造一份最小但结构完整的单页 PDF。

    不借助任何写库：要验的是"读得出来"，用写库生成再读回来等于自己证明自己。
    手写的 xref 偏移是对的，pypdf 能正常解析。
    """
    body = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode("latin-1")
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 200]/Contents 4 0 R"
        b"/Resources<</Font<</F1 5 0 R>>>>>>",
        b"<</Length " + str(len(body)).encode() + b">>stream\n" + body + b"\nendstream",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, item in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + item + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<</Size {len(objects) + 1}/Root 1 0 R>>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


async def test_text_and_pdf_attachments_are_readable_and_classified(client):
    pid = await _project(client, "文档项目")
    doc = await client.post(
        f"/api/v1/projects/{pid}/uploads",
        json={"name": "需求.md", "mime": "", "data": _b64("# 需求\n把按钮改成蓝色".encode())},
    )
    assert doc.status_code == 201, doc.text
    # 浏览器给 .md 的 type 常常是空串，所以分类必须靠扩展名而不是 mime。
    assert doc.json()["kind"] == "text"
    assert doc.json()["mime"] == "text/markdown"

    pdf = await client.post(
        f"/api/v1/projects/{pid}/uploads",
        json={"name": "规格.pdf", "mime": "", "data": _b64(_make_pdf("Spec A"))},
    )
    assert pdf.status_code == 201, pdf.text
    assert pdf.json()["kind"] == "pdf"


def test_loader_pulls_text_and_pdf_into_prompt(tmp_path):
    from blank_api.services.agent.graph import initial_messages
    from blank_api.services.uploads import UploadStore, load_attachments_for

    store = UploadStore(tmp_path)
    md = store.save("p1", "需求.md", "", "# 需求\n把按钮改成蓝色".encode())
    doc = store.save("p1", "规格.pdf", "", _make_pdf("Spec A"))

    bundle = load_attachments_for(
        "p1",
        [{"id": md.id, "name": "需求.md"}, {"id": doc.id, "name": "规格.pdf"}],
        root=tmp_path,
    )
    assert [item["name"] for item in bundle.texts] == ["需求.md", "规格.pdf"]
    assert "把按钮改成蓝色" in bundle.texts[0]["text"]
    assert "Spec A" in bundle.texts[1]["text"]
    assert bundle.summarize()["used"] == 2

    prompt = initial_messages("按文档改", [], texts=bundle.texts)[-1]["content"]
    assert "用户附了 2 份文件" in prompt
    assert "把按钮改成蓝色" in prompt
    assert "Spec A" in prompt


def test_scanned_pdf_is_reported_instead_of_silently_empty(tmp_path):
    """扫描件抽不出文字是常态。抽不出就要说抽不出，而不是给模型一段空白让它编。"""
    from pypdf import PdfWriter

    from blank_api.services.uploads import UploadStore, load_attachments_for

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    target = tmp_path / "blank.pdf"
    with target.open("wb") as handle:
        writer.write(handle)

    store = UploadStore(tmp_path)
    ref = store.save("p1", "扫描件.pdf", "", target.read_bytes())
    bundle = load_attachments_for("p1", [{"id": ref.id}], root=tmp_path)
    assert bundle.texts == []
    assert bundle.dropped[0]["reason"] == "no_text"


def test_over_limit_images_are_counted_not_hidden(tmp_path):
    """少给的张数必须能被上层看到——不然用户以为模型看了图，其实没有。"""
    from blank_api.services.uploads import UploadStore, load_attachments_for

    store = UploadStore(tmp_path)
    first = store.save("p1", "a.png", "image/png", PNG)
    second = store.save("p1", "b.png", "image/png", PNG)

    bundle = load_attachments_for(
        "p1",
        [{"id": first.id}, {"id": second.id}],
        max_image_bytes=len(PNG),
        root=tmp_path,
    )
    assert len(bundle.images) == 1
    summary = bundle.summarize()
    assert summary["attached"] == 2
    assert summary["used"] == 1
    assert summary["dropped"][0]["reason"] == "over_limit"


# ---------------------------------------------------------------------------
# 回收：删任务清文件、启动扫孤儿
# ---------------------------------------------------------------------------


async def test_deleting_a_task_removes_only_its_own_uploads(client):
    pid = await _project(client, "回收项目")
    kept_ref = (
        await client.post(
            f"/api/v1/projects/{pid}/uploads",
            json={"name": "留下.png", "mime": "image/png", "data": _b64()},
        )
    ).json()
    gone_ref = (
        await client.post(
            f"/api/v1/projects/{pid}/uploads",
            json={"name": "删掉.png", "mime": "image/png", "data": _b64()},
        )
    ).json()

    first = (
        await client.post(
            f"/api/v1/projects/{pid}/tasks",
            json={"title": "要删的", "attachments": [{"id": gone_ref["id"]}]},
        )
    ).json()
    await client.post(
        f"/api/v1/projects/{pid}/tasks",
        json={"title": "要留的", "attachments": [{"id": kept_ref["id"]}]},
    )

    assert (await client.delete(f"/api/v1/tasks/{first['id']}")).status_code == 204
    # 删掉的那份读不到了，另一份还得在：附件不能按"整个项目"清。
    assert (await client.get(f"/api/v1/projects/{pid}/uploads/{gone_ref['id']}")).status_code == 404
    assert (await client.get(f"/api/v1/projects/{pid}/uploads/{kept_ref['id']}")).status_code == 200


def test_sweep_drops_old_orphans_and_keeps_referenced(tmp_path):
    import os
    import time

    from blank_api.services.uploads import UploadStore

    store = UploadStore(tmp_path)
    orphan = store.save("p1", "旧草稿.png", "image/png", PNG)
    alive = store.save("p1", "在用.png", "image/png", PNG)
    fresh = store.save("p1", "刚选的.png", "image/png", PNG)

    old = time.time() - 30 * 86400
    for ref in (orphan, alive):
        os.utime(ref.path, (old, old))

    removed = store.sweep({alive.id})
    assert removed == 1
    # 被引用的不清、刚上传还可能是"用户正在写字"的草稿也不清。
    assert (tmp_path / "p1" / f"{alive.id}.png").exists()
    assert (tmp_path / "p1" / f"{fresh.id}.png").exists()
    assert not (tmp_path / "p1" / f"{orphan.id}.png").exists()
