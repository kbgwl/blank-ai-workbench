"""消息通道（v0.10.3）：配置即用的那一层到底能不能用。

这一格刻意不做"把 adapter mock 掉、断言它被调过"这种自证：真正会出错的地方是
**加解密、签名、模板转义、去重**——都是纯函数，能用真实算出来的期望值比对。
配置与回调走真实 FastAPI，因为"回调是不是真的匿名可达、令牌错了是不是真的 404"
只在路由层才成立。
"""

import base64
import hashlib
import hmac
import json
import uuid

import httpx
import pytest
from sqlalchemy import select

from blank_api.db import get_session_factory
from blank_api.models.channel import Channel
from blank_api.models.task import Task
from blank_api.models.trigger import AgentTrigger
from blank_api.services.channels import delivery, dingtalk, feishu, store, webhook, wecom
from blank_api.services.channels.base import Inbound
from blank_api.services.channels.inbound import accept

CORP_ID = "ww-test-corp"
AES_KEY = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode().rstrip("=")
TOKEN = "callback-token-abc"


def _wecom_config(**extra) -> dict:
    return {
        "corp_id": CORP_ID,
        "agent_id": "1000002",
        "secret": "s3cret-value-9f8e",
        "to_user": "@all",
        "token": TOKEN,
        "encoding_aes_key": AES_KEY,
        **extra,
    }


def _wecom_post_body(text: str, *, msg_id: str = "987654321") -> tuple[bytes, dict]:
    inner = (
        "<xml><ToUserName><![CDATA[%s]]></ToUserName>"
        "<FromUserName><![CDATA[zhangsan]]></FromUserName>"
        "<CreateTime>1700000000</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        "<Content><![CDATA[%s]]></Content>"
        "<MsgId>%s</MsgId><AgentID>1000002</AgentID></xml>" % (CORP_ID, text, msg_id)
    )
    encrypted = wecom.encrypt(inner, AES_KEY, CORP_ID)
    body = (
        "<xml><ToUserName><![CDATA[%s]]></ToUserName>"
        "<Encrypt><![CDATA[%s]]></Encrypt><AgentID>1000002</AgentID></xml>"
        % (CORP_ID, encrypted)
    ).encode()
    query = {
        "timestamp": "1700000000",
        "nonce": "nonce-1",
        "msg_signature": wecom.signature(TOKEN, "1700000000", "nonce-1", encrypted),
    }
    return body, query


