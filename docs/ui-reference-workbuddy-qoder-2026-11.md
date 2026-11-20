# 同类产品界面考古（WorkBuddy / Qoder，2026-11）

## 0. 一句话结论

WorkBuddy 和 Qoder 在"人机交接"这件事上做的是**同一个决定**：审批卡、提问卡不进对话正文，而是作为**输入区容器的兄弟节点贴在输入框上方**（WB 是 `ConversationInputArea` 里的 `ConversationPendingPanelSlot`，Qoder 是 `bottom-full` 的 overlay / 输入框上方的 `region`）；同时把"它在干活"压缩成**一行会走光的动词短语**，把"它在等你"换成**另一行不同的字**（且不含时长），而不是同一个计时器换个颜色。我们现在的实现把这两件事都放在对话流里、都靠一个 `ThinkLine` 和一张卡，所以"等审批"看起来永远像"还在跑"。

---

## 1. 素材与方法（怎么拆的，可复现）

### 1.1 工具

`D:/kbg harness/asar.mjs`（零依赖 asar 读取器，已有）。四条命令：`list` / `cat` / `find` / `extract`。

```bash
# 列全库（12293 个文件）
node asar.mjs list "D:/Softwares/WorkBuddy/resources/app.asar" > wb_files.txt
node asar.mjs list "D:/Softwares/Qoder CN/resources/app.asar" > qoder_files.txt

# 按子串解出（只解需要的，整包 172MB 会爆盘）
node asar.mjs extract "<asar>" _arch/wb "renderer/assets/common-p2lSs_-n.js"

# 全库搜文本
node asar.mjs find "<asar>" "respondToPermission" "renderer/assets/"
```

另写了三个一次性小脚本（都在 `D:/kbg harness/_arch/`，不进产品仓）：

| 脚本 | 作用 |
|---|---|
| `i18nsearch.mjs <file> <regex>` | 从 `"key":\`value\`` 结构里抽出键值，并解码 `\uXXXX` |
| `zhkv.mjs <file> <regex>` | 同上，但先整体解码再扫（用于一行多语言的巨型语言包） |
| `linekv.mjs <file> <行号> <regex>` | 只解析某一超大行的键值（`conversation-message-output-hooks` 单行 8 万字符） |
| `classes.mjs <file>` | 抽 `className:` 字面量，用来找组件的 BEM 类名 |

### 1.2 路径记号（后文全部沿用）

| 记号 | 实际位置 |
|---|---|
| `wb:` | `D:/Softwares/WorkBuddy/resources/app.asar!` （内部路径紧随其后） |
| `qd:` | `D:/Softwares/Qoder CN/resources/app.asar!` |
| `us:` | `E:/projects/kbgai/kbgai/web/src/` |

### 1.3 行号的诚实说明（重要）

两个包都是 **rolldown/Vite 压缩产物**：一个 `.css` 常常只有 1 行（几十万字符），`.js` 里 UI 代码也普遍压在第 1 行或第 2 行。因此：

- **行号仍然给**，但它只用于"在这个文件的哪一段"，不能精确定位到语句。
- 精确锚点一律附**选择器名 / i18n key / 类名 / 唯一字符串**，例如
  `wb:renderer/assets/common-EkSkH3Q0.css:1 §.conversation-input-area`。
  复现方式：`node asar.mjs cat <asar> <path> | grep -o "<选择器>"`。
- 语言包是例外：`zh-cn-J68R5HFz.js` 有 500 行，`i18n-Bddor7Di.js` 里中文块在第 2 行，行号可用。
- 凡是**没有**给出可 grep 锚点、只写了"看起来像"的结论，一律标 `[推测]`。

### 1.4 两个包的形态差异（这决定了后面能问出什么）

| | WorkBuddy | Qoder |
|---|---|---|
| 体量 | `app.asar` 172MB / 12293 文件 | `app.asar` 139MB / 4499 文件 |
| 前端形态 | Vite 分包，**一组件一 `.js` + 一 `.css`，文件名保留组件名** | 单个 19MB `index-DDa3WwIe.js` + 467KB `index-yldrU_jD.css` |
| 样式 | BEM 类名（`.conversation-exit-plan-panel__option`）+ CSS 变量双层（`--cb-*` / `--wb-*` / `--cr-*`） | Tailwind 原子类内联（`squircle-xl border-border-tertiary`） |
| 文案 | i18n key → 多语言包，**能读出完整中文** | i18n key → 中文直接内联在 bundle 里（`permissionResources.zh`） |
| 对我们的可用度 | 高：组件名即结构，CSS 即数值 | 中：结构要从 JSX 的 className 串反推，但文案极完整 |

Qoder 是 VS Code 系（`product.json`、`out/main`、`out/preload`），AI 面板在 `out/renderer`。

### 1.5 关于 ChatGPT / Claude 桌面版

按要求检查了：

```
C:/Users/22103/AppData/Local/OpenAI/      → Codex/（CLI 数据） + extension/
C:/Users/22103/AppData/Local/Claude/      → logs/（只有日志）
C:/Users/22103/AppData/Local/Claude-Data/ → README.txt + claude_pzs8sxrjxfjjc/
```

**未找到桌面客户端本体**（没有 `app.asar`、没有 `resources/`）。本报告不含这两个产品的任何描述。

---

## 2. 页面地图

### 2.1 WorkBuddy 的面板清单（按导航归类）

**一、顶层导航（会话页左侧栏）**

来源：`wb:renderer/assets/zh-cn-J68R5HFz.js:474` 附近 `conversation.*`

| 分组 | 条目（原文） |
|---|---|
| 主功能 | `conversation.playground`＝任务、`conversation.colleagues`＝助理、`conversation.automation`＝定时任务、`conversation.expert`＝专家、`conversation.projects`＝项目、`conversation.space`＝资料库、`conversation.genie`＝应用·网站、`conversation.library`＝应用、`conversation.myFiles`＝我的文件、`conversation.myMailbox`＝我的邮箱 |
| 最近区 | `conversation.section.tasks`＝最近、`conversation.section.pinned`＝置顶、`conversation.section.groups`＝分组 |
| 过滤 | `conversation.section.filter`＝筛选、`conversation.section.recentFilter`＝筛选最近任务 |

**二、对话页右侧 / 浮层（就是我们 `panel.tsx` 的对位物）**

| 组件（文件名） | 职责 |
|---|---|
| `ConversationFlowPanel` | 对话流展示设置（简洁/详细） |
| `high-credit-approval-floating-panel` | 高积分工具审批浮层 |
| `exit-plan-mode-floating` | 计划确认浮层 |
| `background-tasks`（`background-tasks.module`） | 后台任务胶囊 + 展开面板 |
| `McpPoiConsentCard` | MCP 兴趣点授权卡 |
| `task-preview`（`task-preview-B81PNmRz.css` 30KB） | 任务预览 |
| `share-task-dialog` | 分享任务 |
| `PreviewIframe` / `media-preview` / `pdf-preview-component` / `sheet-preview-component` / `pptx-preview-component` / `docx-preview-component` / `excalidraw-preview-component` | 右侧产物查看器族（每个都是独立分包，说明是"按需加载的重型面板"） |

**三、设置（`settings.nav.*`，`wb:renderer/assets/zh-CN-…` 与 `zh-cn-J68R5HFz.js`）**

四个分组：`设置`(通用/外观/模型/快捷键) · `功能`(智能体/技能/连接器/对话流/应用管理/记忆与进化/实验室) · `数据与安全`(安全中心/数据管理/设备连接) · `关于我们`。

对应的 `*Panel` 组件（按文件名）：`GeneralSettingsPanel`、`ModelsSettingsPanel`、`PersonalizationPanel`、`MemorySettingsPanel`、`SecurityCenterPanel`、`DataManagementPanel`、`DataImportSettingsPanel`、`DeviceConnectionsPanel`、`ExtensionsSettingsPanel`、`ClawSettingsPanel`、`SubscriptionUsagePanel`、`AccountPanel`、`BuddyAppsPanel`、`LaboratoryPanel`、`GetHelpPanel`、`HelpFeedbackPanel`、`ConversationFlowPanel`。

> 观察：**设置项被拆成 18 个独立分包**，每个 Panel 一个 chunk。这意味着"面板"在他们那里是一等公民（有独立加载边界），不是一个 `<div>` 里 `if` 出来的一大坨。

### 2.2 Qoder 的面板清单

Qoder 没有 Panel 级分包，只能从文案命名空间反推。`qd:out/renderer/assets/index-DDa3WwIe.js` 里的顶层命名空间：

| 命名空间 | 对应的面板 |
|---|---|
| `chatActivity` | 对话流（回合状态、思考、工具、子 Agent） |
| `permission` / `permissionResources` | 权限确认（高危操作、MCP、飞书、目录、重复调用） |
| `chatActivity.question` | 提问卡 |
| `chatActivity.composer` | 输入区（模式、模型、权限、发送/停止） |
| `contextCompression` | 上下文窗口占用与压缩 |
| `chatSession` | 任务（session）列表与服务 |
| `usage` / `campaign` | 用量与权益 |
| `recordingNote` | 录音纪要（独立侧卡） |
| `filePreview` / `fileChange` | 文件预览 / diff |
| `permissionModeLabels` | 输入框里的权限模式选择（询问审批/自动审批/完全访问） |

与 WorkBuddy 相比 Qoder 的面板数**少得多但更深**：一个 `chatActivity` 里塞了 40+ 工具名词、6 种 hook 状态、子 Agent 角色表。

---

## 3. 专题

### 专题 1：权限确认 / 审批

#### 人家怎么做

**位置：不在对话流里，是输入区容器的兄弟节点，贴在输入框正上方。**

WorkBuddy 的挂载点是 `ConversationInputArea`（整个底部输入区的容器）。它按固定顺序竖排若干槽位，审批卡是其中之一：

```js
<div className="conversation-input-area" data-testid="conversation-input-area">
  {!P && g.hasActiveTasks && !I && <BackgroundTaskCapsule tasks={…}/>}
  <ConversationModelCampaignNoticeSlot state={b} attached={R}/>
  {M && h.bar ? <Kx {...h.bar}/> : null}
  {j ? <ConversationErrorBannerSlot .../> : null}
  {!P && !A.banner && (f || p || d.state === `pending`) ? <ConversationPendingPanelSlot/> : null}   {/* ← 审批/提问 */}
  {!A.banner && F ? <PendingSessionFallbackCard cancel={…}/> : null}                                {/* ← 兜底 */}
  {P && !A.banner ? <div className="conversation-input-area__custom-floating">{S}</div> : null}
  <div className="conversation-input-area__input-stack">
    {…prompt-queue overlay…}
    {!P && L ? y.dependencyGateNode : null}
    {!N && !L ? a : null}          {/* ← inputTopSlot */}
    <oFe .../>                      {/* ← 输入框本体 */}
  </div>
  <w$/>
</div>
```
— `wb:renderer/assets/common-p2lSs_-n.js:1 §ConversationInputArea`

容器样式：

```css
.conversation-input-area{
  box-sizing:border-box; width:100%; max-width:100%;
  padding:0 var(--wb-spacing-7,24px);   /* 左右各 24px */
  flex-direction:column; flex-shrink:0; margin:0 auto; display:flex
}
/* 同类卡片的间距约定：下边留 12px，和输入框分开 */
.conversation-input-area .conversation-input-area__poi-pick{
  border-radius:24px; width:100%; margin:0 0 12px; overflow:hidden;
  background:var(--cb-question-floating-card-bg);
  border:1px solid var(--cb-question-floating-card-border);
  box-shadow:0 6px 16px #00000005, 0 1px 4px #00000008
}
```
— `wb:renderer/assets/common-EkSkH3Q0.css:1 §.conversation-input-area`

**注意：容器本身没有边框、没有底色。** 卡只是这个纵向 flex 栈里的一项，`margin-bottom:12px` 与输入框拉开。所以"卡片浮在输入框上方"是**布局事实**，不是 `position:absolute` 的技巧。

三个细节值得抄：

1. **顺序即优先级**：任务胶囊 → 公告 → 积分条 → 错误条 → 审批卡 → 兜底卡。审批永远在错误条下面、输入框上面。
2. **互斥由 `deriveConversationInputAreaComposition(...)` 统一算**，不是每个组件自己 `if`。它返回 `banner / replacement / accessories` 三组；`replacement.kind === 'pendingState'` 表示"有 pending 就把别的替换掉"（原文：`P = A.replacement?.kind === 'customFloating'`、`F = A.replacement?.kind === 'pendingState'`）。
3. **没有卡时整项不渲染**（`{cond && <Slot/>}`），不是渲染一个空 div。

> 补一条容易走错的路：`common-EkSkH3Q0.css` 里还有一个 `.conversation-floating-slot-layer`（`position:absolute` + `column-reverse` + `:empty{display:none}`），**它不是审批卡的容器**，而是给插件用的扩展槽（`conversationFloatingSlot` contribution，锚点只有 `content-bottom` / `composer-top` 两种，见 `wb:renderer/assets/common-p2lSs_-n.js:1 §ConversationFloatingSlotLayerImpl`）。我在第一版里认错了，这里更正。

Qoder 同一决定，两种落位：

