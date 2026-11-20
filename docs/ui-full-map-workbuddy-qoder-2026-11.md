# 全量界面地图与逐域对照（WorkBuddy / Qoder → 空白格，2026-11）

> 上一份 `ui-reference-workbuddy-qoder-2026-11.md` 只做了四个专题（审批 / 计划 / 状态 / 密度）。
> 这一份把两个产品的**全部面板与全部功能域**过一遍，一个不抽样，然后逐域摆上我们的现状。

**读法**：每域固定四段——界面元素 / 信息组织 / 状态覆盖 / 我们的现状 + 差距。最后一张表回答"先改哪个"。

---

## 0. 一句话结论

两个产品都不是"功能更多"，而是**把同一件事拆成了更多可命名的状态**：WorkBuddy 给 100+ 个工具 × 4 个阶段各写了一句人话（`progress.phase.*`），Qoder 给 25 条失败原因各写了一句下一步（`automation.detail.reason.*`）。
我们的功能域覆盖并不差（15 域里有 13 域有实现），差的是**每个域只做了"有"，没做"全"**：状态少一档、失败少一句、入口少一个。

---

## 1. 先把"全部"定义清楚

### 1.1 两个产品的"全部"边界

| | WorkBuddy | Qoder |
|---|---|---|
| 可枚举单位 | **一个面板 = 一个分包**（文件名即组件名） | **一个命名空间 = 一个域**（16 个） |
| 本次盘点到的单位 | `renderer/assets/*.js` 人工去掉图标/字体/语言包/高亮主题后，**约 190 个界面相关分包**（估计值；包里混了数百个 lucide 图标单文件，无法用脚本精确切分） | `index-DDa3WwIe.js` 中 **16 个 `*Resources` 中文块**，合计约 1.2 万条字符串（`settingsResources` 一个块 4176 条） |
| 界面是否可穷举 | 是（组件名 + CSS 类名 + i18n key 三重可查） | 部分是（Tailwind 原子类，只能靠 i18n 块反推结构） |

复现命令：

```bash
# WorkBuddy：组件名清单（一行一个域）
node "D:/kbg harness/asar.mjs" list "D:/Softwares/WorkBuddy/resources/app.asar" "renderer/assets/" \
  | grep -E '\.js$' | sed -E 's/-[A-Za-z0-9_-]{8}\.js$//' | sort -u

# Qoder：16 个中文命名空间
node -e 'const t=require("fs").readFileSync(".../index-DDa3WwIe.js","utf8");
  console.log([...t.matchAll(/([A-Za-z_$][\w$]*)=\{zh:\{/g)].map(m=>m[1]).join(" "))'
```

### 1.2 15 个域的覆盖矩阵（本文件逐条打勾）

| # | 域 | WorkBuddy | Qoder | 我们 | 本文章节 |
|---|---|---|---|---|---|
| 1 | 会话与对话 | ✅ 消息流/队列/引用/重放 | ✅ 编辑重发/回退 | ✅ 部分 | §3.1 |
| 2 | 任务与作业 | ✅ 卡片/后台/队列 | ✅ 任务监控/子智能体 | ✅ | §3.2 |
| 3 | 计划与待办 | ✅ PlanBoard + 确认卡 | ✅ Spec + 确认卡 | ⚠️ 无确认门 | §3.3 |
| 4 | 审批与权限 | ✅ 4 档 + AI 审批 | ✅ 6 种资源卡 | ✅ 3 档 | §3.4 |
| 5 | 提问与澄清 | ✅ 浮层多题 | ✅ 提问卡 | ✅ | §3.5 |
| 6 | 产物与文件 | ✅ 产物 tab 条 | ✅ 文件变更卡 | ⚠️ 无归集 | §3.6 |
| 7 | 侧栏与导航 | ✅ 分组/置顶/消息中心 | ✅ 四种分组方式 | ⚠️ 只有日分组 | §3.7 |
| 8 | 空态与首次使用 | ✅ 四种模式 + 推荐 | ✅ 热力图 + 涂鸦 | ⚠️ 四张卡片 | §3.8 |
| 9 | 错误与失败 | ✅ 错误码面板 | ✅ 25 条原因 | ⚠️ 一行 reason | §3.9 |
| 10 | 设置 | ✅ 18 个面板 | ✅ 含"任务监控"开关 | ⚠️ 7 段 | §3.10 |
| 11 | 记忆与个性化 | ✅ 本地/云端/导入 | ⚠️ 只有入口 | ⚠️ localStorage | §3.11 |
| 12 | 技能与工具 | ✅ 市场/安全扫描 | ✅ 市场/来源 | ✅ | §3.12 |
| 13 | 连接器与 MCP | ✅ 能力分级授权 | ✅ 市场型 | ⚠️ 只有表单 | §3.13 |
| 14 | 定时任务与自动化 | ✅ 25 条失败原因 | ✅ 模板/输出模式 | ❌ 只有触发器 | §3.14 |
| 15 | 多 Agent / 协作 | ✅ 助理 + 项目 | ✅ 成员 + 小队 | ⚠️ 群聊 | §3.15 |

"该产品无此域"出现 0 次——两个产品 15 域全覆盖，Qoder 最弱的是记忆（只有入口，没有管理界面）。

---

## 2. 全量面板清单

### 2.1 WorkBuddy：按域归类的面板级组件（文件名 = 组件名）

**对话与消息**
`ConversationFlowPanel`（对话流详略）· `Message` · `conversation-message-output-hooks` · `markdown-preview` · `PreviewIframe` · `taskArtifacts`（产物槽位）

**任务与后台**
`task-preview` · `background-tasks.module` · `background-task-terminal-output` · `task-transfer-popover` · `share-task-dialog` · `project-task-source-chip`

**计划与待办**
`plan-host` · `exit-plan-mode-floating` · `high-credit-approval-floating-panel` · 待办清单（无独立分包，文案命名空间 `todoList.*`）

**审批与安全**
`high-credit-approval-rules` · `permission-preference-service` · `SecurityCenterPanel` · `McpPoiConsentCard` · `browser.approval`

**提问**
`ask-user-question`（BEM 类）· `ask-followup-question` · `colleague-acp-question`

**产物预览（每个都是独立分包，说明是重型按需面板）**
`docx-preview-component` `pptx-preview-component` `sheet-preview-component` `pdf-preview-component` `excalidraw-preview-component` `html-artifact-preview` `image-preview-component` `video-preview-component` `audio-preview-component` `miora-preview-component` `drawio-preview` `standalone-artifact-panel`

**侧栏与消息中心**
`sidebar-next`（`__main-header` / `__body` / `__overview-popover` / `__collapse-toggle--pin`）· `msg-center-bell-btn` · `msg-center-badge`

**设置（18 个 Panel，按四个分组）**
设置组：`GeneralSettingsPanel` `ModelsSettingsPanel` `PersonalizationPanel` `ClawSettingsPanel` `SubscriptionUsagePanel`
功能组：`ExtensionsSettingsPanel` `skills-panel` `connectors-panel` `ConversationFlowPanel` `BuddyAppsPanel` `MemorySettingsPanel` `LaboratoryPanel`
数据与安全组：`SecurityCenterPanel` `DataManagementPanel` `DataImportSettingsPanel` `DeviceConnectionsPanel`
关于组：`GetHelpPanel` `HelpFeedbackPanel` `AccountPanel`

**技能 / 连接器 / 自动化**
`skills-panel` `skill-picker` `skill-market-detail` `builtin-skill-picker` `skill-security-review-bus` · `connector-capsule` `connector-device-code-modal` `connector-token-config-gate` `open-deeplink-connector-store` · `automation`（任务卡）`automation-run-group-edit-dialog` `automation-sidebar-edit-dialog`

**协作与同事**
`colleagues-panel` `collaborator-avatar-stack` `collab`（项目）`teams-task-join-request-actions` `task-invitation`

**空态 / 状态 / 错误（被复用最多的底座）**
`StatusPlaceholder`（info/warning/error/loading + retry）· `Loading` · `errors` · `toast` `toast-manage` `quota-toast` `connector-runtime-toast` `settings-save-error-toast` `login-error-toast`

### 2.2 Qoder：16 个命名空间对应的域

| 命名空间 | 字节偏移 | 对应的界面域 |
|---|---|---|
| `shellResources` | @5589012 | 外壳 / 窗口级 |
| `desktopPetResources` | — | 桌面宠物（彩蛋） |
| `accountResources` | @5625840 | 账号与用量 |
| `permissionModeLabels` | @5634725 | 输入框里的权限模式 |
| `permissionResources` | @5634897 | 审批（6 种资源卡 + 计划确认） |
| `newChatResources` | @5641573 | 新任务空态 / 热力图 / 内置小游戏 |
| `chatSessionResources` | @5690466 | 任务列表 / 编辑重发 / 图片预览 |
| `chatTimelineResources` | @5759840 | 对话流（836 条，工具 × 状态） |
| `sidebarResources` | @5788461 | 侧栏（分组/排序/筛选） |
| `workspaceResources` | @5793614 | 工作区 / Workspace Actions |
| `collaborationResources` | @5838778 | 成员 / Attention 收件箱 |
| `automationResources` | @5892324 | 自动化（450 条） |
| `extensionsResources` | @5907899 | 扩展市场（技能/插件/连接器） |
| `settingsResources` | @5963860 | 设置（4176 条，含任务监控开关） |
| `feedbackResources` | @6179491 | 反馈 |
| `resources` | @6182449 | 通用 |

> Qoder 的域比 WorkBuddy 少一半，但**每个域更深**：`chatTimelineResources` 一个块 836 条，等于 WorkBuddy 的 `progress.phase.*` + `tool.*` + 消息操作三块之和。

---

## 3. 逐域对照

每域格式：**① 界面元素 ② 信息组织 ③ 状态覆盖 ④ 我们的现状与差距**。

**锚点简写（本节通用，与上一份报告一致）**

| 写法 | 实际指向 |
|---|---|
| `§key.path` | `wb:renderer/assets/zh-cn-J68R5HFz.js` 里的这个 i18n key（行号见正文；该文件 479 行，大量 key 挤在 470–479 行） |
| `§.class-name` | 该域对应 CSS 分包的第 1 行（压缩产物，1 行几十万字符），用 `grep -o` 可复现 |
| `wb:<文件>:1 §Symbol` | 已解包的 `renderer/assets/` 下该 JS 文件第 1 行里的符号名 |
| `qd:…@<offset> §ns` | `qd:out/renderer/assets/index-DDa3WwIe.js` 中该命名空间的字节偏移（用于 `slice(offset)` 复现） |
| `web/src/…:行` | 我们自己的仓库，行号精确 |

### 3.1 域 1：会话与对话

#### ① WorkBuddy 的界面元素