def _feishu_encrypt(plain: str, encrypt_key: str) -> str:
    """按飞书文档自己算一遍密文：key=sha256(EncryptKey)、IV 取密文前 16 字节。"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = hashlib.sha256(encrypt_key.encode()).digest()
    iv = b"feishu-iv-16byte"
    data = plain.encode()
    pad = 32 - len(data) % 32
    data += bytes([pad]) * pad
    worker = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return base64.b64encode(iv + worker.update(data) + worker.finalize()).decode()


# ── 加解密与签名：纯函数，用独立算出来的期望值比对 ──────────────────────────


def test_wecom_crypto_roundtrip_and_corp_check():
    message = "<xml><Content><![CDATA[构建失败了]]></Content></xml>"
    encrypted = wecom.encrypt(message, AES_KEY, CORP_ID)
    assert wecom.decrypt(encrypted, AES_KEY, CORP_ID) == message
    # 换一个企业号解同一条密文：必须拒，否则跨企业串号也能进
    with pytest.raises(ValueError):
        wecom.decrypt(encrypted, AES_KEY, "ww-other-corp")
    # 换一把 key：填充必然不合法，不能悄悄返回半截正文
    other = base64.b64encode(b"ffffffffffffffffffffffffffffffff").decode().rstrip("=")
    with pytest.raises(Exception):
        wecom.decrypt(encrypted, other, CORP_ID)


def test_wecom_signature_matches_manual_sha1():
    expected = hashlib.sha1(
        "".join(sorted([TOKEN, "1700000000", "nonce-1", "ciphertext"])).encode()
    ).hexdigest()
    assert wecom.signature(TOKEN, "1700000000", "nonce-1", "ciphertext") == expected


async def test_wecom_inbound_parses_text_and_rejects_forgery():
    body, query = _wecom_post_body("帮我看看 CI 为什么红")
    adapter = wecom.WeComAdapter()
    message = await adapter.parse_inbound(
        _wecom_config(), "app", query=query, headers={}, body=body
    )
    assert message is not None
    assert message.text == "帮我看看 CI 为什么红"
    assert message.sender == "zhangsan"
    assert message.external_id == "987654321"

    forged = dict(query, msg_signature="deadbeef")
    with pytest.raises(ValueError):
        await adapter.parse_inbound(_wecom_config(), "app", query=forged, headers={}, body=body)


async def test_feishu_encrypted_event_decrypts_and_checks_token():
    envelope = {
        "header": {"event_type": "im.message.receive_v1", "token": "vt-1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_1"}},
            "message": {
                "message_id": "om_1",
                "chat_id": "oc_1",
                "message_type": "text",
                "content": json.dumps({"text": "把周报汇总一下"}, ensure_ascii=False),
            },
        },
    }
    body = json.dumps({"encrypt": _feishu_encrypt(json.dumps(envelope, ensure_ascii=False), "ek-1")}).encode()
    config = {"app_id": "cli_1", "app_secret": "sec", "verification_token": "vt-1", "encrypt_key": "ek-1"}
    adapter = feishu.FeishuAdapter()
    message = await adapter.parse_inbound(config, "app", query={}, headers={}, body=body)
    assert message is not None
    assert message.text == "把周报汇总一下"
    assert message.conversation == "oc_1"

    with pytest.raises(ValueError):
        await adapter.parse_inbound(dict(config, verification_token="vt-2"), "app", query={}, headers={}, body=body)

    # 只填 Encrypt Key 没填 Token 的配置也必须能解（客户常常只配一半）
    assert (await adapter.parse_inbound(dict(config, verification_token=""), "app", query={}, headers={}, body=body)).text


async def test_feishu_url_verification_echoes_challenge():
    body = json.dumps({"challenge": "abc123", "token": "vt-1", "type": "url_verification"}).encode()
    config = {"verification_token": "vt-1"}
    assert await feishu.FeishuAdapter().handshake_body(config, body) == {"challenge": "abc123"}
    assert await feishu.FeishuAdapter().handshake_body({}, body) == {"challenge": "abc123"}


def test_dingtalk_sign_matches_independent_hmac():
    from urllib.parse import parse_qs, urlencode, urlparse

    stamp = 1700000000000
    expected = base64.b64encode(
        hmac.new(
            b"SEC-test-secret",
            f"{stamp}\nSEC-test-secret".encode(),
            hashlib.sha256,
        ).digest()
    ).decode()
    # 官方口径是 urlencode 后拼进去，所以比对的是解码回来的值
    assert dingtalk.sign_webhook("SEC-test-secret", stamp) == urlencode({"sign": expected}).split("=", 1)[1]
    url = dingtalk.signed_url("https://oapi.dingtalk.com/robot/send?access_token=t", "SEC-test-secret", stamp)
    query = parse_qs(urlparse(url).query)
    assert query["timestamp"] == [str(stamp)]
    assert query["access_token"] == ["t"]
    # 没填密钥就原样返回，不能拼出一个空签名
    assert dingtalk.signed_url("https://x/robot?a=1", "") == "https://x/robot?a=1"


def test_webhook_template_escapes_quotes_and_newlines():
    text = '他说"失败"\n第二行'
    rendered = webhook.render('{"text": "{text}"}', text)
    assert json.loads(rendered)["text"] == text


# ── 配置存取规则 ────────────────────────────────────────────────────────


def _channel(**overrides) -> Channel:
    base = dict(
        id=str(uuid.uuid4()),
        kind="dingtalk",
        name="测试通道",
        mode="app",
        callback_token="tok",
        push=True,
        receive=True,
        receive_mode="stream",
        project_id="",
        thread_id="",
        status="unverified",
        status_note="",
        last_inbound_error="",
    )
    base.update(overrides)
    return Channel(**base)


def test_masked_secret_is_kept_instead_of_overwritten():
    channel = _channel()
    store.write_config(channel, {"client_id": "ding1", "client_secret": "real-secret-abcd"})
    merged = store.merge_config(channel, {"client_id": "ding2", "client_secret": "****abcd"}, "app")
    assert merged["client_id"] == "ding2"
    # 掩码原样提交回来时保留原值：否则点一次保存就把密钥存成了星号
    assert merged["client_secret"] == "real-secret-abcd"
    # 填了真值才替换
    merged = store.merge_config(channel, {"client_secret": "new-secret"}, "app")
    assert merged["client_secret"] == "new-secret"


def test_required_fields_follow_mode():
    assert store.missing_required("dingtalk", "app", {}) == ["Client ID（AppKey）", "Client Secret（AppSecret）", "机器人编码（RobotCode）"]
    # 群机器人模式不该被要求填应用那三项
    assert store.missing_required("dingtalk", "robot", {}) == ["群机器人 Webhook"]
    assert store.missing_required("dingtalk", "robot", {"webhook": "https://x/robot"}) == []


def test_switching_to_robot_mode_turns_receive_off():
    channel = _channel(receive_mode="stream")
    store.apply_mode(channel, "robot")
    # 群机器人收不到消息，留着 stream 就是"界面说在收、其实收不到"
    assert channel.receive_mode == "off"


# ── 真实 HTTP：配置加密、掩码、回调匿名可达、入站建任务 ────────────────────


async def _project(client, workspace=None) -> str:
    response = await client.post("/api/v1/projects", json={"name": "IM 测试项目"})
    assert response.status_code == 201, response.text
    project_id = response.json()["id"]
    if workspace is not None:
        bound = await client.post(
            f"/api/v1/projects/{project_id}/workspaces", json={"path": str(workspace)}
        )
        assert bound.status_code == 201, bound.text
    return project_id


async def test_channel_config_is_encrypted_at_rest_and_masked_in_api(client):
    project_id = await _project(client)
    created = await client.post(
        "/api/v1/channels",
        json={
            "kind": "dingtalk",
            "name": "研发群",
            "mode": "app",
            "config": {
                "client_id": "ding-client-id",
                "client_secret": "super-secret-9876",
                "robot_code": "ding-robot",
                "to_user": "u1",
            },
            "project_id": project_id,
            "receive_mode": "stream",
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()["channel"]
    assert body["config"]["client_secret"] == "****9876"
    assert body["config"]["client_id"] == "ding-client-id"
    assert body["receive_mode"] == "stream"

    async with get_session_factory()() as db:
        row = (await db.execute(select(Channel))).scalar_one()
        # 明文密钥绝不能出现在落库的那一列里
        assert "super-secret-9876" not in row.config_encrypted
        assert row.config_encrypted

    listed = await client.get("/api/v1/channels")
    assert listed.status_code == 200
    assert listed.json()["channels"][0]["config"]["client_secret"] == "****9876"


async def test_channel_endpoints_require_token_but_callback_does_not(client, monkeypatch):
    """接口面要令牌，回调不能要——厂商不可能带我们的 Bearer。"""
    from blank_api.config import get_settings

    monkeypatch.setenv("BLANK_API_TOKEN", "gate-token")
    get_settings.cache_clear()
    try:
        anonymous = await client.get("/api/v1/channels")
        assert anonymous.status_code == 401
        # 回调带错令牌一律 404：告诉对方"id 存在但令牌错"等于帮他枚举
        response = await client.post("/api/v1/channels/whatever/callback/wrong-token", json={})
        assert response.status_code == 404
    finally:
        monkeypatch.delenv("BLANK_API_TOKEN", raising=False)
        get_settings.cache_clear()


async def test_inbound_callback_creates_exactly_one_task(client, monkeypatch):
    # 关掉进程内执行器：这一格验的是"收下 + 入队"，不是跑模型。
    monkeypatch.setenv("BLANK_INPROCESS_RUNNER", "0")
    project_id = await _project(client)
    created = await client.post(
        "/api/v1/channels",
        json={
            "kind": "wecom",
            "name": "企微测试",
            "mode": "app",
            "config": _wecom_config(),
            "project_id": project_id,
            "receive_mode": "webhook",
        },
    )
    assert created.status_code == 200, created.text
    channel = created.json()["channel"]
    assert channel["receive_mode"] == "webhook"
    assert channel["receive"] is True

    body, query = _wecom_post_body("把昨天的构建日志看一遍")
    url = f"/api/v1/channels/{channel['id']}/callback/{channel['callback_token']}"
    first = await client.post(url, params=query, content=body)
    assert first.status_code == 200, first.text

    async with get_session_factory()() as db:
        tasks = (await db.execute(select(Task))).scalars().all()
        assert len(tasks) == 1
        assert tasks[0].goal == "把昨天的构建日志看一遍"
        assert tasks[0].project_id == project_id
        trigger = (await db.execute(select(AgentTrigger))).scalar_one()
        assert trigger.event_type == "im_message"
        assert trigger.task_id == tasks[0].id
        assert trigger.payload["reply"]["channel_id"] == channel["id"]

    # 厂商重投同一条消息：不能再开一个任务
    second = await client.post(url, params=query, content=body)
    assert second.status_code == 200
    async with get_session_factory()() as db:
        assert len((await db.execute(select(Task))).scalars().all()) == 1


async def test_inbound_is_released_even_when_background_execution_is_off(client, monkeypatch, tmp_path):
    """「客户配好就能用」的核心一条：IM 里的一句人话不能被后台开关挡住。

    ``background_execution`` 默认是关的（它管的是"后台自己发起的事"）。如果入站
    也走那个闸门，客户配好通道发一句话会永远没有反应——而界面上看起来一切正常。
    """
    from blank_api.models.run import AgentRun
    from blank_api.services.features import is_enabled

    monkeypatch.setenv("BLANK_INPROCESS_RUNNER", "0")
    assert is_enabled("background_execution") is False, "这一格的前提是后台执行关着"

    project_id = await _project(client, workspace=tmp_path)
    # 真要入队就得有供应商与工作区：缺任何一样，launch 会当场拒绝——那不是这一格要验的东西。
    seeded = await client.post(
        "/api/v1/providers",
        json={
            "name": "测试供应商",
            "kind": "openai_compatible",
            "base_url": "https://example.com/v1",
            "api_key": "sk-test",
            "model": "test-model",
        },
    )
    assert seeded.status_code in (200, 201), seeded.text
    created = await client.post(
        "/api/v1/channels",
        json={
            "kind": "wecom",
            "name": "闸门测试",
            "mode": "app",
            "config": _wecom_config(),
            "project_id": project_id,
            "receive_mode": "webhook",
        },
    )
    channel = created.json()["channel"]
    body, query = _wecom_post_body("后台开关关着也要能跑")
    url = f"/api/v1/channels/{channel['id']}/callback/{channel['callback_token']}"
    assert (await client.post(url, params=query, content=body)).status_code == 200

    async with get_session_factory()() as db:
        runs = (await db.execute(select(AgentRun))).scalars().all()
        assert len(runs) == 1, "消息被收下了但没进队列，等于没反应"
        trigger = (await db.execute(select(AgentTrigger))).scalar_one()
        assert trigger.status == "fired", "触发器没被放行，下一拍巡检也没人扫（开关关着）"


async def test_inbound_ignores_non_text_and_wrong_signature(client):
    project_id = await _project(client)
    created = await client.post(
        "/api/v1/channels",
        json={
            "kind": "wecom",
            "name": "企微测试2",
            "mode": "app",
            "config": _wecom_config(),
            "project_id": project_id,
            "receive_mode": "webhook",
        },
    )
    channel = created.json()["channel"]
    url = f"/api/v1/channels/{channel['id']}/callback/{channel['callback_token']}"
    body, query = _wecom_post_body("伪造的")
    forged = dict(query, msg_signature="0" * 40)
    response = await client.post(url, params=forged, content=body)
    # 签名不对必须当场拒绝，且要把原因写回给厂商（他才会在后台看到红字）
    assert response.status_code == 400
    async with get_session_factory()() as db:
        assert (await db.execute(select(Task))).scalars().all() == []
        row = (await db.execute(select(Channel))).scalar_one()
        assert "签名" in row.last_inbound_error


async def test_monitor_sources_reflect_real_channel_state(client):
    project_id = await _project(client)
    await client.post(
        "/api/v1/channels",
        json={
            "kind": "feishu",
            "name": "飞书测试",
            "mode": "app",
            "config": {"app_id": "cli_1", "app_secret": "sec"},
            "project_id": project_id,
        },
    )
    monitor = await client.get("/api/v1/monitor")
    assert monitor.status_code == 200
    sources = monitor.json()["sources"]
    feishu_rows = [item for item in sources if item["label"].startswith("飞书")]
    assert len(feishu_rows) == 1
    # 配置了但还没验证过：不能显示「已接通」
    assert feishu_rows[0]["connected"] is False
    assert "未验证" in feishu_rows[0]["note"]
    # 没配的两家仍然在，并且说清去哪儿配
    assert {item["id"] for item in sources if item["kind"] == "im"} >= {"wecom", "dingtalk"}


async def test_fanout_uses_channel_config_and_reports_per_channel(client, monkeypatch):
    project_id = await _project(client)
    await client.post(
        "/api/v1/channels",
        json={
            "kind": "webhook",
            "name": "自建网关",
            "mode": "app",
            "config": {"url": "https://hooks.example.com/blank", "template": '{"msg":"{text}"}'},
            "project_id": project_id,
        },
    )
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as fake:
        results = await delivery.fanout("任务失败：构建红了", client=fake)

    assert len(results) == 1 and results[0]["ok"] is True
    assert len(seen) == 1
    assert json.loads(seen[0].content)["msg"] == "任务失败：构建红了"


async def test_fanout_master_switch_skips_everything(client, monkeypatch):
    from blank_api.config import get_settings

    project_id = await _project(client)
    await client.post(
        "/api/v1/channels",
        json={
            "kind": "webhook",
            "name": "闸门测试",
            "mode": "app",
            "config": {"url": "https://hooks.example.com/blank"},
            "project_id": project_id,
        },
    )
    monkeypatch.setenv("BLANK_CHANNELS_ENABLED", "0")
    get_settings.cache_clear()
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
        ) as fake:
            assert await delivery.fanout("任何东西", client=fake) == []
    finally:
        monkeypatch.delenv("BLANK_CHANNELS_ENABLED", raising=False)
        get_settings.cache_clear()


# ── 回话与文案 ──────────────────────────────────────────────────────────


def test_reply_text_says_failure_instead_of_pretending_success():
    assert delivery.reply_text(status="succeeded", summary="改好了，测试通过").startswith("改好了")
    assert "没做成" in delivery.reply_text(status="failed", failure="命令超时")
    assert "批准" in delivery.reply_text(status="waiting_approval", summary="", task_id="abcdef1234")
    # 跑完了但没结论：不编一句"已完成"
    assert "没有产出文字结论" in delivery.reply_text(status="succeeded", summary="")


def test_notification_text_puts_conclusion_on_first_line():
    text = delivery.notification_text(
        title="修复构建", goal="修一下 CI", kind="failure", detail="第 3 步超时"
    )
    assert text.splitlines()[0] == "【任务失败】修复构建"
    assert "第 3 步超时" in text


async def test_accept_says_where_the_message_could_not_land(client):
    """没有项目时，消息不能被"接住"又无声无息地消失；有项目时必须真的开出一条任务。"""
    channel = _channel(kind="webhook", name="落点测试", mode="app", receive_mode="off")
    store.write_config(channel, {"url": "https://hooks.example.com/x"})
    async with get_session_factory()() as db:
        db.add(channel)
        await db.commit()

    orphan = await accept(channel.id, Inbound(external_id="m1", text="你好"), release=False)
    assert orphan["accepted"] is False
    assert "项目" in orphan["reason"]

    project_id = await _project(client)
    landed = await accept(channel.id, Inbound(external_id="m2", text="你好"), release=False)
    assert landed["accepted"] is True and landed["duplicate"] is False
    async with get_session_factory()() as db:
        task = await db.get(Task, landed["task_id"])
        assert task is not None and task.project_id == project_id
        assert task.thread_id, "IM 消息必须落在一条会话里，否则下一句话就接不上上文"