```js
// 审批：贴在输入框上沿，自身滚动，最高 55vh/440px
<div className="pointer-events-none absolute inset-x-0 bottom-full z-30 px-5 pb-0"
     role="region" aria-label={t("chatActivity.question.toolConfirmRegion")}>
  <div className="pointer-events-auto relative mx-auto max-h-[min(55vh,440px)]
                  w-full max-w-[var(--qba9c19)] overflow-y-auto rounded-xl">
    <PendingPermissionInteraction .../>
```
```js
// 提问：输入框正上方的兄弟块，不浮，占位
<div className="shrink-0 bg-bg-layout px-5 pb-1.5 pt-1"
     role="region" aria-label={t("chatActivity.question.answerRegion")}>
```
— `qd:out/renderer/assets/index-DDa3WwIe.js:1 §PendingInteractionOverlay`

而且**同屏只渲染第一条**：`const i1 = n1[0]`，其余用「还有 N 个」计数（见下）。`aria-label` 把两个区域区分成「工具执行确认」和「回答 Agent 的问题」——同样一个框，读屏用户能听出是哪种。

**整宽还是内嵌：**整宽（`inset-x-0` / `width:100%`），但两边留 20–24px。卡片自身圆角 24px（WB）/ `rounded-xl`＝12px（Qoder）。**它不是气泡，是一个独立面板。**

**边框/底色怎么表达"要你动手"：**

WB 高积分审批卡（`wb:renderer/assets/high-credit-approval-floating-panel-C7R_cGE7.css:1`）：

```css
.high-credit-approval-floating{
  background:var(--cb-question-floating-card-bg);           /* 深色 #181b20 / 浅色 #fff */
  border:1px solid var(--cb-question-floating-card-border);  /* #ffffff14 / #00000014 */
  border-radius:24px; overflow:hidden;
  box-shadow:0 6px 16px #00000005, 0 1px 4px #00000008;
  animation:.2s ease-out highCreditApprovalSlideUp         /* translateY(8px) → 0 */
}
.high-credit-approval-floating__header{padding:20px 24px 12px}
.high-credit-approval-floating__title{font-size:16px;font-weight:500;line-height:24px}
.high-credit-approval-floating__risk-tag{color:#ff7800;font-size:14px;font-weight:500;line-height:22px}
.high-credit-approval-floating__description{
  color:var(--wb-color-text-tertiary); margin-top:8px; font-size:15px; line-height:20px
}
.high-credit-approval-floating__option{height:40px;padding:8px;border-radius:12px}
```

注意：**底色不是警告色**，只有 `__risk-tag` 一个 `#ff7800` 的字。整卡用"浮起来 + 24px 圆角 + 极轻阴影 + 8px 上滑入场"来表达"这是新来的、要你处理"，而不是用红黄底。

我们的做法相反：`us:components/bot/turn.tsx:141` 是 `border-warn/35 bg-warn/[0.04]`——整张卡泡在警告色里。这会让"普通确认"和"危险操作"看起来一样重。

**给几个选项、文案是什么：**

高积分工具（`wb:renderer/assets/high-credit-approval-floating-panel-DemeEjgI.js:1`）：

| 序号 | 文案（原文） | 视觉 |
|---|---|---|
| 1 | `允许`（scope=session 时）/ `确认`（其他） | 带 → 箭头 |
| 2 | `本次会话始终允许`；`computerUse` 且无 scope 时改用 `本次启动内始终允许` | 带 → 箭头 |
| 3 | `拒绝` | **不带箭头**，类名 `__option--reject` |

来源：`wb:renderer/assets/zh-cn-J68R5HFz.js:472` `tool.approval.allow / alwaysAllow / alwaysAllowThisLaunch / reject`。

命令行审批用另一套更短的标签：`允许 / 始终允许 / 拒绝`，标题 `确认执行命令？`（`wb:renderer/assets/zh-cn-J68R5HFz.js` `tool.executeCommand.*`）。

Qoder 的普通权限卡（`qd:…/index-DDa3WwIe.js:1 §permissionResources.zh`）：

```
title: 高危操作
requestTitle: 允许“{{toolName}}”？
mcpRequestTitle: 允许 MCP 工具 {{toolName}}？
description.default: {{toolName}} 想要执行一个操作
deny: 拒绝    allow: 允许    allowOnce: 允许一次
pendingCount: 还有 {{count}} 个权限请求待处理
```

**"允许并记住"怎么表达——这是两家最大的分歧点，也是我们最该学的一处。**

Qoder 不做"一个 alwaysAllow 按钮"，而是把"记住"做成**追加建议（suggestion）**，每条建议自己说清"记住的是什么、记多久"：

```
suggestion.behavior.rules.add.allow  = 本次任务允许
suggestion.behavior.rules.add.deny   = 本次任务拒绝
suggestion.behavior.rules.add.ask    = 本次任务仍询问
suggestion.behavior.rules.replace     = 替换本次任务的{{behavior}}规则
suggestion.behavior.rules.remove      = 移除本次任务的{{behavior}}规则
suggestion.behavior.directories.add   = 本次任务始终允许访问“{{scope}}”
suggestion.behavior.directories.remove= 移除本次任务的目录访问：“{{scope}}”
suggestion.behavior.mode              = 本次任务使用“{{mode}}”模式
```
— `qd:out/renderer/assets/index-DDa3WwIe.js:1 §suggestion`

代码里对应 `actions.allowOptions: permissionSuggestions?.map(...)`（见 `§PermissionCard` 调用处）。也就是说：**基础动作只有"拒绝/允许一次"两个，第三、第四个按钮是运行时算出来的**，先算"这次命中了什么规则、能推导出哪几条更省事的授权"，再渲染。它天然解决了"alwaysAllow 到底允许了什么"——按钮文字本身就是答案。

WB 的做法是固定两档：`本次会话始终允许` / `本次启动内始终允许`，并在按钮下面补一句档位警告（我们抄了这个：`us:components/bot/turn.tsx:191-199` 的 `grants.filter(o=>o.warning)`）。

WorkBuddy 还有一层 Qoder 没有的东西：**审批模式是一个全局设置，不是一个按钮**。

```
settings.securityCenter.assistedApproval.title = AI 智能审批
settings.securityCenter.assistedApproval.mode.off      = 关闭
settings.securityCenter.assistedApproval.mode.riskHint = 仅风险提示
settings.securityCenter.assistedApproval.mode.silent   = 自动审批
```
— `wb:renderer/assets/zh-cn-J68R5HFz.js:472`

Qoder 把同一个东西放在输入框里：`permissionModeLabels.zh = {acceptEdits:"询问审批", auto:"自动审批", bypassPermissions:"完全访问"}`（`qd:…:1`），并以 `ChatInputSelector ariaLabel:"访问权限" fallbackLabel:"默认权限"` 的形式出现在输入区 footer（`qd:…:1 §ChatInput`）。**"要不要问"这件事在动手之前就定了，卡上就不用再给"别再问我"的按钮。**

**拒绝要不要理由？理由输入在哪、什么形态、必填吗？**

- **WB 高积分 / 命令行审批：不要理由。** 拒绝 = `respondToPermission(t,n,[])` 直接返回空数组（`wb:renderer/assets/high-credit-approval-floating-panel-DemeEjgI.js:1`）。没有输入框。
- **WB 计划审批：要，而且理由就是"调整意见"。** 计划卡的第 2 项不是一个选项，是一个**永远可见的输入行**：

```js
// 第 1 行：开始执行（带箭头）
// 第 2 行：<input placeholder="告诉我如何调整计划，我会补充相应内容">
// 底部：一条分隔线 + 右侧 32px 圆形发送按钮
```
— `wb:renderer/assets/common-p2lSs_-n.js:1 §PendingPlanPanel`

文案：`tool.exitPlanMode.confirmTitle = 确认按此计划执行？`、`confirmDescription = 确认后，我将按此计划执行并输出内容`、`yes = 开始执行`、`adjustPlaceholder = 告诉我如何调整计划，我会补充相应内容`（`wb:renderer/assets/zh-cn-J68R5HFz.js:477`）。

回车的行为是**按输入内容分叉**：`Enter` 且输入框非空 → 提交调整意见；`Enter` 且为空 → 批准。原文：

```js
e.key !== `Enter` || t.isComposing || (e.preventDefault(),
  i.trim() ? g().catch(...) : u && h().catch(...))
```
— `wb:renderer/assets/common-p2lSs_-n.js:1 §PendingPlanPanel.onKeyDown`

（`t.isComposing` 是中文输入法保护，我们的 `composer.tsx` 有，`approval.tsx` 没有。）

- **Qoder 计划确认：三个动作，中间那个就是"写反馈"。** `title:"实施此计划？"` / `approveChoice:"是，实施此计划"` / `requestChanges:"告诉 Qoder 计划需要如何调整"` / `feedbackPlaceholder:"告诉 Qoder 计划需要如何调整..."` / `next:"发送"` / `skip:"暂不实施"`（`qd:…:1 §permission.exitPlan`）。组件 `PlanConfirmationCard` 用 `labels` 一次传入 7 个字符串（`qd:…:1 §ExitPlanModeComposerInteraction`）。

- **必填吗？不。** WB 的调整行留空就变成"批准"，Qoder 的 `skip:"暂不实施"` 是独立的第三个动作，不必写字也能退。**没有一家把理由做成必填。**

**Esc / 数字键？**

**两家都没有数字键。** 证据：

- WB 高积分浮层的键盘只处理两个键：`(e.key === "Enter" || e.key === " ")`（`wb:renderer/assets/high-credit-approval-floating-panel-DemeEjgI.js:1`，三处选项都是这两个键）。
- WB `exit-plan-mode-floating` 同样只有 `Enter` / `Space`（`wb:renderer/assets/exit-plan-mode-floating-BBsMz-w8.js:1`）。
- Qoder 全部用真 `<Button>`，靠 Tab/Enter，且按钮上写 `aria-busy`（`qd:…:1 §PendingPermissionInteraction`）。
- 全库搜 `keydown` + `Escape` 在两家的审批卡里都**未找到**。

**结论：我们的 1–N + Esc 是超前的，不要砍。** 但我们的实现有个结构性问题——见"差距"第 3 条。

**审批过期 / 失效怎么表达？过期后卡片变成什么？有没有重试入口？**

**WorkBuddy 没有"过期卡"这个概念。** 全库搜 `审批.*过期 / 审批.*失效` 只找到 `tool.exitPlanMode.adjustStale = 当前计划请求已失效，请重新发起调整`（`wb:renderer/assets/zh-cn-J68R5HFz.js:477`）和 `securityCenter.audit.sensitiveProtection.approvalTimeout = 审批超时，已拒绝本次操作`（同文件:432）。

它的做法是**不保留死卡，而是把卡片整体替换成"一句话 + 一个还能用的动作"**：

```js
// 有决策了（批准/拒绝/过期/触发过）→ 整卡塌成一行
.conversation-high-credit-approval__decision{padding:20px 24px;font-size:13px;line-height:20px}
.conversation-high-credit-approval__decision--approved{color:var(--cb-success-color,#00b42a)}
.conversation-high-credit-approval__decision--rejected{color:var(--cb-error-color,#f53f3f)}
```
— `wb:renderer/assets/common-EkSkH3Q0.css:1 §.conversation-high-credit-approval__decision`

计划卡同理，而且**失败态带重试链接**：

```css
.conversation-exit-plan-panel__decision{display:flex;gap:8px;padding:20px 24px;font-size:13px;line-height:20px}
.conversation-exit-plan-panel__decision--approved{color:var(--wb-status-success)}
.conversation-exit-plan-panel__decision--failed{color:var(--wb-color-text-error-default)}
.conversation-exit-plan-panel__retry{text-decoration:underline;cursor:pointer;background:0 0;border:0}
```
— `wb:renderer/assets/common-EkSkH3Q0.css:1`

对应代码把 3 种错误码翻译成 3 种人话：

```js
t.error === `plan-adjustment-idle-timeout`      ? a(`tool.exitPlanMode.adjustTimeout`)
: t.error === `plan-adjustment-pending-stale`   ? a(`tool.exitPlanMode.adjustStale`)
: t.error === `plan-adjustment-send-failed`     ? a(`tool.exitPlanMode.adjustFailed`)
: t.error
```
— `wb:renderer/assets/common-p2lSs_-n.js:1 §ExitPlanModePanel`

**我们的 `approval.expired` 灰卡 + "重跑这一轮" 在方向上是对的、颗粒度甚至更细**（我们区分了"过期"和"失败"），但位置不对：它留在原地，用户还得往上翻。WB 是让整张卡**变形**成一行状态，视觉噪声直接消失。

**Qoder 也没有过期态。** 只有进度态：`cancelTaskPending:"正在终止任务…"` / `decisionPending:"正在发送选择…"` / `decisionFailed:"选择未发送，请重试。"`（`qd:…:1 §permissionResources.zh`）。发送失败时给红条 `role="alert"`，按钮不禁用，直接可以再点（`qd:…:1 §PendingPermissionInteraction` 里 `W1`/`G1` 两个 banner）。

**有没有"独立审查员 / 风险提示"这类二级信息？放在哪一级？**

**有，而且是两家都做的、且都放在"同一张卡的次级位"而不是另开一个面板。**