| 元素 | 锚点 |
|---|---|
| 消息容器 / 头像 / 角色 / 时间 | `wb:renderer/assets/*.css:1 §.message-container .message-avatar .message-role .message-time` |
| 一条消息的操作容器 | `wb:…:1 §.message-actions-container` |
| **划选文字浮出的工具条**（复制 / 添加到对话 / 快速问答） | `wb:renderer/assets/common-p2lSs_-n.js:1 §MessageQuoteSelectionOverlay`，类 `.message-quote-toolbar__action` |
| 引用注解（给引用加一句话） | `wb:…:1 §.message-quote-annotation__input` / `__done--active` |
| 流式光标 | `wb:…:1 §.message-content` 内联 caret |
| 消息队列（排队 / 编辑 / 立刻发送） | `wb:renderer/assets/zh-cn-J68R5HFz.js:477 §collab.task.queue.title / .sendNowTitle / .editTitle` |
| 队列快捷键提示 | `wb:…:477 §chat.input.insertQueueShortcutTooltip`＝"{sendShortcut} 发送，{insertQueueShortcut} 插入队列" |
| 用户消息「重放」「回退到此版本」 | `wb:…:472 §userMessageActions.resend / .resendTooltip / .revertToThisVersion` |
| 停止生成 | `wb:…:472 §settings.keyboardShortcuts.actions.stopGeneration` |

#### ② 信息组织

- 一次交互 = 用户气泡（右）+ 助手整段正文（左），**中间没有卡片墙**；工具过程收进一层折叠。
- 划选即出工具条，三个动作按"轻→重"排：复制 → 添加到对话（带引用 chip 回到输入框）→ 快速问答（另开一条不打断当前）。
- 引用上限 20 条（`common-p2lSs_-n.js` 里 `d>=20 return`），超了静默丢弃 —— 有上限但不提示，是它的一处糙。
- 运行中按回车＝**插入队列**（默认排队，可在设置里改成"立即发送"直接插话）：`wb:…:477 §settings.conversationFlow.queueSendMode.queueHint / .immediateHint`。
- 队列消息可编辑、可"立刻发送"、可删；只允许改自己发的（`§collab.detail.queue.error.editForbiddenNotSender`）。

#### ③ 状态覆盖

| 状态 | 表现 | 锚点 |
|---|---|---|
| 生成中 | 光标 + 逐字 | `.message-content` |
| 队列中 | "队列中"，可展开 | `§collab.task.queue.title` |
| 队列已满 | "消息队列已满…" | `§collab.detail.chat.toast.queueFull` |
| 消息已被自动发出（改晚了） | "该消息已自动发出，修改未生效" | `§collab.detail.queue.info.editMessageSent` |
| 消息已不在队列 | "这条消息已不在队列中…" | `§collab.detail.queue.info.editMessageGone` |
| 队列被别人改了 | "队列已被其他成员修改，已为你刷新" | `§collab.detail.queue.error.versionConflict` |
| 中断 | "任务被中断" | `§conversation.assistantMessage.interrupted` |
| 停止原因（5 种） | maxTokens / maxTurnRequests / refusal / network / authError | `§error.stopReason.*` |

#### ④ Qoder 的做法

- **编辑并重新发送 + 文件回退**：`editRewindConfirmTitle:"回退文件修改并重新发送？"`，并区分四种失败：`editFailed` / `editPartial`（文件没完全恢复）/ `editLocalSyncFailed` / `editReplacementFailed`（`qd:…index-DDa3WwIe.js:1 @5690466 §chatSessionResources`）。
- 一轮的折叠标题就是摘要，含"执行工具 N 次，其中 M 次失败"（`@5759840 §chatActivity.completedToolsWithFailures`）。
- 消息锚点导航：`anchorNav:"消息锚点"` / `scrollToLatest`（`@5759840`）。
- 没有划选工具条，没有消息队列。

#### ⑤ 我们的现状

| 我们有的 | 位置 |
|---|---|
| 用户气泡 + 附件（图/PDF/文件）回放 | `web/src/components/bot/turn.tsx:365-406` |
| 流式输出 + 光标 | `turn.tsx:498-503` |
| 「在想什么」动词 + 秒数 | `turn.tsx:49-72`（`thinkVerb` + 计时） |
| 状态徽标（6 态） | `turn.tsx:74-122` |
| 单按钮 发送/停止 | `composer.tsx:651-666` |
| 运行中回车 = 插话（steer） | `composer.tsx:342-349` |
| 失败/取消 → 「用同样的话再试一次」 | `turn.tsx:560-568` |

**缺的**：划选引用、消息级复制、编辑重发、队列、（文件）回退到此版本。

**差距判定**：**有但状态不全 + 少 3 个入口**。
- 我们的"插话"其实是 Qoder 的 steer，但 WorkBuddy 把"插话"和"排队"做成了**用户可选**的两条路（`conversationFlow.queueSendMode`），我们只有一条，且没有队列可回看/可改。
- 划选引用是"把上下文说准"的最低成本手段，我们完全没有；用户只能靠 `@文件` 或重打一遍原话。

---

### 3.2 域 2：任务与作业

#### ① WorkBuddy 的界面元素

| 元素 | 锚点 |
|---|---|
| 任务卡（标题/状态/时间/来源/工作模式） | `wb:…:477 §taskHoverCard.localTask / .cloudTask / .workMode / .designMode / .workspace / .updatedAt` |
| 后台任务胶囊（输入框上方，`{count} 个后台任务运行中`） | `wb:…:478 §backgroundTask.capsuleLabel / .capsuleLabelCompleted` |
| 后台任务面板（标题/列表/详情 tab/终端预览） | `.background-tasks` CSS：`._panelHeader_` `._panelList_` `._detailTabs_` `._terminalPreview_` `._taskCancel_` `._taskDeleteConfirm_` |
| 并行/队列：`排队中，等待空闲后执行` | `§automation.row.queued` |
| 任务转交 | `task-transfer-popover`（110KB）+ `§collab.task.handoff.pendingMessageQueued` |
| 任务分享 | `share-task-dialog` |
| 任务启动器（目录/仓库/分支） | `§taskStarter.selectDirectory / selectRepo / selectBranch` |

#### ② 信息组织

- 任务有三种"运行位置"，同一张卡上用图标区分：本地电脑 / 云电脑 / 代码开发（`§taskHoverCard.*`）。
- 后台任务是**胶囊 + 面板**两级：胶囊只说数量，点开才是列表；列表项可取消，取消要二次确认；点进详情有 tab（输出/终端）。
- 长任务的进度不是百分比，而是"已运行 N 分钟 + 当前在做什么"。[推测]（依据：`§automation.row.inProgressWithProgress` 是唯一带分子分母的，任务卡本身没有进度条）

#### ③ 状态覆盖（`taskStatus.*`，8 态 + 描述）

| 状态 | 标签 | 描述（原文） | 锚点 |
|---|---|---|---|
| planning | 规划中 | Agent 正在分析任务并制定执行计划 | `zh-cn:374 §taskStatus.planning.label/.description` |
| working | 执行中 | Agent 正在执行计划中的步骤 | 同上 |
| pending | 等待输入（tag：待确认） | 需要你提供更多信息后才能继续 | 同上 |
| completed | 已完成 | 任务已结束（成功完成或被用户终止） | 同上 |
| terminated | 异常中断 | 任务异常退出 | 同上 |
| failed / error | 执行失败 | 任务执行过程中发生错误 | 同上 |
| archived | 已归档 | 任务已被归档 | 同上 |

后台任务另有 5 态：`running / completed / failed / cancelled / pending`（`§backgroundTask.*`）。

#### ④ Qoder 的做法

- 「任务监控卡片」是**可配置的**：用户在设置里逐项开关要显示什么面板 —— 任务回顾 / 任务目标 / 计划 / 子智能体 / 后台进程 / 侧边聊天 / Skill 与 MCP / 产出 / 网页查阅 / 来源（`qd:…@5963860 §settingsResources` 的 `title:"任务监控"` 段落）。
- 子智能体有独立入口与运行状态（同上 `title:"子智能体"`）。
- 任务级状态：`pending/in_progress/completed/blocked/queued/preparing/thinking/usingTool/waitingConfirmation/background/stopping`（`@5690466 §chatSessionResources`）。

#### ⑤ 我们的现状

| 我们有的 | 位置 |
|---|---|
| 任务列表 + 状态筛选 | `web/src/components/tasks/tasks-view.tsx`、`task-list-pane.tsx` |
| 状态元数据（queued/draft/running/waiting_approval/paused/succeeded/failed） | `tasks/status.ts:19-60` |
| 任务控制台（右栏 4 tab：文件/Diff/终端/证明） | `tasks-view.tsx:420-424` |
| 后台任务收进顶部状态条一格 | `bot/goal-status-bar.tsx` + `bot/background.ts` |
| 子任务展示（`spawn_agent` → "分派子任务"） | `tasks/tool-display.ts:256-262` |
| 并行组测试 | `tasks/parallel.test.ts` |

**缺的**：任务卡三行信息（来源/位置/更新于）、后台任务独立面板（我们只有一格 chip，点进去是会话，不是"任务清单"）、任务转交、任务分享、可配置的任务监控项。

**差距判定**：**有但位置不对**。
后台任务在人家那里是"可管理的东西"（列表、取消、看终端输出），在我们这里只是"一条能点走的提示"。任务一多，用户没有任何地方能一眼看全"现在有几个在跑"。

---

### 3.3 域 3：计划与待办

#### ① WorkBuddy 的界面元素

| 元素 | 锚点 |
|---|---|
| 计划看板宿主（独立 tab，remote 模块） | `wb:renderer/assets/plan-host-DWYVBG7L.js:1 §installPlanBoardHost`；样式 `.project-plan-federation-host` |
| **退出计划模式确认浮层** | `wb:renderer/assets/exit-plan-mode-floating-BBsMz-w8.js:1 §ExitPlanModeFloating` |
| 浮层内的计划正文（路径 + 可滚动内容，max-height 220px） | `exit-plan-mode-floating-D4Toe2nG.css:1 §__plan / __plan-path / __plan-content` |
| 两项编号选项（1 开始执行 / 2 告诉它怎么调整） | 同 CSS `§__option / __option-num / __option-arrow` |
| 内联"调整意见"输入行（不占编号） | 同 CSS `§__adjust-row / __adjust-input` |
| 决定后的收尾态（已批准 / 调整计划中） | 同 CSS `§__decision--approved / --keep-planning` |
| 待办清单（任务标题/描述/状态/增删改） | `zh-cn:474 §todoList.taskTitle / .status.waiting/.inProgress/.completed/.error / .newTask / .noTasks / .taskCount` |
| 计划里要申请的权限清单 | `zh-cn:477 §tool.exitPlanMode.permissionsRequested`＝"请求 {count} 项权限：" |

#### ② 信息组织

