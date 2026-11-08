# 消息通道：填表即用

> 客户在自己机器上填一遍企业微信 / 钉钉 / 飞书的凭据，就能把任务提醒推到手机，
> 也能在那边发一句话让它动手。不需要改代码、不需要重新打包。

入口：**设置 → 消息通道**（`/settings#channels`）。

---

## 1. 这功能到底做了什么

| 方向 | 行为 | 落点 |
| --- | --- | --- |
| 出站 | 任务等审批 / 失败 / 完成时，把一句话推到所有开了「推送」的通道 | `channels/delivery.fanout` |
| 入站 | 通道里来一条文本消息 → 建一条任务 → 与对话页同一个监督者与执行链路 | `channels/inbound.accept` |
| 回话 | 这条任务结束时，把结论回到**原会话**（不看低打扰策略：那是回答，不是打扰） | `channels/delivery.reply_to_source` |

判定与投递仍然分开：`agent/notify.py` 只回答"该不该出声"，`channels/` 只负责发。
两者在 `_record_notify_decision` 收口，各写一条事件（`notify.decided` / `notify.delivered` /
`channel.replied`），所以"策略压掉了"和"发失败了"永远分得开。

## 2. 四种通道、两种接法

| 类型 | 接法 | 出站 | 入站 | 需要公网 |
| --- | --- | --- | --- | --- |
| 企业微信 | 企业应用 | ✅ 应用消息 | ✅ 回调（AES 加密） | **是** |
| 企业微信 | 群机器人 | ✅ | ❌ | — |
| 钉钉 | 企业内部应用 | ✅ 机器人单聊 | ✅ **Stream 长连接** | **否** |
| 钉钉 | 群机器人 | ✅（可加签） | ❌ | — |
| 飞书 | 自建应用 | ✅ | ✅ 事件订阅（AES 加密） | **是** |
| 飞书 | 群机器人 | ✅（可加签） | ❌ | — |
| 通用 Webhook | HTTP 出站 | ✅ 自定义请求体 | ❌ | — |

**桌面版没有公网地址，所以"能收消息"只有两条路**：钉钉 Stream（本机主动连出去），
或者把后端挂到一个公网 https 地址并把 `BLANK_PUBLIC_BASE_URL` 指过去。
界面上会直说这一点，不会给一个填进去必然校验失败的回调地址。

## 3. 配置项

所有字段声明在适配器里（`services/channels/*.py` 的 `fields`），界面表单完全由
`GET /channels/catalog` 生成。加一个字段只需要改后端。

- `secret=True` 的字段**只回显掩码**（`****abcd`）。把掩码原样提交回来按"没改"处理——
  否则客户点一次保存就把密钥存成了那串星号。
- 必填按**接法**算：「群机器人」不会要求填 `AgentId`。
- 配置整份 JSON 用主密钥（`BLANK_SECRET_KEY` / `data/.blank_secret.key`）加密落库，
  与模型供应商 API Key 同一把钥匙。

部署级开关（环境变量）：

| 变量 | 作用 |
| --- | --- |
| `BLANK_CHANNELS_ENABLED` | 总闸。`0` 时出站入站全停（半夜被推醒时比逐条关通道快） |
| `BLANK_PUBLIC_BASE_URL` | 对外地址，决定回调 URL 怎么拼。留空时界面明说"收消息需要公网地址" |

## 4. 为什么入站不受 `background_execution` 管

`background_execution` 是 **BETA 且默认关**，语义是"后台自己发起的事别乱跑"。
但 IM 里的一句话是**人主动发的**——如果它也走那个闸门，客户配好通道发消息会永远
没反应，而界面上一切正常。

所以入站直接调 `worker.release_one(trigger_id)` 入队，**不看**那个开关。
执行路径仍然只有一条：`release_one → launch_task → 队列`。

顺带把 `release_due_triggers` 的那段循环抽成了 `release_one`，定时放行与 IM 入站
共用同一份入队逻辑，不存在第二套。

## 5. 幂等与可追溯

- 去重键 = `im:{channel_id}:{外部消息 id}`，窗口 24 小时。钉钉不 ACK 会重推、
  飞书 3 秒没响应会重投——不去重就是"一句话跑三遍"。
- 每条入站都留一条 `agent_triggers` 行（`event_type=im_message`），带来源、
  外部消息 id、绑定任务 id 与**回话目标**。任务跑完时按这条行找回原会话。
- 入站解析失败写 `channels.last_inbound_error`，同时把原因回给厂商（HTTP 4xx），
  这样客户在厂商后台就能看见红字，而不是"发了没反应"。

## 6. 验证到什么程度（2026-11-08）

真机跑过的（假凭据，验的是请求形状与错误码能不能正确念出来）：

| 通道 | 结果 |
| --- | --- |
| 企业微信 | 真实打到 `qyapi.weixin.qq.com`，回 `errcode=40013 invalid corpid`，界面原样展示 |
| 钉钉 | 真实打到 `api.dingtalk.com`，回「无效的clientId或者clientSecret」 |
| 飞书 | 真实打到 `open.feishu.cn`，回 `code=10003 invalid param` |
| 通用 Webhook | 本地起收件口，收到 `{"text":"【空白格】这是一条连通性测试消息…"}` |
| 企业微信入站 | 自签加密回调 → 200；同一条重投两遍只建 1 条任务；monitor 变「已接通」 |

**未验证**：真实企业微信/钉钉/飞书账号的端到端收发。缺的是客户自己的应用凭据，
不是代码路径——拿到凭据后按第 7 节走一遍即可。钉钉 Stream 的长连接本机只做过
协议层单测（帧格式、ACK、ping/pong），没有真连过钉钉网关。

## 7. 客户接入步骤（以钉钉为例，最快的一条）

1. 开放平台 → 应用开发 → 企业内部应用 → 创建应用，记下 Client ID / Client Secret。
2. 应用能力里添加「机器人」，发布后拿 RobotCode。
3. 机器人 →「消息接收模式」选 **Stream 模式**（不要选 HTTP 回调）。
4. 设置 → 消息通道 → 钉钉 → 企业应用，把三项填进去 → 保存并验证。
5. 应用发布到可见范围，在群里 @ 它说一句话，工作台的「监控」页会多一条待执行。

## 8. 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/channels/catalog` | 可配的类型、字段声明、接法、后台步骤 |
| GET | `/channels` | 已配置的通道 + 真实状态 |
| POST | `/channels` | 新建（保存后不自动验证，由界面触发） |
| PATCH | `/channels/{id}` | 改配置 / 开关 / 落点 |
| DELETE | `/channels/{id}` | 删除 |
| POST | `/channels/{id}/verify` | 真打厂商接口验证凭据 |
| POST | `/channels/{id}/test` | 真发一条测试消息 |
| GET/POST | `/channels/{id}/callback/{token}` | 厂商回调。**不鉴权**，靠不可猜的 token + 厂商签名 |

回调那两条是全站唯一的匿名接口，`tests/test_auth_surface_v34.py` 里显式列了出来——
列出来不是放宽，是承认"这个面的鉴权方式不是 Bearer"。