WorkBuddy 的 `SandboxApprovalCard` 把审查结论挂成**卡片自带的 riskTag + tooltip**：

```js
decorateCard: e => ({...e, riskTag:{
  level:`medium`,
  text: n(`tool.riskReview.badge`),        // 「AI 安全防护」
  variant:`badge`,
  tooltip: <> {n(`tool.riskReview.explanationTooltip`)}  {/* 「由 AI 审查生成，供参考」*/}
              + 积分 + 「耗时：{seconds} 秒」 </>
}}),
defaultChoice: t.decision === `allow` ? `allow_once` : `deny`,
choices: t.decision === `deny`
  ? e.choices.map(c => c.value === `deny` ? {...c, label: n(`tool.riskReview.block`)} : c)  // 「拦截」
  : e.choices
```
— `wb:renderer/assets/common-p2lSs_-n.js:1 §SandboxApprovalCard`

四个设计决定值得逐条抄：

1. **审查员的结论不抢标题**——它是一个橙色小字 `AI 安全防护`，鼠标悬停才展开"由 AI 审查生成，供参考"。
2. **审查员会改默认项**：它说 allow → 默认选中"允许一次"；它说 deny → 默认选中拒绝，并且把那个按钮改名叫 `拦截`。
3. **审查员的话不会被当成授权**：tooltip 原文 `由 AI 审查生成，供参考`。
4. **审查耗时/积分也是二级信息**，藏在同一个 tooltip 里，主卡面上看不到。

`tool.riskReview.*` 全部文案（`wb:renderer/assets/zh-cn-J68R5HFz.js:472`）：

```
badge = AI 安全防护
confirmTitle = 确认执行 {tool}？
blockTitle  = 拦截 {tool} 的风险操作？
allowTitle  = 允许执行 {tool}？
block = 拦截    cancel = 取消    executeAnyway = 仍要执行
explanationTooltip = 由 AI 审查生成，供参考
credit = 积分消耗    duration = 耗时：{seconds} 秒
summary.content_authorization = 返回内容疑似冒充用户或系统授权、要求绕过权限约束。
summary.content_exfiltration  = 返回内容疑似诱导 Agent 泄露或外发数据。
summary.content_execution     = 返回内容疑似诱导 Agent 执行危险代码或命令。
summary.review_unavailable    = 安全审核暂不可用，需要人工确认本次动作。
```

**二级信息的第二层**：决策已经发生之后，审查细节沉到**工具调用行的折叠区**里，不在卡上：

```css
.cr-tool-call__security-summary{cursor:pointer;display:inline-flex;gap:8px}   /* <summary>，原生折叠 */
.cr-tool-call__security-details{background:var(--cr-bg-primary-default);border-radius:16px;padding:12px 16px;display:grid;gap:8px}
.cr-tool-call__security-decision{font-size:12px;font-weight:500;line-height:18px}
.cr-tool-call__security-reason{color:var(--cr-text-secondary);font-size:12px;line-height:18px}
.cr-tool-call__security-preview{border-left:2px solid var(--cr-border-default);padding-left:8px}
```
— `wb:renderer/assets/src-BllqCp4U.css:1 §.cr-tool-call__security*`

即三级信息模型：**卡上一行橙字（一级）→ 悬停 tooltip 一句免责 + 耗时（二级）→ 事后折叠在工具行里（三级）**。我们目前只有 `guardianReviewLabel` 一句话（`us:components/bot/turn.tsx:160`），一级都还没做稳（没有徽标、没有默认项改写）。

Qoder 的对应物叫 `explain`：

```
explain: 解释
analyzingIntent: 正在分析操作意图...
explainFailed: 暂时无法解释此操作。你仍可直接允许或拒绝，也可以重试解释。
toolArguments: 工具参数
collapse: 收起    expand: 展开完整内容
```
— `qd:…:1 §permissionResources.zh`

它是卡脚上的一个**次要按钮**（`footerAction:{label:"解释"}`，`disabled: w1`，加载时显示"正在分析操作意图..."转圈）。解释结果是 `commandIntent`——一句自然语言，替换掉裸命令：

```js
y1 = d1 ? b1 : (c1 ?? b1)          // c1 = input.commandIntent（模型给的意图），b1 = 解释结果
...
command: L1, commandIntent: y1, isLoadingIntent: w1
```
— `qd:…:1 §PendingPermissionInteraction`

**这也是我们该学的：卡片主位放"它想干什么"的人话，命令原文降为次级。**

#### 原文证据（汇总）

| # | 事实 | 位置 |
|---|---|---|
| 1 | 审批卡是 `ConversationInputArea` 的兄弟节点，紧贴输入框上方，不在对话流中 | `wb:renderer/assets/common-p2lSs_-n.js:1 §ConversationInputArea` |
| 1b | 输入区容器：纵向 flex、左右 24px、卡与输入框之间 12px、容器本身无边框无底色 | `wb:renderer/assets/common-EkSkH3Q0.css:1 §.conversation-input-area` |
| 1c | 槽位互斥由 `deriveConversationInputAreaComposition` 统一裁决（banner / replacement / accessories） | `wb:renderer/assets/common-p2lSs_-n.js:1 §ConversationInputArea` |
| 2 | 输入区容器纵向 flex、左右 24px、卡与输入框 12px、容器无边框 | 同上 |
| 3 | Qoder 审批卡 `absolute bottom-full z-30`，`max-h:min(55vh,440px)` 自滚 | `qd:out/renderer/assets/index-DDa3WwIe.js:1 §PendingInteractionOverlay` |
| 4 | Qoder 同屏只渲染第一条审批，其余给计数 | 同上（`n1[0]`）+ `pendingCount:"还有 {{count}} 个权限请求待处理"` |
| 5 | 三选项：允许 / 本次会话始终允许 / 拒绝；拒绝不带箭头 | `wb:renderer/assets/high-credit-approval-floating-panel-DemeEjgI.js:1` |
| 6 | 卡片 24px 圆角、1px `#ffffff14` 边框、标题 16/24/500、风险字 `#ff7800` 14/22 | `wb:renderer/assets/high-credit-approval-floating-panel-C7R_cGE7.css:1` |
| 7 | 决策后整卡塌成一行 13/20 的彩色状态句 | `wb:renderer/assets/common-EkSkH3Q0.css:1 §…__decision` |
| 8 | 计划卡第 2 项是常驻输入行，Enter 按有无内容分叉 | `wb:renderer/assets/common-p2lSs_-n.js:1 §PendingPlanPanel` |
| 9 | 失效/超时/发送失败各一句人话 + 下划线重试 | `wb:renderer/assets/common-p2lSs_-n.js:1 §ExitPlanModePanel` |
| 10 | 审查员＝橙色徽标 + tooltip，且会改默认选中项与拒绝按钮文案 | `wb:renderer/assets/common-p2lSs_-n.js:1 §SandboxApprovalCard` |
| 11 | 三层降级：全卡 → `{tool} 等待确认`+拒绝 → `等待授权`+`停止当前任务` | `wb:renderer/assets/common-p2lSs_-n.js:1 §ConversationPendingPanel` / `§PendingSessionFallbackCard` |
| 12 | 降级文案原文 | `wb:renderer/assets/conversation-message-output-hooks-aYfim7OW.js:20` |
| 13 | Qoder「记住」是运行时算出的追加建议，不是固定第三按钮 | `qd:…:1 §suggestion` |
| 14 | Qoder 权限模式在输入框里选（询问/自动/完全访问） | `qd:…:1 §permissionModeLabels` + `§ChatInput` |
| 15 | 两家都没有数字键 / Esc 处理 | `wb:…/high-credit-approval-floating-panel-DemeEjgI.js:1`（只有 Enter/Space） |

**第 11 条值得单独抄下来。** WorkBuddy 的 pending 面板是一个**按 category 分发的路由**，最后两级兜底是这样的：

```js
function ConversationPendingPanel({pending, resolve, reject, ...}) {
  if (isExitPlanModePending(t) || (!t && planAdjustment)) return <ExitPlanModePanel .../>;
  if (!t) return null;                                  // ← 没有 pending 就什么都不画
  ...
  if (category === `tool` && options?.length)  return <PendingToolApprovalCard .../>;
  if (category === `plan`)                     return <PendingPlanPanel .../>;
  if (category === `question` && questions?.length) return <PendingQuestionPanel .../>;
  if (category === `sandbox` && options?.length) return <PendingSandboxPanel .../>;
  if (isMcpConnectionPending(t))               return <PendingMcpConnectionPanel .../>;
  if (options?.length)                         return <PendingOptionsPanel .../>;
  return <PendingFallbackPanel pendingId={t.id} toolName={t.toolName} reject={reject} />;  // ← 兜底
}
```
— `wb:renderer/assets/common-p2lSs_-n.js:1 §ConversationPendingPanel`

兜底卡：

```js
function PendingFallbackPanel({pendingId, toolName, reject}) {
  const a = toolName ? r(`pending.fallback.waitingWithTool`, {toolName})
                     : r(`pending.fallback.waiting`);
  return <div className="pending-fallback-panel" aria-live="polite">
    <Qc description={a} choices={[{value:`reject`, label:r(`pending.reject`)}]}
        onSubmit={i} onSkip={i}/>
  </div>
}
```
— 同上 `§PendingFallbackPanel`

文案：`pending.fallback.waitingWithTool = {toolName} 等待确认`、`pending.fallback.waiting = 等待确认`、`pending.reject = 拒绝`。

再下一级（连 pending 对象都没有、但服务端说"在等你"）：

```js
function PendingSessionFallbackCard({cancel}) {
  return <div data-testid="pending-session-fallback-card" aria-live="polite">
    <Qc description={t(`pending.stateFallback.title`)}
        command={t(`pending.stateFallback.description`)}
        choices={[{value:`cancel`, label:t(`pending.stateFallback.cancel`)}]}
        onSubmit={() => e()} onSkip={() => e()}/>
  </div>
}
```
— 同上 `§PendingSessionFallbackCard`

文案：`pending.stateFallback.title = 等待授权`、`description = 授权请求详情正在加载，可稍候或停止当前任务`、`cancel = 停止当前任务`（`wb:renderer/assets/conversation-message-output-hooks-aYfim7OW.js:20`）。

**这正是我们第 4 条 bug 的解药**：不管数据齐不齐，永远有一个能渲染的东西；最坏的情况也告诉用户"在等你、你可以停"。

#### 我们怎么做

| 位置 | 现状 | 证据 |
|---|---|---|
| 卡片容器 | `approval.tsx` 选项组 + `turn.tsx` 的 `ApprovalCard`，**渲染在 `props.approvals.map(...)` 即对话流正文里** | `us:components/bot/turn.tsx:538` |
| 边框/底色 | `border-warn/35 bg-warn/[0.04]`——整卡警告色 | `us:components/bot/turn.tsx:141` |
| 选项 | `grants.map` 顺序渲染 + 末尾 `不允许`；序号 `1.` `2.` `3.`；默认首项加 `›` 与 `ring-1` | `us:components/tasks/approval.tsx:73-116` |
| 记住 | 靠 `approvalGrantOptions` 返回的 grant 列表（once / prefix / tool），每个 grant 自带 `warning` | `us:components/bot/turn.tsx:191-199` |
| 拒绝理由 | 点"不允许"才展开，理由挂在列表外、缩进到选项文字列 | `us:components/tasks/approval.tsx:119-160` |
| 键盘 | `tabIndex={0}` 容器 + `1..N` + `Esc` 直接拒绝 | `us:components/tasks/approval.tsx:49-63` |
| 过期 | 同一张卡改灰 + 一段解释 + "重跑这一轮" | `us:components/bot/turn.tsx:166-178` |
| 审查员 | 一行 `guardianReviewLabel(approval.guardian)`，无徽标、无默认项改写 | `us:components/bot/turn.tsx:159-163` |
| 兜底 | 无。数据不来就只剩一句"等你批准" | — |
| 审查员的免责 | 写在 `title` 属性里：`独立审查员先看过了；它放行不等于安全，它说话也不会替你批准` | `us:components/bot/turn.tsx:160` |

#### 差距

1. **位置错了（也是第 1、3、4 条 bug 的共同根因）。** 卡在正文流里，所以：① 用户滚上去就找不到了；② 等待时正文区没有"有新东西"的视觉，用户以为卡住了；③ 卡片渲染条件依赖事件流，而"等审批"恰恰不产生事件。
   → 应迁到输入区里、输入框上方的槽位（WB 的做法是 `ConversationInputArea` 内的 `ConversationPendingPanelSlot`），且**槽位的存在与否由 `pendingApprovals.length` 决定，不由事件驱动**。

2. **警告色用错了对象。** 一张卡整底 `warn/4%` + 边框 `warn/35%`，等于说"这次操作很危险"。但绝大多数审批是普通的 `Read`/`Bash ls`。WB 只在风险标签上用一个 `#ff7800`，卡片本体是中性的 `#fff`/`#181b20`。
   → 卡底色改成 `surface`，风险用一行小字或徽标。