- 计划确认是**一张卡**，不是一个页面：标题"确认按此计划执行？" + 一句说明 + 计划正文（可滚）+ 两条编号选项 + 一行"或者直接说怎么改"。
- 编号 1/2 对应键盘 1/2，Enter 默认选 1；调整意见走 Enter 发送、Shift+Enter 换行。
- 批准后**顺手把权限模式切到 bypassPermissions**（`exit-plan-mode-floating…js` 里 `await a.setMode(t,'bypassPermissions')`）——"批准了计划"和"别再问我"是同一件事，不用用户再点一次。
- 计划板和待办是**两个 tab**，同一时刻只允许挂一份（`installPlanBoardHost` 里对重复注册打 warn）。[推测：tab 结构来自文案与宿主布局，PlanBoard 本体是 remote 模块，包里没有]

#### ③ 状态覆盖

`pending`（等你）→ `approved`（已批准，开始执行…）/ `keep-planning`（调整计划中…）；另有 `modeSyncFailed`："切换执行模式失败，会话可能已被回收，请重新发送消息以继续"（`§tool.exitPlanMode.modeSyncFailed`）。
待办 4 态：`waiting / inProgress / completed / error`。

#### ④ Qoder 的做法

- 计划本体在 **Spec 面板**，卡上只给"查看详情"；卡本身有三个动作：`approveChoice:"是，实施此计划"` / `requestChanges:"告诉 Qoder 计划需要如何调整"` / `skip:"暂不实施"`（`qd:…@5634897 §permissionResources.exitPlan`）。
- 计划可复制、可下载（`qoder-plan.md`）、可展开/收起（`.copyPlan / .downloadPlan / .expandPlan`）。
- 计划读不到时给替代路径：`planUnavailable:"暂时无法读取计划详情，可打开 Spec 面板查看或让 Qoder 调整计划"`。

#### ⑤ 我们的现状

| 我们有的 | 位置 |
|---|---|
| 计划卡片（里程碑分组、进度条、逐项勾选） | `tasks/plan-panel.tsx:38-241`、`tasks/plan.ts §groupByMilestone` |
| 对话内计划卡（左线挂靠、只折尾部、`停在第 N 步等你批准`） | `bot/turn.tsx:582-654` |
| 时间线里的计划块（进度、里程碑、百分比） | `tasks/timeline-blocks.tsx:113-160` |
| 进入/退出计划模式作为系统事件 | `tasks/timeline.ts`（`enter_plan_mode` / `exit_plan_mode`） |

**缺的**：**计划确认门**。我们 `exit_plan_mode` 只渲染成一条"已退出计划模式"的系统块（`timeline.test.ts:257-266` 断言的就是这句），也就是说模型退出计划模式时**不需要用户点任何东西**——计划是不是该执行，界面没有问过。

**差距判定**：**没有这个功能**（关键的一段）。
这是 15 域里唯一一处"人家把它做成了一个必须跨过的门，我们做成了日志"。

---

### 3.4 域 4：审批与权限

#### ① WorkBuddy 的界面元素

| 元素 | 锚点 |
|---|---|
| 审批卡三选项 + 拒绝 | `zh-cn:472 §tool.approval.allow / .alwaysAllow（本次会话始终允许）/ .alwaysAllowThisLaunch（本次启动内始终允许）/ .reject` |
| 卡片标题问句 | `§tool.riskReview.allowTitle`＝"允许执行 {tool}？" |
| 工具级快捷三件套 | `§tool.executeCommand.allow / .alwaysAllow / .reject` |
| 高危积分审批浮层 | `high-credit-approval-floating-panel-DemeEjgI.js` |
| 权限模式（输入框内） | `§input.wbAdd.permission` |
| **安全中心的五层** | `§settings.securityCenter.section.sandboxSecurity / .dataSecurity / .runtimeSecurity / .audit / .securityReview` |
| AI 智能审批三模式 | `§settings.securityCenter.assistedApproval.mode.off / .riskHint / .silent` |
| 模型行为智能审查 + 自定义规则 | `§settings.securityCenter.modelBehaviorReview.*`、`customRulesDescription`（最多 N 条，每条 M 字符） |
| 工具内容注入防护 | `§settings.securityCenter.riskContentProtection.*` |
| 工具管控（Tool / MCP 逐个开关，必要工具单列） | `§settings.securityCenter.toolControl.*` |
| 审计中心（拦截/放行记录、日志导出） | `§settings.securityCenter.section.audit` + `§securityCenter.audit.aiReview.outcome.deny` |
| 企业策略只读态 | `§settings.securityCenter.managedBanner`＝"安全中心已由管理员统一配置，不可修改" |

#### ② 信息组织

- 卡片上「拒绝」不是红色 —— 拒绝才是安全动作，红色留给危险动作。
- 三档范围按"影响半径"从近到远排：这次 / 本次会话 / 本次启动；**范围越大越靠后**，且带一句"这样会免问到什么程度"。
- 安全是**五层**：沙箱（能不能读写文件）→ 数据安全 → 运行时（企业下发的 AgentDR 内容检测，只读展示）→ AI 审查（模型行为 + 内容注入）→ 审计（事后可查）。
- 企业管控时整页变只读并解释原因，而不是把控件藏起来。

#### ③ 状态覆盖

| 状态 | 表现 |
|---|---|
| 待批 | 卡片 + 选项 |
| 已批/已拒 | `decided`，不再出选项 |
| 过期 | 见 §3.9 的 `approval_expired` 处理（我们更细） |
| 部分授权（连接器能力级） | `§connectorPanel.partiallyConnected`＝"已连接 · 部分功能可用" + `grantMorePermissions`＝"补充授权" |
| 企业禁用 | `§skills.security.policy.forbidHighRisk` |
| 审查超时 | 审查失败不阻塞，回落人工（`§settings.modelBehaviorReview` 段） |

#### ④ Qoder 的做法（6 种资源卡，各写各的话）

| 卡 | 标题 | 选项 |
|---|---|---|
| 普通 | `允许"{tool}"？` | 拒绝 / 允许 / 允许一次 |
| MCP | `允许 MCP 工具 {tool}？` | 同上 |
| PAT/令牌 | `Agent 需要以下权限完成任务`（风险等级 低/中/高） | 允许本次 / 本次任务允许 / 始终允许 |
| 飞书 | 浏览器内完成授权 + 授权链接 + 倒计时 | 拒绝授权 / 终止任务 |
| 目录 | `允许连接目录？` | — |
| 重复调用 | `{tool} 工具已使用相同参数连续调用 {count} 次，可能陷入循环` | 继续本次调用 / 终止任务 |

（全部出自 `qd:…@5634897 §permissionResources`）

Qoder 还把"记住"做成**运行时算出的追加建议**（`§suggestion.rules.add/replace/remove`），而不是固定的第三按钮（上一份报告已查实，此处不重复）。

#### ⑤ 我们的现状

| 我们有的 | 位置 |
|---|---|
| 审批卡：问句 + 命令原文 + 风险等级 + 审查备注 | `bot/turn.tsx:124-202` |
| 三档范围（once / prefix / tool）+ 拒绝并写理由 | `tasks/approval.tsx:38-169`、`api.ts:1311-1346` |
| 键盘 1..N + Esc 拒绝（仅在卡获得焦点时） | `approval.tsx:57-72` |
| 过期卡：不给死按钮，只给"重跑这一轮" | `turn.tsx:164-178` |
| 档位警告（"本会话档：…"） | `turn.tsx:189-199` |
| 独立审查 guardian（off / 只收紧 / 可代批） | `settings/agent-runtime-panel.tsx:46-62` |
| 能力开关 + 路径命令边界 | `settings/safety-panel.tsx:292-458` |

**缺的**：AI 审批的"风险提示但自动放行"档（我们有 veto/auto，语义接近，但没把"仅提示"单列）、审计中心（拦截/放行记录表）、工具管控（逐 Tool 开关）、内容注入防护、"本次启动内始终允许"这一档。

**差距判定**：**有但状态不全 + 少一个事后视角**。
卡本身我们做得比两家都细（过期、审查备注、档位警告），但**审批之后没有任何地方能查"上周它想干什么被我拒了"**——人家有审计表。

---

### 3.5 域 5：提问与澄清

#### ① WorkBuddy 的界面元素

| 元素 | 锚点 |
|---|---|
| 提问工具卡（折叠） | `common-EkSkH3Q0.css:1 §.ask-user-question` + `__header/__title/__icon/__arrow/__content` |
| 状态类 | `§--waiting / --completed / --skipped / --cancelled / --failed / --expanded` |
| 多题浮层（浮在输入框上方） | `src-CMAOF5gI.js:1`：按题遍历（`L` 题、`R` 当前题、`ee` 是否最后一题）、多选 checkbox（`--cb-question-floating-checkbox-*`）、自由输入追加（`V()` 里把自由文本并入选项） |
| 追问生成（另一个模型先想问题） | `zh-cn:§tool.askFollowup.generating / .cancelled / .skipped / .failed` |
| 问答结果回放 | `§tool.askUserQuestion.waiting`＝"等待您的回答..."、`.skipped`＝"您跳过了此问题"、`.cancelled`、`.failed` |

#### ② 信息组织

- 一次提问可以有多题，**逐题走**，最后一题才出现"提交"；每题都可多选 + 自由补充。
- 答完不消失：工具卡保留为一等公民，点开能重看"当时问了什么、我选了什么"。
- 跳过是一等状态（`skipped`），不是"没回答"。

#### ③ 状态覆盖

`waiting`（等待您的回答…）/ `completed` / `skipped`（您跳过了此问题）/ `cancelled`（问题已取消）/ `failed`（获取回答失败）/ `expanded`（展开回看）。

#### ④ Qoder 的做法

`chatActivity.question` 命名空间（上一份已查实），选项式提问 + 自由输入，无逐题向导。

#### ⑤ 我们的现状

| 我们有的 | 位置 |
|---|---|
| 提问卡：多题、多选、自由文本、选项预览（HTML 按纯文本渲染） | `bot/turn.tsx:204-306` |
| 「按默认继续」只在每题都有推荐项时出现 | `turn.tsx:214-217` |
| 「回答并继续」在无任何答案时禁用 | `turn.tsx:290-297` |

**缺的**：逐题向导（我们一次铺开所有题）、跳过按钮、答完后的"回看"（我们的卡答完就变成历史对话的一部分，没有独立可重开的记录）、提问生成中的占位态（"问题生成中…"）。

**差距判定**：**有但交互形态不同**。
一次铺开 3 道题在桌面端没问题，但在窄窗口里会变成一屏表单；逐题走是更稳的选择。这条属于"可以不改，但要知道差在哪"。

---

### 3.6 域 6：产物与文件

#### ① WorkBuddy 的界面元素

