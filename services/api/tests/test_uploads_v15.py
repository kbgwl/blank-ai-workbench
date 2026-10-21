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
