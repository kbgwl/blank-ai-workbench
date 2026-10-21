# 工作区与图片附件

这份文档回答两个问题：**"它在哪个目录里干活"** 和 **"我贴的图去哪了"**。
两者都是"用户以为自己有、实际没有"的重灾区，所以把边界写清楚。

---

## 一、工作区

### 它是什么

工作区 = 一个**绝对路径**，是这一轮任务真正读文件、改文件、跑命令的根。
后端所有路径校验、快照、回滚、`restrict_to_workspace` 权限、per-task memory
都以它为基准，**同一时刻只能有一个**。

不能有多个活动工作区，原因很具体：回滚要往某个目录里写，两个根同时活着，
回滚就不知道该写哪个。

### 它挂在哪

挂在**项目**上，不是挂在对话上：

```
Project 1 ── n Workspace
              └─ is_active（同一时刻只有一个为 true）
Task.project_id ──► 跑的时候读 project.active_workspace.path
```

任务本身**不存**工作区快照。切换工作区只影响**下一轮**——正在跑的那一轮
已经把根读进去了，中途改写会让它的快照和改动记录对不上。

### 三个入口

| 位置 | 能做什么 |
| --- | --- |
| 项目页 → 工作区面板 | 绑定新目录、解绑、切到这里、点开看真实路径 |
| 对话页 → 输入框下方 | 切换已绑目录；桌面壳里还能直接"选一个新目录" |
| 桌面壳 → 系统对话框 | `dialog.showOpenDialog({properties:['openDirectory']})` |

浏览器里没有系统对话框（`window.blankDesktop` 不存在），所以那一处入口不渲染，
而不是渲染出来点了没反应。

### 为什么路径默认打码

列表接口返回的是 `<workspace:目录名>`，不是真实路径。原因：投屏、截图、
把界面发给别人看的时候，主机上装了什么不该顺手泄露。

需要确认"我绑的是哪个目录"时点一下那一行，走单独一次请求取真路径：

```
GET /projects/{id}/workspaces/{wid}/path   →  {"id": "...", "path": "E:\\work\\repo"}
```

### 接口

```
POST   /projects/{id}/workspaces                 绑定（第二次绑定不自动抢活动位）
POST   /projects/{id}/workspaces/{wid}/activate  切换活动工作区
GET    /projects/{id}/workspaces/{wid}/path      取真实路径
DELETE /projects/{id}/workspaces/{wid}           解绑（解掉活动的会自动补选一个）
```

解绑时**必须**补选：不补的话剩下的 `is_active` 全是 false，界面显示"0 个活动"，
而任务其实按 `workspaces[0]` 兜底在跑——看到的状态和行为对不上的 bug 最难查。

---

## 二、图片附件

### 链路

```
选择 / 拖拽 / Ctrl+V
  → 前端先校验（类型、8MB）
  → base64 JSON 上传，落 data/uploads/<project>/<upload_id>.<ext>
  → 拿到 id，存在草稿里（缩略图用本地 blob URL）
  → 发送时只带 [{id, name}]
  → tasks.attachments 存引用
  → 运行时读成 base64，挂到最后一条 user 消息上
  → 适配器映射成各家格式
```

选完就传、不等发送：用户写字的十几秒里图已经在路上，按回车时只剩一个 id。

### 为什么不放工作区

工作区是**用户的仓库**。把截图写进去会被 git 看见、被快照带上、被目录搜索扫到，
而它既不是用户要提交的东西，也不是这次任务要交付的东西。

### 为什么用 base64 而不是 multipart

multipart 要在服务端多一个 `python-multipart` 依赖，打包链、venv、安装包里那份
运行时都要跟着同步；这条链上任何一环漏掉，表现都是"上传按钮点了没反应"。
代价是请求体大约 1.37 倍，单张限 8MB，付得起。

### 为什么图片不进 state

`state["messages"]` 会被写进 LangGraph 检查点。一张 8MB 的图 base64 之后 11MB，
每条消息存一遍——一次带图的任务就能把检查点表撑大十几兆。

所以：**state 里只有"附了 N 张图"这句文本，真图挂在 `AgentSession.vision_images`
上，出站前才拼进请求对象**，请求发完就随对象丢弃。

### 模型看不懂图怎么办

`supports_vision=False` 时**一张都不挂**（挂上去就是 400，用户看到的是
"传了图就发不出去"）。降级成一句明确的话：

> 用户附了 N 张图片，但当前模型不支持看图，图片内容你看不到。不要猜测图里是什么，
> 也不要说你看到了；在结论里说明：要看图需要切换到支持视觉的模型。

### 四家供应商的映射

| 供应商 | 表示方式 |
| --- | --- |
| OpenAI 兼容 | `content` 变成 part 数组，`{"type":"image_url","image_url":{"url":"data:..."}}` |
| Anthropic | `{"type":"image","source":{"type":"base64",...}}`，图在文本之前 |
| Ollama | 消息上的 `images: [裸 base64]`（没有 part 概念） |
| OpenAI Responses | `{"type":"input_image","image_url":"data:..."}` |

没图时 OpenAI 系保持 `content` 为**字符串**而不是单元素数组：国内几家的兼容层
对数组形式的纯文本支持不全，而纯文本是绝大多数请求。

### 限额

| 项 | 值 | 位置 |
| --- | --- | --- |
| 单张大小 | 8MB | 前后端各一次 |
| 格式 | PNG / JPEG / WebP / GIF | 同上 |
| 每轮张数 | 8 | 同上 |
| 单轮总字节 | 20MB | `load_images_for` |
| token 估算 | 每张 900 | `context.IMAGE_TOKEN_ESTIMATE` |

token 估算宁可高估：低估会让压缩晚一步触发，而那一步的代价是整轮上下文重发。

### 接口

```
POST /projects/{id}/uploads          {name, mime, data(base64)} → {id, name, mime, size}
GET  /projects/{id}/uploads/{uid}    原图（inline，给 <img> 用）
```

`upload_id` 必须是纯十六进制串才参与拼路径——防 `../` 构造读到目录外。

---

## 三、已知边界

- 切换工作区不影响正在跑的那一轮（刻意的，见上）。
- 会话历史（`history_for`）只带文本，不带图：上一轮的图不会重发给模型。
- 压缩折叠如果吃掉了首条 user，那一轮的图也会跟着丢——只影响超长会话。
- 浏览器端没有原生目录对话框；那边只能用项目页手填绝对路径。