| 元素 | 锚点 |
|---|---|
| **一轮结束后的产物槽位**（三组计数） | `zh-cn:477 §artifactSlot.title`＝"任务产生制品（{count}个）："、`.filesTitle`＝"文件变更（{count}个）："、`.sourcesTitle`＝"引用来源 ({count})" |
| 产物卡（标题/副标题/图标/更多） | `.artifact-slot-panel__card / __card-title / __card-subtitle / __card-actions / __card-more` |
| 产物网格（单个时变单列） | `.artifact-slot-panel__grid / __grid--single` |
| 每个产物的动作 | `§artifactSlot.openPreview`（打开网页预览 →）/ `.independentWindow`（在独立窗口打开）/ `.openInDefaultApp` / `.download` / `.copyPath` / `.share` |
| **右侧产物 tab 条**（可拖拽排序、可分离、溢出渐隐 + 左右箭头） | `.artifact-tabs / __list / __list--dragging / .artifact-tab--active / --detached / --drag-placeholder / .artifact-tabs__scroll-arrow--start/--end / .artifact-tabs__list--fade-start/--end` |
| 产物 tab 右键菜单 | `.artifact-tabs__context-menu-item` |
| 独立窗口打开的三条降级 | `§artifactSlot.independentWindowFailed` / `.independentWindowTooLarge`（"文件超过 {size}…"）/ `.independentWindowUnsupportedType` |
| 文件版本回滚 | `§fileVersion.revert`＝"回滚到此版本"、`.revertTip`（保留之后所有提交记录，可再次回滚） |
| 引用来源（favicon + 计数 + tooltip） | `.artifact-slot-panel__sources / __source-favicon`、`§artifactSlot.sourcesTooltip`＝"引用 {count} 篇资料作为参考" |
| 分享（带积分活动） | `§artifactSlot.shareActivityBadge` / `.shareActivityGuide` |

#### ② 信息组织

- 产物不是"右栏一个列表"，而是**两处**：一轮正文下方的**归集条**（制品/文件变更/来源三组，各带计数），和右侧的**tab 工作区**（多个产物并排开、可拖、可分离成窗口）。
- 归集条解决"我不知道它产出了什么"，tab 条解决"我要同时看两个"。
- 三组各自可"查看所有"（`§artifactSlot.viewAll`）。

#### ③ 状态覆盖

预览中 / 独立窗口打开失败（3 种原因分别给话）/ 文件过大 / 不支持类型 / 另存为失败（`§artifactSlot.saveAsFailed`）/ 无产物（该条不渲染）。

#### ④ Qoder 的做法

- 「任务文件变更卡片」是**可开关的**：`conversationFileChanges`＝"控制 Agent 完成一轮后，是否在任务流末尾展示文件变更和 Diff 审阅入口"（`qd:…@5963860 §settingsResources`）。
- 每个文件变更给"打开文件 / 展开本次修改"两个动作；diff 生成失败有专门文案：`diffUnavailable` / `diffBeforeUnavailable`（读不到写入前内容）/ `diffTooLarge`（`@5759840 §chatTimelineResources`）。
- 产物在"任务监控"里是一项（`title:"产出"`）。

#### ⑤ 我们的现状

| 我们有的 | 位置 |
|---|---|
| 右栏三 tab（变更/证据/活动），计数写在标签上 | `bot/panel.tsx:102-192` |
| 同一文件多次改动合并成一行 | `panel.tsx:64-77 §uniqueChanges` |
| 行内 diff（+/-/@@ 着色，最多 400 行） | `panel.tsx:26-48` |
| 无 diff 时说明原因，不装死 | `panel.tsx:279-281` |
| 对话内"依据条"（看了/改了/跑了/失败/耗时） | `turn.tsx:426-457` |
| 任务详情右栏 4 tab（文件/Diff/终端/证明） | `tasks-view.tsx:420-424`、`file-panel.tsx`、`diff-panel.tsx` |

**缺的**：一轮下方的**产物归集条**（我们现在只有"依据条"报数量，没有"产出了哪几个文件/哪几篇来源"的可点列表）、多产物 tab、独立窗口、下载/另存/复制路径/分享、文件版本回滚。

**差距判定**：**有但位置不对**。
"改了什么"我们现在只能从依据条点开折叠过程、或打开右栏；人家在正文下方直接列"文件变更（3 个）"+ 可点的三个文件名。**用户读完回答后想知道"东西在哪"，我们让他多走两步。**

---

### 3.7 域 7：侧栏与导航

#### ① WorkBuddy 的界面元素

| 元素 | 锚点 |
|---|---|
| 侧栏骨架（header / body / overview popover / 折叠） | `.sidebar-next / __main-header / __body / __overview-popover / __collapse-toggle / --left-collapsed` |
| 置顶 | `.sidebar-next-pin-button / --active`、`§conversation.section.pinned`、`.pinnedDropHint`＝"拖到此处置顶" |
| 分组（可增/可排序/可折叠） | `§conversation.section.groups / .addWorkspace（文案"新建分组"）/ .groupSort / .groupSortByUpdatedAt / .groupSortManual / .collapseAll / .expandAll` |
| 分区状态旗标（进行中/待确认/失败/未读） | `§conversation.sectionFlag.tooltip.working / .pending / .failed / .unread` |
| 筛选 | `§conversation.section.filter` / `.recentFilter`＝"筛选最近任务" / `.recentHideOtherSections` |
| 搜索（含快捷键说明与空结果） | `§conversation.search.placeholder`＝"搜索任务"、`.resultCount`、`.recentTasks`、`.shortcut.navigate/open/close`、`§conversation.section.noFilterResult`＝"没有匹配的任务" |
| 消息中心（铃铛 + 红点 + 弹层） | `.msg-center-bell-btn / .msg-center-badge / .msg-center-dot / .msg-center-popup-host` |
| 顶部分区：任务/助理/定时任务/专家/项目/资料库/应用/我的文件/我的邮箱 | 上一份 §2.1 已列（`conversation.playground / colleagues / automation / expert / projects / space / genie / library / myFiles / myMailbox`） |

#### ② 信息组织

- 侧栏是**三段**：顶部功能入口（10 个）→ 置顶 → 分组（组内按更新时间或手动排序）。
- 每组标题上带"状态旗标"：这组里有任务在跑 / 等你确认 / 失败 / 未读，直接说数量性质（点开才看具体）。
- 搜索是侧栏内联展开，不是跳页；结果里区分"最近任务"和"搜索结果"。
- 未读是独立概念（`.unread`），与"进行中"分开。

#### ③ 状态覆盖

loading（骨架）/ empty（"暂无任务，点击上方按钮开始新任务"）/ 无筛选结果 / 折叠态 / 分组空 / 拖拽中（置顶落点）/ 未读 / 失败 / 待确认。

#### ④ Qoder 的做法

- **四种分组方式**：按工作区 / 按活动日期 / 按自定义 / 不分组（`qd:…@5788461 §sidebarResources.label:"分组方式"`）。
- **四种排序**：手动 / 最近更新 / 名称 / 创建时间。
- **两层筛选**：工作区（全部/无/不可用）+ 最近活动（全部/今天）。
- 自定义分组支持：新建并移动 / 移动所选 / 归档组内任务 / 解散分组（任务回到"未分组对话"，不删除内容）+ 每一步的失败文案。
- 加载更多失败有独立重试（`loadMoreError` / `retryLoadMore`）。

#### ⑤ 我们的现状

| 我们有的 | 位置 |
|---|---|
| 侧栏：搜索、项目区（可置顶）、按天分组（今天/昨天/7天/更早）、折叠 | `bot/sidebar.tsx:96-240` |
| 重名会话补时间戳 | `sidebar.tsx:59-73 §disambiguate` |
| 状态点（全站唯一实现，running/failed/done） | `bot/status-dot.tsx:1-30` |
| 全站左栏（独立于对话页） | `components/side-nav.tsx`、`app-rail.tsx` |

**缺的**：置顶**单条会话**（我们只能置顶项目）、自定义分组（我们只有按天）、未读、失败/待确认的**聚合旗标**、消息中心、搜索快捷键、无结果态文案。

**差距判定**：**有但状态不全**。
三个具体后果：① 同时跑 5 个任务时，侧栏没有一个地方说"有 1 个在等你批准"；② 关掉窗口再回来，只能从头翻；③ 重名会话已经处理了（做得比人家细），但"哪些没读"完全没做。

---

### 3.8 域 8：空态与首次使用

#### ① WorkBuddy 的界面元素

| 元素 | 锚点 |
|---|---|
| 首页标题 / 副标题 | `zh-cn:§welcome.title`＝"WorkBuddy，我帮你"、`.subtitle` |
| **四种模式卡**（开发/办公/设计/问答），各带一句边界说明 | `§welcome.mode.coding / .working / .design / .quick` + `*.subtitle` + `*.description` |
| 问答模式的特殊说明（不消耗积分、不接工具） | `§welcome.mode.quick.description` |
| 任务启动器（GitHub/工蜂/CNB + 目录 + 分支） | `§welcome.taskStarter.github / gongfeng / cnb / selectRepo / selectBranch / hint` |
| 快捷动作（8 条，随模式变） | `§welcome.quickAction.draftSpecs / analyzeBugs / generateDocs / codeReview / refactorCode / writeTests / writeDocs / analyzeData` |
| 活动位（可关闭） | `§welcome.activity.label / .notice / .action / .modelSwitched` |
| 连接应用引导（8 个应用 + 三态） | `§welcome.claw.apps.*`、`.appClickToConnect / .appConnecting / .appConnected / .appComingSoon` |
| 通用空态组件（info/warning/error/loading + 重试） | `StatusPlaceholder-B312aGar.js:1 §StatusPlaceholder`；类 `.status-placeholder--error/--info/--warning`、`__retry-btn` |
| 导入引导 | `DataImportOnboardingHost-BfCK_h5K.js`、`libraryOnboarding.*` |

#### ② 信息组织

- 首页不是"一个大输入框"，是**先选模式、再选落地方式、再给例子**：选模式（决定能力边界）→ 选目录/仓库（决定在哪儿干）→ 快捷动作（决定做什么）。
- 每种模式的说明都写了"能做什么/不能做什么"，而不是营销词。
- 空态组件是全站共用的一个：`type` 决定图标与颜色，`title/description/action/onRetry` 由调用方给。

#### ③ 状态覆盖

空 / 加载（`cb-loading`，带渐变环）/ 错误（含重试按钮）/ 警告 / 无筛选结果 / 未连接 / 连接中 / 已连接 / 敬请期待。

#### ④ Qoder 的做法

- 新任务页：`title:"开始一个新任务"` + `prompt:"用 Qoder 创造了不起的事物"` + 四套"涂鸦"文案（苹果树/Bee/Mint/Built For Builders，`@5641573 §newChatResources`）。
- 首页有**活跃热力图**（按会话或按 Credits 两种口径，可切换），以及 Attention 入口。
- 空态写得很具体：`emptyTitle:"还没有可发起任务的 Agent"` / `emptyDescription:"从 Runtime 创建或启用 Agent 后，就可以在这里直接发起任务"`（`@5690466`）。

#### ⑤ 我们的现状