3. **键盘焦点是"容器级"的，容易和输入框打架。** 我们现在靠 `tabIndex={0}` 的 `div` 上挂 `onKeyDown`，用户必须先 Tab 到这个 `div`。而 WB/Qoder 用的是真 `<button>`，Tab 顺序天然正确。同时我们的 `Explaining` 分支 `return` 掉了所有键盘处理（`approval.tsx:52`），导致展开后 `1/2` 失效——这是有意的，但用户不知道。
   → 保留 1–N/Esc，但把容器改成 `role="radiogroup"` + 每项 `role="radio"`，让 Tab 落在第一项，数字键在 `onKeyDown` 里 `document.activeElement` 判定。

4. **没有兜底卡。** 第 4 条 bug 的直因。WB 有三层，我们只有零层。

5. **没有"多张待审批"的处理。** WB 在同一个纵向栈里按优先级排（任务胶囊 → 公告 → 错误条 → 待处理卡 → 兜底卡，见 `§ConversationInputArea` 的子节点顺序）；Qoder 只渲染第一张 + `还有 N 个`。我们现在 `approvals.map` 全渲染在正文里，长会话里会变成一堵卡片墙。

6. **审查员信息只有一句话，且藏在 `title` 里。** 移动端/触屏无法 hover，等于没有。

---

### 专题 2：计划与进度

#### 人家怎么做

**有没有把任务拆成步骤展示？在哪显示？**

两家三种落位，**没有一家放在对话流正文里做主视觉**：

| 产品 | 步骤在哪里 | 证据 |
|---|---|---|
| WorkBuddy | ① 计划正文自动开在**右侧产物面板**；② 卡片里只放一段 220px 高的预览 | `wb:renderer/assets/common-p2lSs_-n.js:1 §usePendingPlanAutoOpen`；`wb:renderer/assets/exit-plan-mode-floating-D4Toe2nG.css:1 §__plan-content{max-height:220px;overflow:auto}` |
| WorkBuddy（todo 工具） | 折叠在对话流的"过程"里，四项状态 | `wb:renderer/assets/zh-cn-J68R5HFz.js:474 §todoList.status.*` |
| Qoder | 计划本身是 **Spec 面板**里的一个文件；卡上只给「查看详情」 | `qd:…:1 §exitPlan.viewPlan:"查看详情"` / `planUnavailable:"暂时无法读取计划详情，可打开 Spec 面板查看"` |

WB 的"计划自动开右栏"值得单独看：

```js
function usePendingPlanAutoOpen({pending, conversationId, hostReady, openDetail}) {
  const o = resolvePendingPlanPreview(pending);   // {filePath, title, content}
  ...
  useEffect(() => {
    hostReady && a && s && claimPendingPlanAutoOpen(`${n}:${a}:${s}:${l?"with-content":"path-only"}`)
      && i({view:`artifacts`, filePath:s, artifactType:`implementation-plan`, title:c, content:l});
  }, [...]);
}
```
— `wb:renderer/assets/common-p2lSs_-n.js:1 §usePendingPlanAutoOpen`

`claimPendingPlanAutoOpen` 用 `Set` 去重且上限 200，**同一份计划只自动打开一次**——不会每次重渲染都把用户正在看的别的东西顶掉。

**这直接回答我们的一个隐含问题：计划该不该占对话流？答案是"给一个入口，别给全文"。**

**步骤状态有哪几种？当前进行中的那一步怎么强调？**

WorkBuddy 的 todo 四态（`wb:renderer/assets/zh-cn-J68R5HFz.js:474`）：

```
todoList.status.waiting    = 等待
todoList.status.inProgress = 进行中
todoList.status.completed  = 已完成
todoList.status.error      = 错误
```

注意是 **waiting / inProgress / error**，不是 pending/running/failed —— 措辞偏"人话"。而且**没有 skipped 态**（用户拒绝后那一步不进 todo，而是整轮进 `cancelled`）。

更细的工具级状态机（10 态，`wb:renderer/assets/main-content-core-D6Z9wzXe.css:1 §.status-text.status-*`）：

```
idle · parsing · pending · stream_executing · full_executing
executed · failed · cancelled · skipped · destroyed
```
配色：
```css
.status-text.status-idle,
.status-text.status-parsing,
.status-text.status-pending,
.status-text.status-stream_executing,
.status-text.status-full_executing { color: var(--color-accent) }      /* 蓝：在跑 */
.status-text.status-executed  { color:#238636 }   /* 绿 */
.status-text.status-failed,
.status-text.status-cancelled { color:#d1242f }   /* 红 */
.status-text.status-skipped,
.status-text.status-destroyed { color: var(--color-text-muted) }  /* 灰 */
```

**当前那一步怎么强调——WB 用的是"动词短语 + 扫光"，不是高亮某一项：**

```css
.cr-collapse--streaming > .cr-collapse__header > .cr-collapse__title{
  background:linear-gradient(110deg,
    color-mix(in srgb,var(--cr-text-primary) 26%,transparent) 0%,
    … 35%, … 55% …, … 65%, … 100%);
  -webkit-text-fill-color:transparent; color:#0000;
  background-size:200% 100%; background-clip:text;
  animation:2.2s linear infinite cr-text-shimmer-sweep;
}
.cr-collapse--streaming.cr-collapse--streaming-shimmer-suppressed > .cr-collapse__header > .cr-collapse__title{
  color:var(--cr-text-tertiary); -webkit-text-fill-color:currentcolor; background:0 0; animation:none;
}
```
— `wb:renderer/assets/src-BllqCp4U.css:1 §.cr-collapse--streaming`

一行标题在跑的时候有 2.2s 的文字扫光，跑完变成静止的三级灰。**只有一处动画，且动画本身就是"这一项在进行中"的标识**——不需要额外的高亮行、进度条、闪烁圆点。

**用户拒绝 / 打断后，计划怎么标？失败的那一步怎么标？**

- 整轮被打断：`metaFold.turnCancelled = 已取消` / `turnCancelledWithDuration = 已取消 {duration}`（`wb:renderer/assets/i18n-Bddor7Di.js:2`），配 `.cr-cancelled-indicator{color:var(--cr-text-tertiary);font-size:15px;line-height:20px}`（`wb:renderer/assets/src-BllqCp4U.css:1`）——**灰色、同字号、不加图标**。
- 工具级失败：`metaFold.error / errors` 计数，并在摘要里拼 `执行工具 {count} 次，其中 {failedCount} 次失败`（Qoder 的写法，`qd:…:1 §chatActivity.completedToolsWithFailures`）；WB 的同位物是 `metaFold.summary.categoryCount = {count} 类操作` + 失败的 `cr-tool-call__error{color:var(--cr-color-danger);background:var(--cr-color-danger-bg);border-radius:6px;padding:6px 8px;font-size:12px}`（`wb:renderer/assets/src-BllqCp4U.css:1`）。
- Qoder 明确区分"被用户终止"和"失败"：`turnInterrupted:"Qoder 的回复已被你终止。"` vs `turnFailed:"这轮回复失败。查看错误详情后重试。"`，后者带 `retryTurn:"重试"`。

**计划会不会折叠？折叠规则是什么？**

**WB 有用户级开关，规则写在设置里：**

```
settings.conversation.toolDisplay.title       = 对话过程展示方式
settings.conversation.toolDisplay.compact     = 简洁
settings.conversation.toolDisplay.compactDescription  = 只展示正在执行的工具，其余调用收起，可手动展开
settings.conversation.toolDisplay.detailed    = 详细
settings.conversation.toolDisplay.detailedDescription = 展示某一阶段的全部工具调用记录，可直接查看
```
— `wb:renderer/assets/zh-cn-J68R5HFz.js:470`

"简洁"模式的规则是**"只展开正在跑的那一个，其余摺起"**——不是"只显示前 N 条"。剩下 N 条的入口是一个计数：

```
metaFold.showEarlier = 显示更早的 {count} 条
metaFold.toolCalls   = 个工具调用
metaFold.messages    = 条过程消息
```
— `wb:renderer/assets/i18n-Bddor7Di.js:2`

Qoder 的折叠是**按"轮"（turn）折**，一轮一折，标题就是那句摘要：

```
chatActivity.running          = 正在执行
chatActivity.runningExpanded  = 正在执行中
chatActivity.runningToolAction= 正在{{action}}
chatActivity.runningToolCall  = 正在调用 {{tool}}
chatActivity.completed        = 执行完成
chatActivity.processed        = 已处理
chatActivity.completedTools_one   = 执行工具 {{count}} 次
chatActivity.completedToolsWithFailures_one = 执行工具 {{count}} 次，其中 {{failedCount}} 次失败
```
— `qd:out/renderer/assets/index-DDa3WwIe.js:1 §chatActivity`

**我们现在的规则是"超过 6 条折尾"（`us:components/bot/turn.tsx:580 PLAN_LIMIT = 6`）。** 这是在折叠**内容**；两家折的是**层级**。

#### 原文证据（汇总）

| # | 事实 | 位置 |
|---|---|---|
| 1 | 计划正文自动开在右侧产物面板，同一份只开一次 | `wb:renderer/assets/common-p2lSs_-n.js:1 §usePendingPlanAutoOpen` |
| 2 | 卡内计划预览 `max-height:220px` 自滚，路径条 12/18 | `wb:renderer/assets/exit-plan-mode-floating-D4Toe2nG.css:1` |
| 3 | todo 四态 = 等待/进行中/已完成/错误 | `wb:renderer/assets/zh-cn-J68R5HFz.js:474` |
| 4 | 工具 10 态 + 状态色映射 | `wb:renderer/assets/main-content-core-D6Z9wzXe.css:1 §.status-text.status-*` |
| 5 | "进行中"= 标题 2.2s 文字扫光，结束变静止灰 | `wb:renderer/assets/src-BllqCp4U.css:1 §.cr-collapse--streaming` |
| 6 | 取消 = 灰色同字号，无图标 | 同上 `§.cr-cancelled-indicator` |
| 7 | 折叠规则可由用户选"简洁/详细" | `wb:renderer/assets/zh-cn-J68R5HFz.js:470` |
| 8 | 折叠后的入口是一句 `显示更早的 {count} 条` | `wb:renderer/assets/i18n-Bddor7Di.js:2` |
| 9 | Qoder 一轮一折，标题即摘要（含失败计数） | `qd:…:1 §chatActivity` |
| 10 | Qoder 计划本体在 Spec 面板，卡上只有"查看详情" | `qd:…:1 §exitPlan.viewPlan` |

#### 我们怎么做

- 步骤：`us:components/bot/turn.tsx:582-631` `PlanCard`，**放在正文之上、不折叠**，三态字符 `✓ / ▸ / ○`，超过 6 条折尾显示 `… 还有 N 项`。
- 工具步骤：`us:components/bot/turns.ts` 的 `filterSteps` + `turnEvidence`，`turn.tsx:426-495` 的一行"依据条"（`看了 3 · 改了 2 个文件 · 跑了 5 条命令` + 右对齐耗时）。
- 进行中强调：`us:components/bot/turn.tsx:444` 一个 `正在做` 文字 + `turn.tsx:63` 的 `animate-pulse` 圆点。
- 折叠：`openSteps ?? running`（`turn.tsx:348`）——跑着展开、跑完收起。**没有用户开关。**
- 失败：`turn.tsx:449` `· {evidence.failed} 次失败`；整轮失败/取消给"用同样的话再试一次"（`turn.tsx:560-568`）。

#### 差距

1. **计划占用了对话流的主视觉。** 我们把它做成了"正文上方、不折叠、左侧 2px 竖线"的旁注（`turn.tsx:595`）。这解决"看不见"的问题，但换来了"每一轮都多一块"。WB/Qoder 的做法是：卡里只放一个**入口**，正文永远归回复。
   → 折中方案：计划卡保留但**默认折叠成一行**（`计划 2/5 · 停在第 2 步等你批准`），点开才展开；同时把完整计划推给右侧面板（我们的 `panel.tsx` 已经有 tab 结构，加一个 `plan` tab 即可）。

2. **"进行中"的强调方式太弱且和"等待"撞车。** 我们用 `▸` + `animate-pulse` 圆点；`animate-pulse` 在"等审批"时也在动（`turn.tsx:63` 的 `ThinkingLine` 只在 `approvals.length === 0` 时渲染，但 `StatusChip` 的 `animate-ping` 在 `waiting_approval` 时走的是另一分支——目前是对的）。真正的问题是：**没有一处动画是"专属于进行中"的**。
   → 抄 WB 的做法：只有"进行中"的那一行有扫光；等待态是静止字。

3. **没有 10 态，只有 3 态。** 我们计划步骤只有 `completed / in_progress|running / 其他`（`turn.tsx:610-611`）。缺 `skipped`（用户在这一步说"跳过"）和 `destroyed`（上下文丢失）——这两个在长任务里很常见。

4. **折叠规则不可配置。** WB 把它做成设置项，因为"我要不要看过程"是个体差异。我们是硬编码 `running`。
   → 至少加一个 localStorage 偏好的"过程明细：简洁/详细"。

5. **没有"还有 N 个待处理"的全局入口。** 侧栏有 `conversation.sectionFlag.tooltip.pending = 有任务待确认，点击打开`（`wb:renderer/assets/zh-cn-J68R5HFz.js:8`）——一条会话级标记。我们侧栏没有。

---

### 专题 3："它在干活" vs "它在等你"

这是两家做得最狠、我们做得最差的一块。

#### 人家怎么做

**一、用两套完全不同的句子，不是一个计时器换颜色。**