| 我们有的 | 位置 |
|---|---|
| 空对话态：logo + "今天想做点什么？" + 一句说明 + 4 张示例卡（点击填入输入框） | `bot/bot-app.tsx:1170-1200` |
| 通用 rail 空态（图标 + 标题 + 正文 + fill/compact） | `components/rail-empty.tsx` |
| 右栏三种空态各写各的话（不是一句"暂无数据"） | `bot/panel.tsx:238-248 / 292-302 / 354-368` |
| 未绑定目录时的引导（不是静默失败） | `composer.tsx:684-698` |

**缺的**：模式选择（我们只有场景下拉，且藏在输入框工具条里）、落地方式（目录/仓库/分支）、按场景变化的快捷动作、连接应用引导、加载态的统一视觉。

**差距判定**：**有但文案糙**（相对而言）+ 少一层"先选模式"。
我们的 4 张示例卡是写死的通用句，不随场景变；WorkBuddy 的 8 条快捷动作随模式换。用户第一次进来，我们没告诉他"这个产品有哪几种用法"。

---

### 3.9 域 9：错误与失败

#### ① WorkBuddy 的界面元素

| 元素 | 锚点 |
|---|---|
| 错误详情卡（标题/消息/错误码/消息 ID/时间/原始错误/复制） | `zh-cn:§error.title / .message / .code / .messageId / .time / .original / .copy / .copied / .close` |
| 网络/超时/模型超时/过载/限流（各一对 title+message） | `§error.network.*` `§error.timeout.*` `§error.modelTimeout.*` `§error.modelOverload.*` `§error.rateLimit.*` |
| 限流可操作（消耗积分继续 / 切 Auto / 重置时间） | `§error.rateLimitCap.usePaid / .switchAuto / .hoverPaid` |
| 上传类错误（过大/格式/路径/超时） | `§error.fileTooLarge / .unsupportedFormat / .pathNotAllowed / .uploadTimeout` |
| 停止原因 7 种 | `§error.stopReason.maxTokens / maxTurnRequests / refusal / modelService / generic / network / authError`；另有 `§error.contentFilter.banner` |
| 启动失败（组件加载失败 → 给下载地址） | `§error.storageBootstrapFailed.title / .message` |
| 通用错误占位（标题/描述/重试） | `§error.loadFailTitle / .loadFailDesc`＝"服务暂时不可用，请稍后重试，系统正在自动恢复"、`.retry` |
| **25 条定时任务失败原因，每条给下一步** | `§automation.detail.reason.*`（见 §3.14） |

#### ② 信息组织

- 一个错误分**三层**：一句人话（title）→ 一句该怎么办（message）→ 折叠的技术详情（code/messageId/time/original + 复制按钮）。
- "能自己好的"和"要你动手的"措辞不同：`loadFailDesc` 说"系统正在自动恢复"，`rateLimit` 说"您的使用量将在 {time} 重置"。
- 限流不是死路：给"消耗积分继续""切 Auto"两个出口，并预告"频控解除后可继续免费使用"。

#### ③ 状态覆盖

网络 / 超时 / 模型超时 / 模型过载 / 限流 / 免费额度超限 / 上传失败 / 路径不允许 / 启动失败 / 内容过滤 / 拒绝 / token 截断 / token 凭证失败 / 未知。

#### ④ Qoder 的做法

- 25 条命名的失败原因（连"原定时间设备未在线，重试仍未成功"这种都有），见 §3.14。
- diff 失败三种（无法生成 / 读不到原文 / 太大）。
- 编辑重发失败四种（见 §3.1）。
- 授权码过期、目录不可用、工作区路径不可用等，各有独立文案。

#### ⑤ 我们的现状

| 我们有的 | 位置 |
|---|---|
| 一轮失败原因：一行红字 | `bot/turn.tsx:516-520` |
| 连接/读取失败横幅 | `bot-app.tsx:1155-1165` |
| 输入区 error / hint 两条 | `composer.tsx:247-256` |
| 错误码解析（`errorCode()`） | `lib/http`（多个面板引用） |
| 右栏空/错不分（空态写"这一轮还没有…"） | `panel.tsx:246-248` |
| 审批过期专门处理（不装死） | `turn.tsx:80-88 / 164-178` |

**缺的**：错误分层（人话/怎么办/技术详情 + 复制）、错误码可见、按错误类型给不同出口、失败原因枚举（我们现在透传后端的字符串）、限流类可操作提示。

**差距判定**：**有但文案糙**（这是 15 域里文案差距最大的一域）。
我们现在的 `reason` 是"模型/后端说了什么"，人家是"你要做什么"。用户看到一行英文栈或一句 `429`，除了重试没有第二个动作。

---

### 3.10 域 10：设置

#### ① WorkBuddy 的界面元素（18 个面板 / 4 组）

| 组 | 面板 |
|---|---|
| 设置 | 通用 `settings.nav.settings`、外观 `appearance`、模型 `models`、快捷键 `keyboardShortcuts` |
| 功能 | 智能体 `extensions`、技能、连接器、对话流 `conversationFlow`、应用管理 `buddyApps`、记忆与进化 `memory`、个性化 `personalization`、实验室 `laboratory` |
| 数据与安全 | 安全中心 `securityCenter`、数据管理 `dataManagement`、设备连接 `deviceConnections`、导入 `dataImport` |
| 关于我们 | 套餐与积分 `subscriptionUsage`、个人主页 `account`、关于 `about`、获取帮助 `getHelp` |

（`wb:renderer/assets/zh-cn-J68R5HFz.js:477 §settings.nav.*`）

值得单说的四个机制：

| 机制 | 锚点 | 说明 |
|---|---|---|
| **设置项搜索 + 锚点跳转** | `settings-search-anchors-wkJgyYfD.js`、`§settings.nav.<x>.keywords` | 每个面板都有 keywords，搜索能命中"队列/排队/立即发送"这类用户词 |
| **lite 模型单列** | `§settings.models.lite.default / .description` | 标题、摘要、记忆筛选用便宜模型，不影响主对话 |
| **保存失败分原因** | `§settings.saveFailed.permission`＝"配置文件没有写入权限"、`§settings.personalization.saveErrorReason.diskFull` | 不说"保存失败"，说为什么 |
| **系统授权单独一页** | `§settings.systemPermissions.*`（完全磁盘访问/辅助功能/自动化/通知/日历） | 缺哪项、还差几项（`pendingCount`）、点哪去授权 |

#### ② Qoder 的做法

- 设置分 5 组：个人 / 集成 / 编码 / 归档管理 / 其他。
- **「任务监控」是一整页开关**：展示方式 + 12 个可开关的区块（回顾/目标/计划/子智能体/后台进程/侧边聊天/Skill 与 MCP/产出/网页查阅/来源/Quick Notes/记忆更新）。
- **「模式配置」按编程/通用分别设置**主题、运行位置入口、输入区标签、任务卡工程信息显隐。
- Git 侧有分支前缀、始终 force-push（带一句"会改写远端历史，但远端已变化时拒绝覆盖"）。

#### ③ 我们的现状

| 我们有的 | 位置 |
|---|---|
| 7 段：模型与供应商 / 安全与权限 / 执行与智能体 / 消息通道 / 外部工具与插件 / 查看与效率 / 高级 | `settings/settings-view.tsx:18-120` |
| 角色分工模型（跟随主模型 / 按角色选） | `settings/role-model-settings.tsx:31-45` |
| guardian 三档 + 超时 + 上下文水位 | `settings/agent-runtime-panel.tsx:46-62 / 186-260` |
| 安全能力开关 + 路径/命令边界 | `settings/safety-panel.tsx` |
| 每个面板标"何时生效"（即时 / 下一个任务 / 保存即生效 / 实时读取） | `settings-view.tsx:46,60,74,88,103` |

**缺的**：设置搜索、外观/主题页（只有顶栏切换）、快捷键页、记忆页、个性化页、设备连接、用量/套餐、系统授权、数据管理。

**差距判定**：**有但少一半**（数量和分组都少）。
优点是我们每个面板都标了"何时生效"，这一点比两家都清楚；缺点是用户找不到东西——没有搜索，7 段里塞了 20 多个开关。

---

### 3.11 域 11：记忆与个性化

#### ① WorkBuddy 的界面元素

| 元素 | 锚点 |
|---|---|
| 记忆总述（"记住你的偏好和习惯，对话越多，它就越懂你"） | `zh-cn:439 §settings.memory.intro` |
| **两种记忆并列**：本地记忆（写文件，跨会话）/ 云端记忆（自动整理画像，多设备同步） | `§settings.memory.localMemory.description`、`.generateMemory.description` + `.tooltip` |
| 来自对话的记忆（时间 + 查看和编辑） | `§settings.memory.chatMemoryTitle / .chatMemoryUpdated / .chatMemoryViewEdit` |
| 记忆管理面板：当前记忆 + **"修改建议"**（不直接改） | `§settings.memory.modal.manage.currentMemory / .suggestion / .suggestionPlaceholder`＝"添加或删除一条记忆，例如'以后都用中文回复'" |
| 提交后的中间态 | `.submitting` → `.submitSuccess`＝"已提交，记忆更新中…" |
| 空态 + 空态原因 | `.empty`＝"暂无记忆内容"、`.emptyHint`＝"开启'生成对话记忆'后，系统将自动记录你的偏好" |
| 重置记忆（说明不可恢复 + 说明重置后仍会生成） | `.chatMemoryResetModalDesc / .chatMemoryResetModalHint` |
| **从别的 AI 导入**（复制提示词 → 粘贴结果） | `§settings.memory.modal.import.step1 / promptTemplate / step2 / placeholder` |
| 隐私声明（仅你本人可见 + 隐私政策链接） | `§settings.memory.privacy.prefix / linkText / suffix` |
| 个性化：7 种回复风格 | `§settings.personalization.style.default/professional/friendly/straightforward/creative/efficient/sarcastic/socratic` |
| 个性化：自定义指令（全局生效，带字数上限） | `§settings.personalization.customPrompt / .customPromptHint / .customPromptTooLong` |
| 个性化：首页推荐 / 后续推荐 / 文件修改过程 三个开关 | `.homeTaskRecommend / .followUpRecommend / .fileChanges` |

#### ② 信息组织

- 记忆是**"它对你的画像"**，不是聊天记录：每晚重新生成一份摘要，用户只能提"修改建议"，系统再改。
- 导入走"提示词搬运"：给一段结构化模板，让用户复制到 ChatGPT/Claude，再把结果粘回来。**不爬、不导 API，纯手工但零信任风险。**
- 个性化与记忆分开：记忆是"事实"，个性化是"说话方式"（风格 + 全局指令）。

#### ③ 状态覆盖

未开启 / 加载中 / 加载失败 / 空 / 有内容 / 提交中 / 提交成功（更新中）/ 提交失败 / 重置确认 / 导入中 / 导入失败。

#### ④ Qoder 的做法

只有入口没有管理：设置里一项"记忆更新"（`title:"记忆更新" / description:"记忆发生更新时，显示前往记忆设置的入口"`，`@5963860`），任务监控里显示"记忆更新"提示。**该产品此域基本空着**。

#### ⑤ 我们的现状

| 我们有的 | 位置 |
|---|---|
| 日常页：写一条习惯 → 存 localStorage（最多 50 条） | `components/habits.tsx:14-36` |
| 记忆候选：后端给候选 → 用户确认 → 变成技能草案 | `habits.tsx:42-60`、`skills/skills-view.tsx:210-212` |
| 输入区记忆开关（仅日常场景） | `composer.tsx:628-646` |
| 记忆文件（MEMORY.md）概念 | 后端侧，前端只有开关 |

**缺的**：记忆画像的查看/编辑（我们没有任何界面能看到"它记住了我什么"）、修改建议、重置、导入、语气风格、全局自定义指令。

**差距判定**：**有但用户看不见**（这是最危险的一类"有"）。
我们把记忆做成了"技能的副产品"，用户无法回答"它到底记住我什么"。可解释性为零，用户只能选择全开或全关。

---

### 3.12 域 12：技能与工具

#### ① WorkBuddy 的界面元素

| 元素 | 锚点 |
|---|---|
| 五个 tab | `§skills.tab.recommend / .skillhub / .plugins / .knot / .enterprise / .installed` |
| 卡片（下载量/收藏量/使用量/安装量） | `§skills.card.downloads / .stars / .useCount / .installCount` |
| 卡片操作 | `§skills.card.enable / .disable / .edit / .uninstall / .pin / .install / .more` |
| 已安装分组（用户自定义 / 来自市场 / 套件） | `§skills.installed.group.custom / .market / .plugins` + `.group.loadMore` |
| 已安装空态（给出两条路：上传文件夹 / 让 AI 造一个） | `§skills.installed.empty.title / .desc` |
| **安装前安全扫描**（进度 + 风险等级 + 判定 + 倒计时确认） | `§skills.security.scanning / .resultTitle / .result.safe/.low/.medium/.high / .verdict.white/.grey/.black / .overallRisk / .continueInstall / .confirmCountdown / .skip` |
| 企业禁止高风险 | `§skills.security.policy.forbidHighRisk` |
| 深度分析可等待或跳过（后台继续） | `§skills.security.deepAnalysis / .deepAnalysisHint` |
| 安装确认（同名覆盖 / 下载到本地目录） | `§skills.installConfirm.title.overwrite / .content.overwrite / .confirm.overwrite` |
| 输入框里的技能选择器（搜索/空/加载失败/团队 vs 个人/置顶） | `§input.wbAdd.skills.*`（14 条状态文案） |

#### ② 信息组织

- 技能是**市场 + 已安装**两个世界，卡片正面给"四个人气数"，背面给"启用/停用/卸载"。
- 安装是一个**带安全门的三步**：扫描（几秒）→ 结果（4 级风险 + 黑白灰判定）→ 确认（高风险要等 3 秒倒计时才能点）。
- 扫描可以跳过但会继续在后台跑，结果稍后送达（`deepAnalysisHint`）。

#### ③ 状态覆盖

推荐 / 加载失败 / 搜索中 / 无结果 / 空 / 已安装 / 已启用 / 已停用 / 安装中 / 覆盖确认 / 扫描中 / 需深度分析 / 安全 / 低 / 中 / 高 / 企业禁止 / 跳过 / 上传中 / 上传失败。

#### ④ Qoder 的做法

- 扩展管理一个页面管四类：技能 / 插件 / 连接器 / 智能体（`@5907899 §extensionsResources`）。
- **来源标注很细**：Qoder 市场 / 本地 Skill / 本地扩展 / 本地 Plugin / 自定义 Connector / 本地 Subagent / 企业市场 / 由 Plugin 提供（`installedFrom*`）。
- 由插件提供的项不能单独卸载：`providedByPluginTitle`＝"由插件提供，请在插件页管理其开关与卸载"。
- 教育横幅：把文档转成可复用技能（`educationBannerTitle`）。

#### ⑤ 我们的现状

| 我们有的 | 位置 |
|---|---|
| 技能页三块：对话里能用的命令 / 技能库 / 待采用的提案 | `skills/skills-view.tsx:230-410` |
| 启用/停用（进系统提示的"工作偏好"） | `skills-view.tsx:133-145` |
| 从本地目录/Git 安装、装到"我的"或"当前项目"、删除 | `skills-view.tsx:249-340` |
| 技能市场面板 | `skills/market-panel.tsx`（271 行） |
| 斜杠菜单 + 技能建议（命中提示） | `bot/slash-menu.tsx`、`lib/skill-suggest`、`turn.tsx:524-536` |

**缺的**：安装前安全扫描（我们直接装）、技能卡片的人气数据、置顶、团队/企业维度、"由插件提供"的归属关系、深度分析。

**差距判定**：**有但少一道门**。
技能是**能执行任意脚本**的东西，我们没有扫描就直接落盘。WorkBuddy 在这一处的设计（4 级风险 + 倒计时 + 企业禁止）是安全底线，不是增值功能。

---

### 3.13 域 13：连接器与 MCP

#### ① WorkBuddy 的界面元素

| 元素 | 锚点 |
|---|---|
| 连接器管理页（搜索/官方内置 vs 企业/自定义） | `§connectorPanel.title / .tab.official / .tab.enterprise / .customConnector / .customConnectorDisabled` |
| **四个连接状态** | `§connectorPanel.connected / .partiallyConnected（已连接 · 部分功能可用）/ .disconnected / .error` |
| **能力级授权**（每项功能单独 可用/未授权） | `§connectorPanel.capabilitiesTitle / .capabilityGranted / .capabilityMissing / .capabilityPartialHint / .grantMorePermissions` |
| 具体能力（以 Gmail 为例：搜索读取/草稿/发送） | `§connectorPanel.capability.read / .draft / .send` + `.gmailAuthorizationHint` |
| 危险区（撤销授权 = 断开账号） | `§connectors.github.dangerZoneDesc` |
| 仓库级二次授权 | `§connectors.github.repoNeedAuth`＝"需要仓库授权"、`.addRepo`、`.authedRepos` |
| 三种授权方式 | 扫码 `§connectors.qrModal.*` / 设备码 `§connectors.deviceCodeModal.*`（含"授权码 {time} 后失效"/"已过期"）/ 令牌表单 `§connectors.cnb.modal*` + `.modalHelpTitle` |
| 连接器状态胶囊 | `connector-capsule-mGYm2esg.js`、`§connectorPanel.connecting / .statusFailed` + `connector-runtime-toast` |
| MCP 服务器管理 | `§mcp.title / .addServer / .form.* / .statusEnabled / .statusFailed / .reconnect` |
| MCP 工具数 + 超限警告 | `§mcp.toolsEnabled`＝"{enabled}/{total} 个工具已启用"、`§mcp.toolsLimitWarning.description`＝"启用超过 {limit} 个工具可能影响 AI 回复质量" |
| MCP 兴趣点授权卡 | `McpPoiConsentCard` |
| 连接器推荐（Agent 主动要） | `§tool.pluginRecommendation.title.connector`＝"请连接" |

#### ② 信息组织

- 连接器卡片正面只说"连没连"，点进去才是**能力清单**：每项功能一个开关式状态（可用/未授权），并明确"需要其他功能时可补充授权，已有功能不受影响"。
- 授权失败按原因分：令牌无效 / 权限不足 / 授权过期 / 访问超限 / 文件无权限 / 文件不存在 —— 每种给不同的下一步（重连 / 升级账号 / 找管理员）。
- MCP 与连接器是两个页面，因为"我自己起一个服务"和"授权一个 SaaS"是两件事。

#### ③ 状态覆盖

未连接 / 连接中 / 已连接 / 部分可用 / 连接失败 / 连接错误 / 授权过期 / 授权码过期 / 需要仓库授权 / 无权限 / 超限 / 企业禁止新增 / 工具数超限。

#### ④ Qoder 的做法

- 连接器在**同一个扩展市场**里，不单开页面：`connectorsTitle:"连接器"`，`connectorsDescription:"连接 Qoder 市场中的远端服务；公开连接可直接使用，需要授权的连接先保存待配置"`（`@5907899`）。
- 没有能力级授权，只有"已配置/待配置"。

#### ⑤ 我们的现状

| 我们有的 | 位置 |
|---|---|
| MCP 面板（增删改、工具列表） | `settings/mcp-panel.tsx:255` |
| 插件包面板（plugin.json） | `settings/plugins-panel.tsx:156` |
| 消息通道（企业微信/钉钉/飞书） | `settings/channels-panel.tsx` |

**缺的**：连接器市场、能力级授权、扫码/设备码授权流程、授权失败分类文案、连接器状态胶囊、Agent 主动请求连接。

**差距判定**：**没有这个功能**（就"连接器"这个形态而言）。
我们只有"MCP 服务器"这一种外部能力接入，用户要连 Gmail/Figma/腾讯文档没有入口。"MCP"是给开发者的词，不是给用户的词——WorkBuddy 把同一件事包装成了 12 个带图标的卡片。

---

### 3.14 域 14：定时任务与自动化

#### ① WorkBuddy 的界面元素

| 元素 | 锚点 |
|---|---|
| 任务卡（名称/描述/计划时间/状态/开关/更多/删除） | `.atm-task-card / __name / __desc / __schedule / __status / __switch / __more / __menu-danger`；变体 `--paused / --expired / --readonly` |
| 工具栏（标题/tab/筛选/搜索/新建） | `.atm-toolbar / __title / .atm-toolbar-tabs / .atm-filter-btn / .atm-search-input / .atm-create-split` |
| 批量操作栏 | `.atm-batch-bar / __count / __action` |
| 空态 + **模板区** | `.atm-task-empty / __hero / __illustration / __templates / .atm-template-card*` |
| 运行记录表（列：名称/状态/时间/耗时/类型/操作） | `§automation.records.column.*`、`.atm-records-table / .atm-record-row--no-conversation / --readonly` |
| 记录筛选（全部状态/全部类型/全部任务 + 搜索） | `§automation.records.statusAll / .typeAll / .taskAll / .taskSearchPlaceholder` |
| 编辑弹窗（名称/工作空间/提示词/执行频率/权限/推送） | `§automation.modal.name / .projects / .prompt / .schedule / .permissionMode.* / .pushToWecomBot` |
| 执行频率控件（每天/每周/间隔 + 时间轮） | `.atm-schedule-tabs / .atm-schedule-days / .atm-schedule-v2-*`（含 `atm-freq-year-wheel`）`atm-freq-item--selected/--disabled` |
| 任务卡上的"下一次"与"已错过" | `§automation.row.startsIn`＝"{time}执行"、`.missedPending`＝"已错过计划时间，将补跑一次" |
| 详情页（摘要/制品/运行明细/技术详情） | `§automation.detail.summary / .artifacts / .runs / .technicalDetails / .errorTruncated / .viewFullError` |