WorkBuddy 的 `progress.phase.*` 是一个**按"阶段 × 工具"生成动词短语**的字典，不是状态枚举：

```
progress.phase.preparing                    = 准备中
progress.phase.model_requesting             = 等待模型响应
progress.phase.model_streaming              = 生成回复中
progress.phase.model_done                   = 处理结果中
progress.phase.tool_executing               = 调用工具中
progress.phase.tool_executing.Read          = 正在读取文件
progress.phase.tool_executing.Bash          = 正在执行命令
progress.phase.tool_executing.Grep          = 正在搜索内容
progress.phase.model_streaming               = 正在准备读取文件   （同一工具有"准备"和"执行"两句）
progress.phase.waiting_for_permission        = 等待授权
progress.phase.waiting_for_permission.Bash   = 等待执行命令授权
progress.phase.waiting_for_permission.Write  = 等待写入文件授权
progress.phase.waiting_for_user              = 等待回复
progress.phase.waiting_for_user.AskUserQuestion = 等待用户回复
progress.phase.retrying                      = 重试中
progress.compacting                          = 压缩上下文中
```
— `wb:renderer/assets/zh-cn-J68R5HFz.js:472`（`progress.phase.*` 共 195 条）

三件事同时成立：

1. **"在等你"不是"在干活"的子状态**——`waiting_for_permission` 是 `progress.phase` 的**兄弟**，不是 `tool_executing` 的一个变体。数据结构上就是两条不同的路径。
2. 每个工具都有专属的等待文案（`等待执行命令授权` 而不是 `等待授权`），用户一眼知道要点哪个。
3. `model_streaming.*` 和 `tool_executing.*` 有同一工具的两套词（`正在准备读取文件` vs `正在读取文件`），因为"准备"阶段没有输出、用户最容易以为卡死。

**二、等待时计时器停止，并且换成另一句话。**

```js
// TurnFoldAvatar：状态行的文案由 3 个互斥分支决定
if (D) {                                    // D = 这一轮已结束（!active || completed）
  return S ? (k == null ? Y(`metaFold.turnCancelled`)
                        : Y(`metaFold.turnCancelledWithDuration`, {duration:k}))
           : (k == null ? null : Y(`metaFold.turnCompletedWithDuration`, {duration:k}));
}
if (k != null) {                            // 还在跑
  let t = Y(`streamingDuration.label`, {duration:k});   // 「已处理 3 分 12 秒」
  return i ? <>{t}<span className="fold-status-separator"> · </span>{i}</> : t;
}
```
— `wb:renderer/assets/src-CMAOF5gI.js:1 §TurnFoldAvatar`

```js
const D = !x || u === true;              // done
const O = useStreamingDurationMs(!D, n); // ← 计时器只在 !done 时走
```

三个关键点：

1. `useStreamingDurationMs(!D, createdAt)` 的第一个参数是"是否启用"。**done 时停止计时**，而不是继续涨。
2. 跑完的文案是 `已完成 {duration}` / `已取消 {duration}`——**动词换了**。跑着的时候叫 `已处理 {duration}`。
3. 等待时有**第四种文案**：`metaFold.summary.waiting = 等待确认：{object}`、`metaFold.summary.waitingPlan = 等待确认执行计划`、`metaFold.summary.waitingAnswer = 等待你的回答`（`wb:renderer/assets/i18n-Bddor7Di.js:2`）。注意它**完全不含 duration**。

也就是说 WB 的状态行有 4 个互斥的模板：

| 情形 | 模板 | 含时长？ |
|---|---|---|
| 跑着 | `已处理 {duration}` | 是（在走） |
| 等审批 | `等待确认：{object}` | **否** |
| 等回答 | `等待你的回答` | **否** |
| 完成 / 取消 | `已完成 {duration}` / `已取消 {duration}` | 是（冻结） |

**这正是我们第 3 条 bug（"等用户批准"却显示"在翻代码… 44 分 39 秒"）的根因：我们只有一个模板，时长是它的必要成分。**

**三、Qoder：状态栏是一个带时间的独立条，且时间用等宽右对齐。**

```js
const TurnStatusBar = ({children, status, icon, time, className}) => (
  <div className={clsx("w-full h-10","bg-primary-bg","rounded-t-xl","relative","px-3.5", className)}>
    <style>{`@keyframes status-dots{0%,100%{opacity:.4}50%{opacity:1}}
             .status-dots-anim{animation:status-dots 1.2s ease-in-out infinite}`}</style>
    <div className="flex items-center gap-2 absolute left-3.5" style={{top:"3px"}}>
      {icon && <span className="size-4 … text-primary-active">{icon}</span>}
      {status != null && <StatusWithAnimatedDots content={String(status)}/>}
    </div>
    {time && <span className="text-[13px] font-medium leading-[22px] text-primary-active
                            font-mono text-right absolute right-3.5" style={{top:"3px"}}>{time}</span>}
  </div>
);
```
— `qd:out/renderer/assets/index-DDa3WwIe.js:1 §TurnStatusBar$1`

- 高度固定 `h-10`（40px），`rounded-t-xl`（只有上圆角）→ 它和下面的输入框是**同一个视觉块的上半部分**。
- 装配时用负边距把它藏到输入框底下：`<div className="w-full mb-[-12px] relative z-0"><TurnStatusBar …/></div>`（`qd:…:1 §ChatInput` 返回体开头）。
- 状态字 `text-[13px] font-medium leading-[22px] text-primary-active` + **三个点的透明度呼吸动画 1.2s**（不是转圈、不是扫光、不是脉冲圆点）。
- 时间 `font-mono` 右对齐——**等宽字体是让"位数变化时不抖"的唯一手段**（我们 `turn.tsx:452` 也做了，是对的）。

Qoder 的"等"与"干"靠**同一栏、不同文本**区分：

```
chatActivity.agentThinking   = 正在思考
chatActivity.agentThought    = 已思考          ← 过去式
chatActivity.thinking        = 思考中
chatActivity.thought         = 已思考
chatActivity.agentWorkDuration = 耗时 {{seconds}}秒
chatActivity.running         = 正在执行
chatActivity.processed       = 已处理
```
— `qd:…:1 §chatActivity`

注意 `正在思考` / `已思考` 是**一对**，中间只差一个"在"和"了"——中文里这就是完成体的语法标记。比"思考中 / 思考完成"更短，比图标更不易误读。

**而且 Qoder 在"干活"时随机换词**（`qd:…:1 §chatActivity.processing`，共 50+ 条）：

```
快好了... 准备一下... 等我一下... 整理一下... 处理一下... 同步一下...
往前推一下... 汇总一下... 检查一下... 想一想... 算一下... 串一下线索...
看一下方案... 再想一下... 准备回复... 处理数据... 多想一下... 斟酌一下...
差不多了... 再等一下... 嗯，我看看... 稍等一下... 继续推理... 马上好...
```

这是**用"人味的废话"替代"精确但焦虑的进度数字"**。它不承诺时间，也就不存在"说到没做到"。

**四、Qoder 的等待卡会自己数秒退出。**

```
chatActivity.question.skipAll              = 无偏好
chatActivity.question.autoSkipCountdown    = {{count}} 秒后自动跳过
chatActivity.question.collapsedTitle_one   = 有 {{count}} 个问题待你回应
chatActivity.question.recommend            = 推荐
chatActivity.question.continue             = 下一题  previousQuestion = 上一题
chatActivity.question.customInputPlaceholder = 输入其他答案
chatActivity.question.sending              = 正在发送…
```
— `qd:…:1 §chatActivity.question`

`useUserQuestionAutoResolutionCountdown(n1.autoResolution)`（`qd:…:1 §UserQuestionInteraction`）——**等待有一个可见的倒计时终点**。这是"它在等你"的第三种表达：不是静止，而是"再不动我就替你决定"。

**五、输入框 placeholder 在两种状态下分别写什么（原文摘录）**

| 产品 / 状态 | placeholder 原文 | 位置 |
|---|---|---|
| WB 默认 | `提问或输入"/"快捷命令` | `wb:renderer/assets/i18n-Bddor7Di.js:2 §input.placeholder` |
| WB 任务页 | `今天幫你做些什麼？@ 新增上下文，/呼叫技能與指令`（**仅找到繁体**，简体键在本 build 缺失） | `wb:renderer/assets/conversation-message-output-hooks-aYfim7OW.js:20 §input.taskPlaceholder` |
| WB 快速问答模式 | `快速問答模式不消耗積分，可解答簡單問題但不支援複雜任務` | 同上 `§input.placeholder.quickQa` |
| Qoder 默认 | `描述你想完成的工作…` | `qd:…:1 §ChatInput`（`placeholder:i1="描述你想完成的工作…"`） |
| Qoder 空会话 Agent | `描述你想完成的工作，默认 Agent 会在当前工作目录中持续推进。` | `qd:…:1 §agentEmptyTitle` 邻域 |
| Qoder 语音 | `可以直接开口说了` | 同上 `§voiceEmptyTitle` |

**关键：两家的 placeholder 都不随"跑着/等着"变化。** 它们把状态放在输入框**上方那一栏**（Qoder 的 TurnStatusBar / WB 的输入区待处理槽），placeholder 只负责"这里能输入什么"。

而我们的 `us:components/bot/composer.tsx:360-373` 是**六路分支改 placeholder**：

```
"先选一个项目"
"正在读取项目…"
"还没有工作目录：点下面的「未绑定目录」选一个，它才知道在哪儿干活"
"它在等你点一下——批不批上面那张卡，由你定"
"它正在做，可以插一句话调整方向…（Enter 发送）"
`${placeholderText}（Enter 发送，Shift+Enter 换行）`
```

其中"等审批"那句是对的（明确指了"上面那张卡"），但它把**三种不同性质的信息**压进了同一个槽位：选项目（阻塞原因）、读项目（加载态）、没目录（缺配置）、等审批（等用户）、跑着（可插话）。用户一打字它全部消失，且每切一次状态整段跳变。

#### 原文证据（汇总）

| # | 事实 | 位置 |
|---|---|---|
| 1 | `progress.phase` 把 `waiting_for_permission` / `waiting_for_user` 与 `tool_executing` 并列为兄弟阶段 | `wb:renderer/assets/zh-cn-J68R5HFz.js:472` |
| 2 | 每个工具有专属等待文案（`等待执行命令授权`） | 同上 |
| 3 | 计时器 hook 以 `!done` 为启用参数，done 即冻结 | `wb:renderer/assets/src-CMAOF5gI.js:1 §TurnFoldAvatar`（`useStreamingDurationMs(!D, n)`） |
| 4 | 4 个互斥模板：`已处理{}` / `等待确认：{}` / `等待你的回答` / `已完成{}{已取消{}}` | `wb:renderer/assets/i18n-Bddor7Di.js:2` |
| 5 | 等待文案不含 duration | 同上 |
| 6 | Qoder 状态条 `h-10 rounded-t-xl`，负边距 -12px 藏到输入框下 | `qd:…:1 §TurnStatusBar$1` + `§ChatInput` |
| 7 | 状态点动画是 1.2s 透明度呼吸（非旋转/扫光） | 同上（`@keyframes status-dots`） |
| 8 | `正在思考` / `已思考` 成对，仅差语法标记 | `qd:…:1 §chatActivity` |
| 9 | 50+ 条"快好了"式轮换文案 | `qd:…:1 §chatActivity.processing` |
| 10 | 提问卡有自动跳过倒计时 | `qd:…:1 §chatActivity.question.autoSkipCountdown` |
| 11 | 两家 placeholder 不随运行状态变化 | 见上表 |
| 12 | WB"进行中"的动画只有一处：`cr-collapse--streaming` 的 2.2s 扫光 | `wb:renderer/assets/src-BllqCp4U.css:1` |

#### 我们怎么做

- 状态徽标：`us:components/bot/turn.tsx:74-122` `StatusChip`，六分支（审批已过期 / 等你批准 / 已暂停 / 进行中 / 失败 / 已停止）。**注意 `waiting_approval` 分支在 `running` 之前判断**，注释里写明了原因（`turn.tsx:75-79`）——这一点我们已经做对了。
- 计时：`us:components/bot/turn.tsx:49-72` `ThinkingLine`，`setInterval(1000)` 无条件走，只靠 `props.approvals.length === 0` 在**渲染层**挡掉（`turn.tsx:512`）。
- 计划等待：`turn.tsx:601-603` `停在第 N 步等你批准`。
- placeholder：`composer.tsx:360-373` 六路分支切换。

#### 差距

1. **计时器没有"停"这个概念，只有"藏"这个概念。** `ThinkingLine` 的 `setInterval` 不受任何状态控制，`approvals.length` 一变它就整块被卸载。这有两个后果：① 一旦审批卡因为数据缺失没渲染出来（bug #4），`approvals.length` 是 0，`ThinkingLine` 就重新出现并继续计时——**44 分 39 秒就是这么来的**；② 没有办法显示"已处理 3 分 12 秒"这种**冻结的**时长。
   → 改法：`ThinkingLine` 收一个 `running: boolean` 参数，`false` 时 `clearInterval` 并保留最后值，文案从 `{verb}…` 切成 `已处理 {duration}`。