#### ② 信息组织

- 三层：**列表（卡片）→ 运行记录（表）→ 单次详情（摘要 + 制品 + 明细 + 技术详情）**。
- 卡片上直接给"下一次什么时候跑"，过期/暂停/中断各有徽标；开关就是暂停/恢复。
- 编辑弹窗把**执行权限**摆在台面上：免确认执行 vs 逐次确认，并解释"无人值守时你不在屏幕前会发生什么"（`§automation.permission.defaultDesc`）。
- 失败原因 25 条，每条一句话说清"为什么 + 你该做什么"。

#### ③ 状态覆盖

**卡片级 12 态**：已启动 / 已暂停 / 已过期 / 运行中（含 `{done}/{total}`）/ 准备中 / 正在创建会话 / 需确认账号 / 高峰期排队中 / 已错过将补跑 / 等待登录 / 排队中 / 上次执行中断。
**记录级**：成功 / 失败 / 排队中 / 中断 / 已归档 / 已删除任务的历史记录。
**失败原因 25 条**（全部列在 `§automation.detail.reason.*`）：
`running / queued / partialDelivered / historyMissing / auth / cloudReasonMissing / connectorUnavailable / contextLimit / emptyResult / environmentNotReady / failed / finalizationTimeout / interrupted / modelConfigFailed / network / rateLimited / recoveryFailed / requestRefused / runTimeout / sessionEnded / sessionStartFailed / sessionStartTimeout / sessionStuck / turnLimit / workspaceUnavailable`

#### ④ Qoder 的做法

- 创建走**模板或自然语言**：`createWithQoder` + `createWithQoderPrompt:"我要创建一个自动化，每{interval}执行{task}"`。
- **输出模式二选一**：独立任务（每次新任务）/ 合并任务（同一任务内累积历史）——`independent` / `merged`，各带说明。
- 单次自动化可"跑完自删"（`deleteAfterRun`，但保留记录）。
- 可设**到期日**（到期自动暂停，`setExpirationDate`）。
- 支持 cron 直接输入（`cronPlaceholder:"0 9 * * 1-5"`）。
- 全部归档（`archiveAll` + 二次确认 + 失败文案）。

#### ⑤ 我们的现状

| 我们有的 | 位置 |
|---|---|
| 「待触发」列表（说"盯着某事""9 点提醒我"之后登记） | `monitor/monitor-view.tsx:284-330` |
| 暂停 / 恢复 / 立即放行 | 同上（`control()` / `release()`） |
| 永不触发时给警告色 | `monitor-view.tsx:305`（`never_fires`） |

**缺的**：**周期性定时任务**（没有频率编辑器、没有 cron）、运行记录表、单次执行详情、模板、执行权限选择、失败原因分类、批量操作、归档。

**差距判定**：**没有这个功能**（我们只有"一次性触发器"，没有"重复执行的作业"）。
这是 15 域里覆盖最薄的一域，也是用户最容易提的需求（"每天早上帮我汇总"）。

---

### 3.15 域 15：多 Agent / 协作

#### ① WorkBuddy 的两套东西

**A. 助理（colleagues）——把 Agent 当同事**

| 元素 | 锚点 |
|---|---|
| 名册（工作中/已停用 + 空态） | `§colleagues.statusAvailable / .statusIdle / .empty` |
| 助理详情：首页 / 对话任务 / 记忆 三个 tab | `§colleagues.detail.nav.home / .tasks / .memory` |
| 档案动作（查看/编辑/复制为新/雇佣到项目/发布到项目/暂停/恢复/删除） | `§colleagues.action.viewProfile / .copyAsNew / .hireToProject / .publishToProject / .pauseColleague / .delete` |
| 任务概览 + **近一年活跃热力图** | `§colleagues.detail.taskOverviewV2 / .daysOnDuty / .activityHeatmap / .activityLevel / .less / .more` |
| 状态三态 | `§colleagues.detail.colleagueStatus.working / .idle / .abnormal` |
| IM 绑定状态 | `§colleagues.detail.imStatus / .notBound` |
| 可见范围三档（个人分身/发布到项目/公开） | `§colleagues.bindChannel.permission.personal / .project / .public` + 每档一句说明 |
| 每个助理的独立记忆开关 | `§colleagues.imConfig.memory.switch.name / .desc` |

**B. 项目（collab）——人 + AI 共用的计划看板**

| 元素 | 锚点 |
|---|---|
| 项目列表（多人协同、空/加载/重命名/删除/退出/置顶提示） | `§collab.list.title / .subtitle / .loading / .toast.*` |
| 加入方式（申请审批 / 直接加入） | `§collab.applyJoin.*` / `§collab.directJoin.*` |
| 角色两档（成员/管理员）+ 每档能力说明 | `§collab.applyJoin.role.memberDesc / .adminDesc` |
| 审批流（谁批、几个批、进度） | `§collab.apply.flow.label / .project / .projectSingle / .progressLabel` |
| 审批状态 6 态 | `§collab.activity.approval.status.approved / .rejected / .cancelled / .expired / .invalid` |
| 无权限四种原因（不是企业成员/账号不对/已归档/已删除） | `§collab.noAccess.* / §collab.permissions.*` |
| 团队待授权（工具级） | `§conversation.teamMember.pendingPermission.label`＝"待授权"、`.countTooltip`＝"{count} 个工具待授权" |
| 计划看板上的任务分派 | `§collab.directJoin.capabilities.plan.desc`＝"成员与 AI 均可在看板上分派任务、跟进进展" |
| 协作活动流（含审批动作） | `§collab.activity.approval.action.approve / .reject` + `rolePrefix` |

#### ② 信息组织

- 助理是**可雇佣的资源**：有档案、有记忆、有热力图、有 IM 通道、有可见范围。"雇佣到项目"是把一个 Agent 放进一个项目组。
- 项目是**权限容器**：申请→审批→角色→能力，每一步都有文案；AI 和人在同一个成员表里，只是角色不同。
- 权限粒度到"工具"：`{count} 个工具待授权` 是成员卡上的徽标。

#### ③ 状态覆盖

助理：工作中 / 空闲 / 异常 / 已停用 / 待授权（N 个工具）/ 未绑定 IM / 无记忆记录。
项目：加载中 / 空 / 无权限（4 因）/ 申请中 / 已通过 / 已拒绝 / 已撤回 / 已过期 / 已失效 / 人数满 / 邀请过期。

#### ④ Qoder 的做法

- **成员页**：全部/人类/Agent/小队 四类，每类给定义（`humanDescription / agentDescription / squadDescription`）。
- **Attention 收件箱**：只收"只有你能做的判断"——`blocking`（阻塞中）/ `requested`（等待处理）/ `waitingForMe`（等我判断），并给三句解释：`whyNow`（为什么现在）/ `whyYou`（为什么找我）/ `recommendation`（Agent 建议）/ `reversibility`（可逆性）/ `afterDecision`（决定后）。
- 三个分组：判断与请求 / 结果验收 / 协作；过期项单独一组。
- 没有"雇佣/发布/可见范围"这套组织模型。

#### ⑤ 我们的现状

| 我们有的 | 位置 |
|---|---|
| 群聊（多 Bot + 人）+ 消息搜索（任务结论/群聊两类结果） | `team/team-view.tsx:551-600`、`:169-200` |
| Bot 名册（启停/编辑/新建） | `team-view.tsx:213-220`、`tab === "bots"` |
| 权限档文案（默认/自动接受编辑/只读计划/全部放行） | `team-view.tsx:59-64` |
| 子任务分派（`spawn_agent`）在任务时间线里可见 | `tasks/tool-display.ts:256-262` |

**缺的**：Agent 档案/能力/记忆/热力图、雇佣到项目、可见范围三档、成员角色与审批流、Attention（"只有你能做的判断"聚合）、工具级待授权徽标、任务转交。

**差距判定**：**有但位置不对**（我们的"团队"是 IM，人家的"同事"是 HR）。
我们的 group chat 解决"人和 Bot 在同一个房间说话"；WorkBuddy 解决"这个 Agent 归谁、能看什么、干了多少、记忆是什么"。这两件事不冲突，但**后者的信息在我们这里完全不存在**。

---

## 4. 15 域差距总表

| # | 域 | 我们 | 差在哪一层 | 一句话 |
|---|---|---|---|---|
| 1 | 会话与对话 | ✅ 部分 | **少入口 + 状态不全** | 缺划选引用、复制、编辑重发、队列；插话只有一条路 |
| 2 | 任务与作业 | ✅ | **位置不对** | 后台任务只是状态条一格，没有可管理的清单 |
| 3 | 计划与待办 | ⚠️ | **没有这个功能** | 退出计划模式不需要用户确认，计划没被"批准"过 |
| 4 | 审批与权限 | ✅ | **状态不全 + 缺事后视角** | 卡做得好，但没有审计表、没有工具级管控 |
| 5 | 提问与澄清 | ✅ | 形态不同 | 一次铺开所有题，无逐题向导、无跳过 |
| 6 | 产物与文件 | ⚠️ | **位置不对** | 没有"本轮产物"归集条，找东西要多走两步 |
| 7 | 侧栏与导航 | ⚠️ | **状态不全** | 无未读、无"有任务等你"聚合、无自定义分组 |
| 8 | 空态与首次使用 | ⚠️ | **少一层 + 文案糙** | 没有"先选模式"，示例卡写死不随场景变 |
| 9 | 错误与失败 | ⚠️ | **文案糙** | reason 是后端原话，不是"你该做什么" |
| 10 | 设置 | ⚠️ | **少一半** | 7 段 vs 18 面板，且没有设置搜索 |
| 11 | 记忆与个性化 | ⚠️ | **有但用户看不见** | 看不到"它记住了我什么"，不可解释 |
| 12 | 技能与工具 | ✅ | **少一道门** | 没有安装前安全扫描 |
| 13 | 连接器与 MCP | ❌ | **没有这个功能** | 只有 MCP，没有面向用户的连接器与能力级授权 |
| 14 | 定时任务与自动化 | ❌ | **没有这个功能** | 只有一次性触发器，没有重复作业与运行记录 |
| 15 | 多 Agent / 协作 | ⚠️ | **位置不对** | 我们做的是 IM，人家做的是"Agent 的 HR + 项目权限" |

**统计**：没有这个功能 2 域（13/14）、关键一段缺失 1 域（3）、位置不对 4 域（2/6/11/15）、状态不全 3 域（1/4/7）、文案糙 2 域（8/9）、少一半或少一道门 2 域（10/12）、形态差异 1 域（5）。

---

## 5. 该先改什么（按"用户会立刻感觉到"排序，不按兴趣）