2. **没有"已完成"这一档。** 一轮正常结束后，页面上**没有任何时长**（只剩依据条右侧的 `evidence.durationMs`，而那是工具耗时之和，不是回合时长）。用户没法比较两轮快慢。
   → 抄 `metaFold.turnCompletedWithDuration = 已完成 {duration}`，与 `已取消 {duration}` 成对。

3. **等待文案不够具体。** 我们只有"等你批准"四个字 + 计划卡里的"停在第 N 步"。WB 是 `等待执行命令授权` / `等待写入文件授权`——**按工具换词**。我们的 `approval.tool` 已经有了，直接拼即可。
   → `等你批准 · 执行命令` 比"等你批准"信息量高一倍。

4. **动画重复且冲突。** 同一个页面上：`StatusChip` 的 `animate-ping`（`turn.tsx:108`）、`ThinkingLine` 的 `animate-pulse`（`turn.tsx:63`）、打字光标的 `animate-pulse`（`turn.tsx:501`）三处在动。
   → 只保留一处：把 `StatusChip` 的 `animate-ping` 去掉，让"进行中"的动效只出现在**那行动词短语**上（扫光）。

5. **placeholder 承担了它不该承担的职责。** 操作提示应该常驻。
   → placeholder 固定为一句（保留 `（Enter 发送，Shift+Enter 换行）` 这种常驻信息），状态迁移到输入框上方的状态条。

---

### 专题 4：信息密度与视觉层级

#### 人家怎么做：真实数值

**一、字号阶梯（px）**

WorkBuddy 有两套并行：

`--wb-font-size-*`（`wb:renderer/assets/src-DIwCVExo.css:1`）：

```
1:8  2:10  3:12  4:13  5:14  6:16  7:18  8:20  9:24  10:28  11:32
```

行高：

```
4:16  4-2:18  5:20  5-2:22  6:24  7:28  8:32  10:40
```

语义别名：

```
--wb-font-body-size:14px      --wb-font-body-line-height:22px
--wb-font-caption-size:12px   --wb-font-caption-line-height:18px
--wb-font-h1-size:24px        --wb-font-h1-line-height:32px
--wb-font-h2-size:20px        --wb-font-h2-line-height:28px
--wb-font-h3-size:16px        --wb-font-h3-line-height:24px
--wb-font-display-size:32px   --wb-font-display-line-height:40px
```

对话流专用（`--wb-font-chatflow-*`，同文件），这是最能抄的一档：

```
h1 21/37 · h2 19/33 · h3 17/30 · h4 15/24
p1 15/24 · p1-highlight 15/24 · p2 13/23 · p3 11/19
code 13/20
```

**注意 chatflow 的 p1 是 15px 而不是 14px，行高 24（1.6）而不是 22**——正文比 UI 正文大一档、松一档，因为要长读。

Qoder 用 Tailwind 的 rem 阶梯（`qd:out/renderer/assets/index-yldrU_jD.css:1`）：

```
--text-xs:.75rem (12px) / lh 1
--text-sm:.875rem (14px) / lh 1.25
--text-base:1rem (16px) / lh 1.5
--text-lg:1.125rem (18px) / lh 1.75
--text-xl:1.25rem (20px) / lh 1.75
--text-2xl:1.5rem (24px) / lh 1.5
```

但**实际用的是方括号内的硬值**：`text-[13px] leading-[22px]`（状态条）、`text-[18px] leading-7`（对话框标题）、`text-[11px] leading-4`（下拉说明）。即：**Qoder 的 UI 正文是 13px/22px，和 WB 的 chatflow p2 一致。**

**二、间距阶梯**

```
WB  --wb-spacing-0:0  1:2px  2:4px  2-5:5px  3:8px  4:12px  5:16px
    6:20px  7:24px  8:32px  9:40px  10:48px  12:64px
WB  --cb-spacing-xs:4  sm:8  md:12  lg:16  xl:20  xxl:24
```

**注意 4 的整数倍不严格**：有 2px、5px。2px 用于分隔线/微调，5px 用于图标与文字。**不是"所有间距都 8 的倍数"**。

**三、圆角**

```
WB  --wb-radius-xs:2  sm:4  md:6  lg:8  xl:12  2xl:16  3xl:20  4xl:24  full:9999
WB  --cb-border-radius-sm:4  md:6  lg:8  xl:16  2xl:24
Qoder  radius-xs:.125rem(2) sm:.25(4) md:.375(6) lg:.5(8) xl:.75(12) 2xl:1rem(16) + full
       另加 squircle-3/4/5/6 + squircle-sm/md/lg/xl/2xl/full
```

Qoder 全部圆角都套了 `corner-shape:squircle`：

```css
.squircle-xl{corner-shape:squircle;border-radius:.75rem}
.squircle-2xl{corner-shape:squircle;border-radius:1rem}
```
— `qd:out/renderer/assets/index-yldrU_jD.css:1 §squircle-*`

这是 CSS `corner-shape`（Chrome 139+）：把正圆弧换成"连续曲率"（Apple 那种方不方圆不圆的角）。**同一个 12px 数，观感比 `border-radius:12px` 更"软"。** 我们的 `--radius-lg:12px` 是正圆角。

具体组件的圆角用法：

| 元素 | WB | Qoder |
|---|---|---|
| 审批浮层卡 | 24px | `rounded-xl` 12px |
| 选项行 | 12px | — |
| 输入框容器 | — | 内 `squircle-xl` 12px，外 `rounded-[18px]` |
| 后台任务胶囊 | 100px（`border-radius:100px;height:36px`） | — |
| 状态条 | — | `rounded-t-xl` 12px 只上圆角 |
| 按钮 | — | `rounded="pill"` |

**四、色板（亮/暗两套）**

WorkBuddy `--cb-*`（`wb:renderer/assets/_theme-light-DVW6cH6b.css:1`）：