| 序 | 改什么 | 影响文件 | 依据 | 拦住什么问题 | 量 |
|---|---|---|---|---|---|
| 1 | **一轮结束后给"产物归集条"**：文件变更 / 制品 / 来源三组，各带计数与可点列表 | `bot/turn.tsx`（新增 footer）、`tasks/api.ts`（已有 files/增补 artifacts/sources） | `wb:zh-cn:477 §artifactSlot.title / .filesTitle / .sourcesTitle` | "它说改好了，东西在哪" —— 现在要开右栏才知道 | M |
| 2 | **计划确认门**：`exit_plan_mode` 从"系统日志"改成"等你确认的卡"（开始执行 / 调整后再发 / 暂不实施） | `bot/turn.tsx`、`tasks/timeline-blocks.tsx`、`tasks/timeline.ts` | `wb:exit-plan-mode-floating-BBsMz-w8.js:1 §ExitPlanModeFloating`；`qd:@5634897 §exitPlan.approveChoice/requestChanges/skip` | 计划是不是该执行，界面从来没问过；用户离开一会儿回来，文件已经改了 | M |
| 3 | **侧栏状态聚合**：每个分组标题带"1 个等你批准 / 2 个失败 / 3 个未读"，单条会话可置顶、可标未读 | `bot/sidebar.tsx`、`bot/status-dot.tsx`、`tasks/api.ts`（thread 增加 unread） | `wb:zh-cn:477 §conversation.sectionFlag.tooltip.pending/.failed/.unread` | 同时跑 5 个任务时找不到"哪个在等我" | S |
| 4 | **错误分层**：`reason` → 一句人话 + 一句怎么办 + 可展开技术详情（错误码/时间/原始错误/复制） | `bot/turn.tsx:516`、`lib/http`、新增 `tasks/error-copy.ts` | `wb:zh-cn:§error.title/.message/.code/.original/.copy`；`§error.rateLimitCap.usePaid/.switchAuto` | 用户看到 429/英文栈，除了重试没有第二个动作 | M |
| 5 | **消息级操作**：复制、划选引用到输入框、（可选）编辑重发 | `bot/turn.tsx`、`bot/composer.tsx`、`bot/references.ts` | `wb:common-p2lSs_-n.js:1 §MessageQuoteSelectionOverlay`（copy/add/quickAsk，上限 20） | 想把上下文说准，只能重打一遍原话 | M |
| 6 | **任务工具 × 阶段文案矩阵**：`phase + tool → 一句人话`，带 fallback | `tasks/tool-display.ts`、`bot/turns.ts`、新增 `tasks/phase-copy.ts` | `wb:zh-cn:§progress.phase.model_streaming.<Tool> / tool_executing.<Tool> / waiting_for_permission.<Tool>`（3×100+ 条） | 20 多个工具在跑的时候，用户看到的还是"调用工具中" | S |
| 7 | **记忆可见**：一个页面列出"它记住了你什么"，允许提交修改建议与重置 | 新增 `settings/memory-panel.tsx`、`components/habits.tsx` | `wb:zh-cn:439 §settings.memory.chatMemoryViewEdit / .modal.manage.suggestion / .chatMemoryReset` | 用户无法回答"它到底记住我什么"，只能全开或全关 | M |
| 8 | **连接器（先做 2 个）：能力级授权 + 三态** | 新增 `settings/connectors-panel.tsx`、`settings/connectors-api.ts` | `wb:zh-cn:§connectorPanel.capabilitiesTitle / .capabilityGranted / .capabilityMissing / .grantMorePermissions` | 想连邮箱/文档没有入口；MCP 是开发者词 | L |
| 9 | **定时任务（最小可用）**：频率 + 提示词 + 运行记录表 + 暂停 | 新增 `app/automation/page.tsx`、`components/automation/*` | `wb:zh-cn:§automation.modal.schedule / .row.startsIn / .records.column.*`；`qd:@5892324 §automationResources` | 用户提的"每天早上汇总"这类需求完全无法承接 | L |
| 10 | **技能安装安全扫描**（最低：安装前弹风险确认 + 展示 hook/命令） | `skills/skills-view.tsx:249-340`、`skills/api.ts` | `wb:zh-cn:§skills.security.result.* / .confirmCountdown / .policy.forbidHighRisk` | 现在直接落盘，等于默认信任任何来源 | M |
| 11 | **后台任务清单**（胶囊 → 面板：列表/取消/看输出） | `bot/goal-status-bar.tsx`、`bot/background.ts`、新增 `bot/background-panel.tsx` | `wb:background-tasks-BPEN5SmF.css:1 §._panelList_ / ._detailTabs_ / ._taskCancel_` | 后台任务只能"点走"，不能"管" | M |
| 12 | **首次使用：模式选择 + 随场景变的示例** | `bot/bot-app.tsx:1170-1200`、`components/scenarios` | `wb:zh-cn:§welcome.mode.coding/working/design/quick` | 第一次进来不知道这产品有几种用法 | S |
| 13 | **Agent 档案页**（能力/记忆/近 30 天活跃/可见范围） | `team/team-view.tsx`（Bot 名册 tab） | `wb:zh-cn:§colleagues.detail.* / .action.hireToProject / .bindChannel.permission.*` | "这个 Bot 归谁、能看什么、干了多少"无从回答 | L |
| 14 | **设置搜索 + 生效时机统一标注** | `settings/settings-view.tsx:18-120` | `wb:settings-search-anchors-wkJgyYfD.js`、`§settings.nav.<x>.keywords` | 7 段 20+ 开关，找不到东西 | S |

**排序理由**：1–4 是"用户每天都会撞上"的（找产物、计划谁来批、哪个任务在等、报错看不懂）；5–7 是"每周撞上"的；8–11 是"想用但用不上"的（连接器/定时/技能/后台）；12–14 是体验打磨。

---

## 6. 最值得抄的一条

**"动作 × 阶段 = 一句人话"的文案矩阵（`progress.phase.*`），而不是任何单个界面。**

WorkBuddy 的 `zh-cn` 里有三组同构的 key：

```
progress.phase.model_streaming.<Tool>     正在准备<动作>     （如：正在准备读取文件）
progress.phase.tool_executing.<Tool>      正在<动作>          （如：正在读取文件）
progress.phase.waiting_for_permission.<Tool>  等待<动作>授权  （如：等待写入文件授权）
```

三组 × 100 多个工具名，**不是手写 300 句，而是"阶段词 + 工具动词"的笛卡尔积**；`<Tool>` 缺失时有 `progress.phase.tool_executing`（"调用工具中"）兜底。同一套 key 还被 `taskStatus`（规划中/执行中/等待输入）和 `automation.detail.reason.*`（25 条失败原因）复用同一套句式。

为什么它比任何一个界面都值得抄：

1. **它是所有界面的底座**。侧栏徽标、任务卡、后台胶囊、状态条、审批标题，说的都是同一件事的不同阶段。文案矩阵一变，全站一起变。
2. **它直接落在我们已有的文件上**。我们已经有 `tasks/tool-display.ts`（工具 → 中文标签）和 `bot/turns.ts`（步骤归纳），现在只补"阶段"这一维，不用改架构。
3. **它解决的是"它到底在干什么"**。这是我们和两家最大的体感差：我们的工具卡说"调用工具"，人家说"正在读取 package.json"→"等待写入授权"→"已改 3 个文件"。用户不需要展开任何东西就知道进展。
4. **抄起来最便宜**。一个 `phase` 字段 + 一张 `(phase, tool) → 文案` 表 + 一条兜底，`S` 量级，不涉及后端协议。

次选（如果只能改一处界面）：**计划确认门**（§3.3）。它是唯一一处"人家把它做成了必须跨过的门，我们做成了日志"，且直接对应"文件已经被改了而我没同意"这类不可逆后果。

**明确不建议抄的**：
- WorkBuddy 的 18 个设置面板（我们是 7 段，用户量级不同，先做搜索不拆页）；
- Qoder 的首页小游戏与热力图（是留存手段，不是能力）；
- WorkBuddy 的"分享赚积分"（`§artifactSlot.shareActivityGuide`，与产品定位冲突）。

---

## 7. 事实与推测的分界

### 7.1 读源码/语言包确认的（可直接引用）

- WorkBuddy 15 域的界面元素与状态文案：全部来自 `wb:renderer/assets/zh-cn-J68R5HFz.js`（12611 条）与组件分包，key 与行号已在正文标注。
- `StatusPlaceholder` 是单一共用组件（`StatusPlaceholder-B312aGar.js:1`，`type` 四值 + `onRetry`）。
- 计划确认浮层会顺手把权限模式切到 `bypassPermissions`（`exit-plan-mode-floating-BBsMz-w8.js:1` 内 `await a.setMode(t,'bypassPermissions')`）。
- 划选工具条三个动作、引用上限 20（`common-p2lSs_-n.js:1 §MessageQuoteSelectionOverlay`，`d>=20 return`）。
- 25 条自动化失败原因、8 态 taskStatus、12 态自动化卡片：均为语言包原文，已逐条列出。
- Qoder 16 个命名空间、字节偏移、各域 key 内容：来自 `qd:out/renderer/assets/index-DDa3WwIe.js` 直接解析。
- Qoder 的编辑重发四种失败、计划确认三动作、6 种审批资源卡：均为 `permissionResources` / `chatSessionResources` 原文。
- 我们自己的所有 `file:line`：直接读 `E:/projects/kbgai/kbgai/web/src` 源码。

### 7.2 推断的（有证据但结论是推的）

- "WorkBuddy 的 PlanBoard 是独立 tab、同一时刻只挂一份"：依据是 `installPlanBoardHost` 的重复注册 warn 文案与 `project-plan-federation-host` 的 flex 布局，**没有看到 PlanBoard 内部**（它是 federation remote 模块，不在 asar 内）。
- "Qoder 的任务监控 12 个开关是可独立开关的区块"：依据是设置段每个 `title/description` 成对出现，但没看到开关控件的实际渲染。
- "我们的后台任务只占状态条一格"：依据是 `goal-status-bar.tsx` 与 `background.ts`，未穷举 `bot-app` 全部引用点。

### 7.3 没查清的（不要当成结论用）

- WorkBuddy 计划看板（PlanBoard）内部的列/泳道结构、拖拽规则、里程碑交互 —— **remote 模块，包里没有**。
- WorkBuddy 的 `colleagues` 与 `collab` 在数据模型上是不是同一套成员表 —— 文案上"雇佣到项目/发布到项目"暗示是，但没读到接口。
- Qoder 的 Attention 收件箱如何判定"只有你能做的判断"（`whyNow/whyYou/reversibility` 是文案，判定逻辑在 CLI 侧）。
- 两个产品的**服务端**能力（如记忆生成、审计存储）—— 本次只盘前端。

---

## 附：一句话给决策者

WorkBuddy 值得学的不是"面板多"，是**它把状态写全了**：100 个工具 × 4 个阶段、25 条失败原因、12 种定时任务状态、6 种协作审批状态。我们 15 域里 13 域有实现，但每域都少一档状态、少一句人话、少一个入口——这就是"功能都有，用起来还是不知道发生了什么"的根因。