| 语义 | 浅色 | 深色 |
|---|---|---|
| `--cb-text-primary` | `#333` | `#d2d3e0` |
| `--cb-text-secondary` | `#666` | `#858699` |
| `--cb-text-tertiary` | `gray`(#808080) | `#a0a0a0` |
| `--cb-text-disabled` | `#0000004d` | `#ffffff40` |
| `--cb-bg-primary` | `#0000000d` | `#101114` |
| `--cb-bg-secondary` | `#f5f5f5` | `#17191d` |
| `--cb-border-secondary` | `#ebebeb` | `#ffffff1a` |
| `--cb-question-floating-card-bg` | `#fff` | `#181b20` |
| `--cb-question-floating-card-border` | `#00000014` | `#ffffff14` |
| 悬浮层底色 `--cb-bg-card` | — | `#181b20` |

浮层体系单独一套，且**浅色的边框是 `#00000014`（8% 黑），深色是 `#ffffff14`（8% 白）**——同一个 8% 不透明度，只换基色。这是我们该抄的：不是分别调两个灰，而是"边 = 前景色 8%"。

`--cr-*`（对话渲染层，`wb:renderer/assets/src-DIwCVExo.css:1`）：

| 语义 | 浅色 | 深色 |
|---|---|---|
| `--cr-internal-text-primary` | `#000000e6` | `#ffffffeb` |
| `--cr-internal-text-secondary` | `#000000b3` | `#ffffffa6` |
| `--cr-internal-text-tertiary` | `#00000080` | `#ffffff73` |
| `--cr-internal-bg-primary-default` | `#f2f2f2` | `#2a2c31` |
| `--cr-internal-border-default` | `#ebebeb` | `#2a2c31` |
| `--cr-internal-status-success` | `#0cbf5b` | `#91e8b8` |
| `--cr-internal-status-error` | `#f64041` | `#f78d8e` |
| `--cr-internal-status-warning` | `#ff7800` | `#ffae6e` |
| `--cr-internal-status-info` | `#00c29a` | `#5fe3c5` |
| `--cr-internal-accent` | `#3370ff` | `#4da3e0` |

**注意三级文字的透明度阶梯：`e6`(90%) / `b3`(70%) / `80`(50%) / `73`(45%)。** 不是 `#333/#666/#999` 这种实色，而是**同色不同透明度**——这样在任何底色上都能自动保持相对对比度。我们用的是实色（`--text:#1d1d1f / --muted:#5a5a5d / --faint:#8b8b8f`），换底色时必须重新调。

语义色板 `--wb-palette-*` 是 10 级 + 深浅各一套，例如 brand：

```
浅色 1:#dff7f2 2:#bff0e6 3:#9fe8d9 4:#80e1cd 5:#60d9c0 7:#40d1b3 8:#4cf0ce 9:#7cf0d8 10:#a8f5e5
深色 1:#0e2520 2:#163932 3:#185f51 4:#168770 5:#1ca98c 7:#22cba8 8:#00c29a 9:#009273 10:#00614d
```

（浅色 1→10 越来越淡，深色 1→10 越来越亮；同一个数字 = 同一个对比度档位，不是同一个颜色。）

Qoder 的语义 token 命名体系（`qd:…/index-DDa3WwIe.js:1 §HQ` 的 zod 枚举）：

```
--color-background-{primary,secondary,tertiary,inverse,ghost,info,danger,success,warning,disabled}
--color-text-{primary,inverse,ghost,info,danger,success,warning,disabled}
--color-border-{primary,inverse,ghost,info,danger,success,warning,disabled}
```

**数值未找到**：这些变量的值在运行时注入，且部分名字被混淆成 `--q8a88b1` / `--qca4712` / `--q1fb96a`。CSS 里只有 `--color-green-500:oklch(72.3% .219 149.579)` 一个具名色（`qd:…/index-yldrU_jD.css:1`），说明底层是 **oklch** 色彩空间。`[未查清]`

**五、边框粗细**

```
WB  --cr-input-border-width: 1px（浅）/ .5px（深）
WB  --wb 边框统一 1px（`border:1px solid var(--wb-border-default)`）
QD  border（Tailwind 默认 1px）+ 半透明：`border-border-tertiary/40`
```

**深色下用 0.5px 是 WB 的一个细节**：深色高对比下 1px 会显得比浅色粗（视觉膨胀），减半补偿。

**六、主次怎么区分（不靠边框靠什么）**

这是本专题最值得抄的部分。WB 的四层手段，按使用频率排：

1. **透明度**（最常用）。三级文字 `e6/b3/80/73`。
2. **字号 + 行高同降**（14/22 → 12/18）。
3. **字重**（400 / 500 / 600，**没有 700**）。
4. **只有一个元素带背景**。看 `.cr-collapse__header`：`color:var(--cr-text-tertiary)`，**无边框、无底色、无 padding**，hover 才升到 `--cr-text-primary`：

```css
.cr-collapse__header{width:fit-content;max-width:100%;color:var(--cr-text-tertiary);
  cursor:pointer;user-select:none;align-items:center;gap:8px;line-height:1.75;display:flex;
  transition:color .18s}
.cr-collapse__header:hover,
.cr-collapse__header:hover .cr-tool-head__icon,
.cr-collapse__header:hover .cr-tool-head__status,
.cr-collapse__header:hover .cr-tool-head__primary,
.cr-collapse__header:hover .cr-tool-head__secondary{color:var(--cr-text-primary)}
```
— `wb:renderer/assets/src-BllqCp4U.css:1 §.cr-collapse__header`

**这就是"主次的唯一手段是颜色，不是框"。** hover 时整行变主色，而不是加一个 hover 背景。

**七、一屏里允许出现几个"框"？怎么避免框套框？**

WorkBuddy 的答案：**同屏最多 2 个带边框的东西——输入框（1）+ 至多 1 张待处理卡。其余全部无框。**

手段清单（都能在我们的代码里落地）：

1. **卡片由 JSX 条件决定是否输出，不靠 CSS 藏** —— 没卡时节点根本不存在，零占位（`wb:…/common-p2lSs_-n.js:1 §ConversationInputArea` 里的 `!A.banner && F ? <PendingSessionFallbackCard/> : null`）。
2. **过程全部折进"一行摘要"**（metaFold）。工具调用不是一个框，是一行 `正在读取文件`。
3. **计划卡不画框，只画一条左边线**（我们自己已经悟到了，`turn.tsx:595` 的注释写得很清楚）。
4. **"依据条"是一行文字按钮，不是卡片**（`turn.tsx:428` 的 `hover:bg-surface-2`）——我们这里也对了。
5. **工具调用的输入/输出用 `<pre>` + 1px 边框，但只在展开时存在**（`.tool-input,.tool-output{margin-top:8px}`，`wb:…/main-content-core-D6Z9wzXe.css:1`）。
6. **安全审查信息用 `<details>/<summary>`**，收起时只有一行 14px 的三级灰文字（`.cr-tool-call__security-summary`）。

Qoder 的答案：**绝大多数东西根本没有框。** `chatActivity` 的整棵树里，只有 `PermissionCard` 和 `UserQuestion` 两项有容器（前者还是 `glass` 半透明）。工具行、思考行、命令输出全是裸文本 + 缩进。

```js
// 提问卡：不是一个"卡片"，是一段带区域的文本
<div className="shrink-0 bg-bg-layout px-5 pb-1.5 pt-1" role="region">
```
— `qd:…:1 §PendingInteractionOverlay`

它的"层级"靠**缩进 + 前导图标**，不是靠嵌套容器。对比我们：`ApprovalCard` 里面 `ApprovalOptions` 里面 `ol>li>button`，最里层还有个 `div` 包理由输入——四层容器，三层有边框（`rounded-xl border` → `rounded-md` → `border border-line`）。

#### 原文证据（汇总）

| # | 事实 | 位置 |
|---|---|---|
| 1 | 字号阶梯 8/10/12/13/14/16/18/20/24/28/32 | `wb:renderer/assets/src-DIwCVExo.css:1 §--wb-font-size-*` |
| 2 | chatflow 正文 15/24，UI 正文 14/22，辅助 12/18，微 11/19 | 同上 `§--wb-font-chatflow-*` |
| 3 | 间距阶梯含 2px、5px，非严格 8 倍数 | 同上 `§--wb-spacing-*` |
| 4 | 圆角 2/4/6/8/12/16/20/24 + full | 同上 `§--wb-radius-*` |
| 5 | Qoder 全部圆角套 `corner-shape:squircle` | `qd:out/renderer/assets/index-yldrU_jD.css:1 §squircle-*` |
| 6 | 三级文字 = 同色 90%/70%/50%/45% 透明度 | `wb:renderer/assets/src-DIwCVExo.css:1 §--cr-internal-text-*` |
| 7 | 浅/深同语义色成对给出（如 success `#0cbf5b` / `#91e8b8`） | 同上 `§--cr-internal-status-*` |
| 8 | 深色输入框边框 .5px，浅色 1px | `wb:renderer/assets/src-BllqCp4U.css:1 §--cr-input-border-width` |
| 9 | 主次只靠 color，hover 才升主色，不加背景 | 同上 `§.cr-collapse__header` |
| 10 | 审批浮层卡 24px 圆角 + 8% 前景色边框 + 极轻双层阴影 | `wb:renderer/assets/high-credit-approval-floating-panel-C7R_cGE7.css:1` |
| 11 | 选项行 40px 高、12px 圆角、hover 只换底色 | 同上 |
| 12 | 后台任务胶囊 36px 高 / 100px 圆角，展开面板 16px 圆角、列表 `max-height:96px` | `wb:renderer/assets/background-tasks-BPEN5SmF.css:1 §_capsule_ / §_panel_ / §_panelList_` |
| 13 | 状态条 40px 高，`rounded-t-xl`，13/22 字 | `qd:…/index-DDa3WwIe.js:1 §TurnStatusBar$1` |
| 14 | 输入框 `p-2`、`min-h-[44px] textarea`、`leading-[24px]`、`maxRows 6` | `qd:…:1 §ChatInput` |
| 15 | 对话列表 `padding-inline:16px`、滚动条 6px | `wb:renderer/assets/src-BllqCp4U.css:1 §.cr-message-list` |

#### 我们怎么做

`us:app/globals.css`：

- 字号：**没有阶梯**，全部是组件内硬编码 `text-[13px] / text-[11px] / text-[12px] / text-[14px]` 等。正文 13px（注释里写了"正文 13px、辅助 11–12px、标题 13–15px"）。
- 圆角：`--radius-sm:6px / md:8px / lg:12px`（三档）。
- 色板：实色（`--text:#1d1d1f / --muted:#5a5a5d / --faint:#8b8b8f`），深色 `#ededf1 / #a3a5ad / #6c6e77`。
- 边框：1px 统一，无半像素。
- 阴影：三档（`--shadow-sm/md/lg`）。
- z-index：`--z-dropdown:20 / overlay:30 / modal:50`。

#### 差距

1. **没有字号 token，13/11/12 散落在 60+ 个组件里。** 想改"正文大一档"要全局搜索。WB 一套 `--wb-font-*` 就够。
   → 加 `--fs-body:14px` 等 5 个 token，先只在新代码用，`us:app/globals.css` 里加 `@theme` 映射即可（Tailwind v4 的 `@theme inline` 已经有了，加几行）。

2. **三级文字是实色，不是透明度。** 换 skin（我们有两个皮肤）时必须手调两遍，且未来加第三个皮肤会继续乘。
   → 改成 `color-mix(in srgb, var(--text) 70%, transparent)` 这类派生值。**这一条改动量 S，收益最大。**

3. **正文 13px 偏小。** 两家 UI 正文 13–14px，但**长读正文**都是 15px（WB chatflow p1）。我们回复正文也是 13px（`turn.tsx` 里 `MarkdownView` 无字号设定，继承全局）。中文 13px 在 1.5 倍行高下偏挤。
   → 回复正文提到 14px / 行高 1.75。这是"读 300 字就累"和"不累"的差别。

4. **框太多。** 一轮对话里可能同时出现：审批卡（border）、计划卡（border-l）、依据条（无）、QuestionCard（border）、失败原因（border）、技能提示（border）。**六个候选框。**
   → 按 WB 的规则收敛到：**有边框的只允许"要你动手的"一个**（审批卡/提问卡二选一），其余全走"左侧线 + 缩进 + 三级灰"。

5. **没有 `corner-shape:squircle`。** 这是纯 CSS 一行、零风险的观感升级（`border-radius` 数值不变，形状变软）。

6. **没有半像素边框。** 深色皮肤下 1px 边框偏重。`@media (prefers-color-scheme: dark)` 下改 `.5px` 即可——但注意 Qoder 用了 `border-border-tertiary/40`，即**降不透明度**比改粗细更稳（不会因缩放出现断线）。

---

## 4. 可迁移改动清单

按"投入产出比"排序。工作量：S ≈ 半天内、M ≈ 1–2 天、L ≈ 3 天以上。

| # | 改什么 | 我们改哪个文件 | 依据是哪个证据 | 工作量 |
|---|---|---|---|---|
| 1 | **审批卡/提问卡迁到输入区容器里、输入框上方**，槽位由 `pendingApprovals.length` 决定，不用事件驱动 | 新建 `us:components/bot/pending-slot.tsx`；`bot-app.tsx` 挂在 composer 上方；`turn.tsx` 删掉 `props.approvals.map` | `wb:…/common-p2lSs_-n.js:1 §ConversationInputArea`；`wb:…/common-EkSkH3Q0.css:1 §.conversation-input-area`；`qd:…:1 §PendingInteractionOverlay` | M |
| 2 | **三层兜底卡**：全卡 → `{tool} 等你确认`+拒绝 → `等待授权`+`停止当前任务` | 新建 `us:components/bot/pending-slot.tsx`（同 #1） | `wb:…/common-p2lSs_-n.js:1 §ConversationPendingPanel` / `§PendingFallbackPanel` / `§PendingSessionFallbackCard`；文案 `wb:…/conversation-message-output-hooks-aYfim7OW.js:20` | S |
| 3 | **`ThinkingLine` 加 `running` 参数**：false 时停表、文案切 `已处理 {duration}` | `us:components/bot/turn.tsx:49-72` + `turn.tsx:512` | `wb:…/src-CMAOF5gI.js:1 §TurnFoldAvatar`（`useStreamingDurationMs(!D, n)`） | S |
| 4 | **加"已完成 / 已取消"带时长**（现在整轮结束无时长） | `us:components/bot/turn.tsx`（`StatusChip` 旁） | `wb:…/i18n-Bddor7Di.js:2 §metaFold.turnCompletedWithDuration / turnCancelledWithDuration` | S |
| 5 | **等待文案按工具换词**：`等你批准 · 执行命令` / `· 写入文件` | `us:components/bot/turn.tsx:89-96` | `wb:…/zh-cn-J68R5HFz.js:472 §progress.phase.waiting_for_permission.<Tool>` | S |
| 6 | **审批卡去掉整卡警告底**，风险降为一行小字/徽标 | `us:components/bot/turn.tsx:139-143` | `wb:…/high-credit-approval-floating-panel-C7R_cGE7.css:1`（卡底中性，只有 `__risk-tag` 是 `#ff7800`） | S |
| 7 | **审查员做成"橙色徽标 + tooltip"**，并允许它改默认选中项 | `us:components/bot/turn.tsx:159-163`；`approval.tsx` 接受 `defaultChoice` | `wb:…/common-p2lSs_-n.js:1 §SandboxApprovalCard`（`decorateCard` + `defaultChoice`） | M |
| 8 | **"允许并记住"改成运行时算出的建议按钮**（每条按钮文案自带范围） | `us:components/tasks/api.ts` `approvalGrantOptions` → 输出 `label` 而非 `scope+warning` | `qd:…:1 §suggestion`（`本次任务始终允许访问"{{scope}}"`） | M |
| 9 | **计划卡默认折叠成一行**，完整计划推右侧面板新 tab | `us:components/bot/turn.tsx:582-631`；`us:components/bot/panel.tsx` | `wb:…/common-p2lSs_-n.js:1 §usePendingPlanAutoOpen`（去重 + 只开一次） | M |
| 10 | **把"进行中"的唯一动画收成一处**：删 `StatusChip` 的 `animate-ping`，给动词短语加扫光 | `us:components/bot/turn.tsx:104-114`、`:49-72` | `wb:…/src-BllqCp4U.css:1 §.cr-collapse--streaming`（2.2s 扫光）；`qd:…:1 §status-dots`（1.2s 呼吸） | S |
| 11 | **三级文字改透明度派生**，不再写死三个灰 | `us:app/globals.css:29-31` 与 dark 段 `:84-86` | `wb:…/src-DIwCVExo.css:1 §--cr-internal-text-*`（`e6/b3/80/73`） | S |
| 12 | **正文 13px → 14px，行高 1.6 → 1.75** | `us:app/globals.css` 加 `--fs-read`；`MarkdownView` 用 | `wb:…/src-DIwCVExo.css:1 §--wb-font-chatflow-p1-size:15px` | S |
| 13 | **加 `corner-shape:squircle`** 到所有圆角工具类 | `us:app/globals.css`（加一个 `.squircle` 工具类，逐步套） | `qd:…/index-yldrU_jD.css:1 §squircle-*` | S |
| 14 | **加字号/间距 token 阶梯** | `us:app/globals.css` 的 `@theme inline` 段 | `wb:…/src-DIwCVExo.css:1 §--wb-font-size-* / §--wb-spacing-*` | S |
| 15 | **审批卡容器改 `role="radiogroup"`**，Tab 落在第一项而不是容器 | `us:components/tasks/approval.tsx:65-71` | `qd:…:1 §PendingPermissionInteraction`（真 `<button>` + `aria-busy`） | S |
| 16 | **中文输入法保护**：`approval.tsx` 的数字键/Esc 加 `isComposing` 判定 | `us:components/tasks/approval.tsx:49-63` | `wb:…/common-p2lSs_-n.js:1 §PendingPlanPanel.onKeyDown`（`t.isComposing`）；`qd:…:1 §ChatInput`（`P2.nativeEvent.isComposing`） | S |
| 17 | **后台任务从"10 个"改胶囊 + 96px 列表**，失败项单独一行计数 | `us:components/tasks/task-console.tsx`（或 `task-list-pane.tsx`） | `wb:…/background-tasks-BPEN5SmF.css:1 §_capsule_ / §_panelList_{max-height:96px}` | M |
| 18 | **侧栏会话行加状态旗标**（进行中/待确认/失败/未读）+ 相对时间 | `us:components/bot/sidebar.tsx:322-340` | `wb:…/zh-cn-J68R5HFz.js:8 §conversation.sectionFlag.tooltip.{working,pending,failed,unread}` | S |
| 19 | **"过程明细：简洁/详细"用户偏好** | `us:components/bot/turn.tsx:347-349` + 设置页 | `wb:…/zh-cn-J68R5HFz.js:470 §settings.conversation.toolDisplay.compact` | M |
| 20 | **深色皮肤边框降不透明度**（`/40`）而非改 0.5px | `us:app/globals.css` dark 段 | `wb:…/src-BllqCp4U.css:1 §--cr-input-border-width`；`qd:…:1 §border-border-tertiary/40` | S |

**如果只做三件事**：#1（浮层）、#3（停表）、#11（透明度文字）。前两条直接消掉 bug #3 和 #4，第三条影响每一屏的观感。

---

## 5. 人家也没做好 / 我们反而更强的地方

### 5.1 我们更强

| # | 我们做了什么 | 证据 | 人家的问题 |
|---|---|---|---|
| 1 | **审批卡有 1–N 数字键 + Esc 直接拒绝** | `us:components/tasks/approval.tsx:49-63` | **两家都没有。** WB 只处理 `Enter`/`Space`（`wb:…/high-credit-approval-floating-panel-DemeEjgI.js:1`，三处）；Qoder 全是 `<Button>` 靠 Tab。全库未找到审批卡里的 `Escape` 处理 |
| 2 | **驳回理由"点拒绝才展开"**，且展开后理由挂在列表外、缩进到选项文字列 | `us:components/tasks/approval.tsx:39-43`、`:119-160`（注释写明了"插在 1/2 之间序号就对不上"） | WB 计划卡把"调整意见"做成**常驻输入行**（`§PendingPlanPanel` 第 2 项），普通审批卡则**完全不要理由**。中间态没有 |
| 3 | **过期/失败/取消三态分开**，过期卡明确说"再点也提交不上去"并给"重跑这一轮" | `us:components/bot/turn.tsx:80-88`、`:166-178` | WB/Qoder **都没有"过期"这个概念**。WB 只有 `approvalTimeout = 审批超时，已拒绝本次操作`（一句话审计日志），Qoder 只有"发送失败可重试" |
| 4 | **一轮的"依据条"把 4 个数字压成一行，每个可点开溯源** | `us:components/bot/turn.tsx:426-457` | WB 的过程摘要只有 `{count} 类操作`（`metaFold.summary.categoryCount`），Qoder 只有 `执行工具 {count} 次`。**都没有"看了/改了/跑了"三分法** |
| 5 | **耗时右对齐等宽、位数变化不抖** | `us:components/bot/turn.tsx:450-456`（注释写明了原因） | Qoder 也做了（`font-mono text-right`），WB **没做**——`streamingDuration.label` 直接拼在状态句里 |
| 6 | **计划卡不画框，用左侧 2px 竖线挂正文旁** | `us:components/bot/turn.tsx:591-595` | WB 的计划预览是**框套框**（卡 24px 圆角里再套一个 `border-radius:6px` 的 `__plan` 盒，见 `exit-plan-mode-floating-D4Toe2nG.css:1`） |
| 7 | **驳回理由会原样交给模型** | `us:components/tasks/approval.tsx:22-24`（`onDeny(feedback?)` 注释："它会被原样交给模型"） | Qoder 的 `feedback` 也是回传（`respondPermission(...,{approved:false,feedback})`），但 WB 的普通审批**根本不收理由** |

### 5.2 人家也没做好（我们不必抄）

| # | 问题 | 证据 |
|---|---|---|
| 1 | **WorkBuddy 的"允许"和"确认"共用同一行**，靠 `approvalScope === "session"` 切换文案，用户无法从界面判断"我点下去记住多久" | `wb:…/high-credit-approval-floating-panel-DemeEjgI.js:1`（`M = m(l===`session`?`tool.approval.allow`:`tool.approval.confirm`)`） |
| 2 | **WB 的审批卡提交失败是静默的**：`catch` 里只有 `console.error`，界面上没有任何提示，卡片退回 pending 态看上去像"没点动"，用户会重复点 | 同上（`catch(e){console.error('[HighCreditApprovalFloatingPanel] respond failed',e)}`，无 toast/无 inline error） |
| 3 | **Qoder 的"解释"要额外一次模型调用**（`explainToolCall`），慢且可能失败，失败态只是把按钮留在那儿 | `qd:…:1 §PendingPermissionInteraction`（`M1` → `permission.explainFailed`） |
| 4 | **Qoder 的浮层 `max-h:min(55vh,440px)`**，在 13 寸屏上会盖掉半个对话；且 `bottom-full` 无退路，卡高时会把输入框顶出视口 | `qd:…:1 §PendingInteractionOverlay` |
| 5 | **Qoder 的 50+ 条"快好了..."** 是可爱但不稳定的：用户第二次看到同一句话会怀疑"是不是卡住了" | `qd:…:1 §chatActivity.processing` |
| 6 | **WB 的 `pending.fallback` 兜底卡只有"拒绝"一个选项**，用户想"看一眼再决定"没有路（对比我们的过期卡至少有"重跑"） | `wb:…/common-p2lSs_-n.js:1 §PendingFallbackPanel`（`choices:[{value:`reject`}]`） |
| 7 | **两家的 token 体系都在膨胀**：WB 同时有 `--cb-*`、`--wb-*`、`--cr-*` 三套前缀加 `--vscode-*` 透传；Qoder 把部分变量名混淆成 `--q8a88b1`（CSS 不可读，调试时无法反查） | `wb:…/_theme-light-DVW6cH6b.css:1`；`qd:…:1 §HQ`（`literal("--q8a88b1")`） |

---

## 6. 事实与推测的分界

### 6.1 读源码确认的（可直接引用）

1. 审批/提问卡在两家都**不在对话正文里**：WB 是 `ConversationInputArea` 里排在输入框之前的 `ConversationPendingPanelSlot`（容器 `.conversation-input-area` 是纵向 flex，左右 24px，卡下方 12px）；Qoder 是 `bottom-full z-30` overlay / 输入框上方 `role="region"` 的兄弟块。
   `wb:…/common-p2lSs_-n.js:1 §ConversationInputArea`；`wb:…/common-EkSkH3Q0.css:1 §.conversation-input-area`；`qd:…:1 §PendingInteractionOverlay`
2. WB 的状态行有 **4 个互斥模板**（`已处理{}` / `等待确认：{}` / `等待你的回答` / `已完成{}{已取消{}}`），**等待态不含 duration**。
   `wb:…/i18n-Bddor7Di.js:2`；`wb:…/src-CMAOF5gI.js:1 §TurnFoldAvatar`
3. WB 的计时 hook 调用形式是 `useStreamingDurationMs(!D, n)`，其中 `D = !turnActive || completed`，即"已结束"；并且结束态走的是另一个模板（`已完成 {duration}` / `已取消 {duration}`），**不再走 `streamingDuration.label`（已处理 {duration}）**。`wb:…/src-CMAOF5gI.js:1 §TurnFoldAvatar`
4. WB 的 `progress.phase` 把 `waiting_for_permission` / `waiting_for_user` 与 `tool_executing` 并列为兄弟阶段，每个工具有专属等待文案（195 条）。
   `wb:…/zh-cn-J68R5HFz.js:472`
5. WB 有**三层降级**（全卡 → `{tool} 等待确认`+拒绝 → `等待授权`+`停止当前任务`），最后一层不管有没有数据都能渲染。
   `wb:…/common-p2lSs_-n.js:1 §ConversationPendingPanel / §PendingFallbackPanel / §PendingSessionFallbackCard`
6. WB 的审查员是**橙色徽标 + tooltip**，并且**会改写默认选中项和拒绝按钮文案**（`拦截`）。
   `wb:…/common-p2lSs_-n.js:1 §SandboxApprovalCard`
7. WB 的计划正文**自动开在右侧产物面板**，同一份只开一次（`Set` 去重，上限 200）。
   `wb:…/common-p2lSs_-n.js:1 §usePendingPlanAutoOpen / §claimPendingPlanAutoOpen`
8. Qoder 的"记住"是**运行时算出的建议列表**（`permissionSuggestions`），不是固定按钮；建议文案自带范围（`本次任务始终允许访问"{{scope}}"`）。
   `qd:…:1 §permissionResources.zh §suggestion`
9. Qoder 的权限模式是**输入区的一个选择器**（`访问权限`：询问审批/自动审批/完全访问），不是卡上的按钮。
   `qd:…:1 §permissionModeLabels §ChatInput`
10. **两家都没有审批卡内的数字键/Esc 快捷键**；WB 只有 `Enter`/`Space`，Qoder 只有 Tab。
    `wb:…/high-credit-approval-floating-panel-DemeEjgI.js:1`；`qd:…:1 §PendingPermissionInteraction`
11. WB 的具体数值：字号 8/10/12/13/14/16/18/20/24/28/32；行高 16/18/20/22/24/28/32/40；间距 2/4/5/8/12/16/20/24/32/40/48/64；圆角 2/4/6/8/12/16/20/24/9999；三级文字 = 90%/70%/50%/45% 透明度。
    `wb:…/src-DIwCVExo.css:1`；`wb:…/_theme-light-DVW6cH6b.css:1`
12. Qoder 全部圆角用 `corner-shape:squircle`，半径 2/4/6/8/12/16/24；字号用 rem 阶梯，但实际组件大量用 `text-[13px] leading-[22px]` 硬值。
    `qd:…/index-yldrU_jD.css:1`
13. WB 的"进行中"动效只有一处：`cr-collapse--streaming` 的 2.2s 文字扫光，结束变静止灰。
    `wb:…/src-BllqCp4U.css:1`
14. WB 的"过程明细"是**用户可配置**的（简洁：只展开正在跑的那个；详细：全部展开），折叠后的入口是一句 `显示更早的 {count} 条`。
    `wb:…/zh-cn-J68R5HFz.js:470`；`wb:…/i18n-Bddor7Di.js:2`
15. Qoder 提问卡有**自动跳过倒计时**（`{{count}} 秒后自动跳过`）和**折叠标题**（`有 {{count}} 个问题待你回应`）。
    `qd:…:1 §chatActivity.question`
16. 两家的输入框 placeholder **都不随运行状态变化**；状态在输入框上方独立一栏。
    `wb:…/i18n-Bddor7Di.js:2 §input.placeholder`；`qd:…:1 §ChatInput`
17. 本机**没有** ChatGPT / Claude 桌面客户端（`AppData/Local/OpenAI` 只有 `Codex/` 与 `extension/`；`AppData/Local/Claude` 只有 `logs/`）。

### 6.2 推断（有证据但结论是推的）

1. `[推测]`**`.conversation-floating-slot-layer` 的 `column-reverse` 与我们无关**。它是插件扩展槽（`conversationFloatingSlot` contribution，锚点只有 `content-bottom`/`composer-top`），我在第一版里把这个容器当成审批卡的老家，是认错了；它为什么反向堆叠，取决于插件注册顺序，没找到实际使用它的插件。
2. `[推测]`**Qoder 的 `PendingInteractionOverlay` 一定渲在输入框 DOM 之前**。依据是 `absolute bottom-full`（相对最近定位祖先向上），且外层是 `pointer-events-none`；但没确认它的父元素到底是不是 composer 容器。
3. `[推测]`**WB 的 `pending.stateFallback` 是为"审批事件先到、详情后到"这个竞态准备的**。文案是"授权请求详情正在加载"，逻辑上吻合，但没有找到触发它的具体状态机代码。
4. `[推测]`**Qoder 的 50+ 条 `processing` 文案是轮换而非随进度变化**。词表里全是同义感叹（"快好了/马上好/差不多了"），没有任何与进度相关的词，但没找到选取逻辑。
5. `[推测]`**WB 的 24px 圆角浮层 + 8px 上滑 200ms 是刻意区别于消息气泡（12px）**，用来表达"临时层"。数值上确实两套，但没有设计文档佐证。
6. `[推测]`**Qoder 的 `glassSurfaceClassName` 是半透明磨砂**。类名含 glass 且与 `bg-bg-container` 组合使用，但 CSS 里没找到 `backdrop-filter` 的定义（可能在运行时注入）。
7. `[推测]`**WB 的"等待确认：{object}"是从 `foldStatusOverride` 参数进入状态行的**。`TurnFoldAvatar` 的 `foldStatus` 由 `streamingDuration.label` 和 `i`（`foldStatusOverride`）拼成，但 `i` 的赋值处在别的组件里，我没有追到 `metaFold.summary.waiting` 被塞进 `foldStatusOverride` 的那一行。文案与用途吻合，接线未证实。
8. `[推测]`**`useStreamingDurationMs` 的第一个参数语义是"启用/暂停"**。依据是它被传入 `!D`（= 在跑），且结束态不显示 `已处理`；但函数体没读到，不排除是"归还最后值"之类的语义。

### 6.3 明确没查清（不要当成结论用）

1. **Qoder 的色值。** 语义 token 名齐全（`--color-background-*` / `--color-text-*` / `--color-border-*`），但值是运行时注入，部分名字被混淆（`--q8a88b1` `--qca4712` `--q1fb96a`）。只确认了色彩空间是 oklch（`--color-green-500:oklch(72.3% .219 149.579)`）。**亮/暗两套的具体 hex 全部未知。**
2. **Qoder 的 permission card 具体样式。** 组件名 `PermissionCard` 已知、`variant` 有 `warning`/`info` 两档已知、`glassPermissionCardProps` 已知，但它的 CSS 全是 Tailwind 原子类内联在 JSX 里，没有语义类名可锚定；未能还原出完整的外观数值（padding/字号/间距）。
3. **WB 的计划卡在对话流中的对位组件。** 我确认了 `PendingPlanPanel`（浮层/待定态）和 `exit-plan-mode-floating`（浮层版本）两种，但**没有找到"已批准的计划在对话流里怎么展示"** 的那个组件。`plan-host` 是 project-plan 联邦宿主的空壳（`plan-host-CC9M2B5k.css` 只有 3 条规则）。
4. **WB 侧栏如何区分同名任务。** 只确认了有 `conversation.sectionFlag.tooltip.{working,pending,failed,unread}` 四种状态旗标和 `conversation.time.*` 相对时间；**没有找到会话行组件的源码**（侧栏在 `wb-status-chips` 那个 89KB 的 chunk 里，但类名被 CSS Modules 哈希成 `_status_1sy7k_99`，无法确认渲染内容）。
5. **WB 的 `ask-user-question`（工具级提问）和 `Qc`（浮层提问卡）是不是同一个组件。** 前者是 `common-EkSkH3Q0.css` 里的 `.ask-user-question`（无边框，8px 竖 padding，5 种终态），后者被 `PendingQuestionPanel` 用来渲染浮层卡。两者样式完全不同，但都把文案走 `tool.askUserQuestion.*`。**不敢断言是一个组件的两个 variant。**
6. **WB 的数字键是否存在但藏在别处。** 我只检查了 `high-credit-approval-floating-panel`、`exit-plan-mode-floating`、`PendingPlanPanel` 三处。`Qc` 组件的源码没找到（它被 import 进来，别名在压缩后不可追），所以"WB 全站没有数字键"这句话应弱化为"**在能读到的三个审批卡实现里都没有**"。
