# 代码高亮 / 代码面板 / diff / 产物预览：opencode 与 DeepSeek Harness 实现调研

取证时间：2026-11-19。取证方式：直接读源码，不是看截图猜。

- **opencode**：`anomalyco/opencode`，根 `package.json` 版本 `2.3.0`（SolidJS + Vite 重写后的版本，不是早期 Go TUI，也不是 Next.js）。文件经 GitHub Contents API + jsDelivr CDN（`@dev`）拉取。
- **DSH（DeepSeek Harness）**：`deepseek-ai/deepseek-harness`，版本 `0.2.2-rc.5`，`apps/web` 是 **React 18 + Vite 6**（不是 Vue；Vue 只出现在 `website/` 文档站）。
- **我们**：`web/package.json` 实际是 **Next 16.4.1 + React 19.3.0 + Tailwind v4**（不是 15），依赖只有 `clsx` / `tailwind-merge`。

已有的 `docs/ui-reference.md` 讲的是信息架构与视觉密度，本文只讲**技术实现**：库、版本、组件、数据从哪来、安全边界。

---

## 一、一句话结论

**我们最该先补的三件，按性价比排序：**

1. **shiki 4.6 + CSS 变量主题（而不是某个写死的 `github-dark`）**，接到时间线的代码块上；已完成的块放服务端/RSC 预渲染，流式块用 `codeToTokensBase` + `grammarState` 增量续算。这是 DSH 的路线，代码量最小，颜色能跟着我们现有的 Tailwind token 走。
2. **右侧产物面板 = 一块 iframe，按类型换 sandbox 预设**（HTML：`srcDoc` + `sandbox=""` 静态 / Blob + `sandbox="allow-scripts"` 交互；图片：直接 `<img>`；PDF：原生 iframe）。opencode 的 `ArtifactFrame` 只有约 20 行，是全文最值得照抄的组件。
3. **diff 先做「统一视图 + 折叠未变更区」**，数据由后端算好（unified patch），前端只用 `diff@9` 的 `structuredPatch` 兜底。并排视图和虚拟滚动放到第二批。

一句话版本：**别自研高亮，别自研 diff 算法，别用"独立端口"做预览。**

---

## 二、技术选型表

| 能力 | opencode（2.3.0）用什么 | DSH（0.2.2-rc.5）用什么 | 我们该用什么（Next 16 + React 19 + Tailwind v4） |
|---|---|---|---|
| 语法高亮库 | `@pierre/highlights@0.2.0`（WASM/WAT 高亮器），经 `@pierre/diffs@1.6.1` 调用；catalog 里另有 `shiki@4.4.3` + `marked-shiki@1.2.1` | `shiki@^4.4.3` + `@shikijs/langs@^4.4.3`，`createHighlighterCoreSync` + **JavaScript 正则引擎**（不加载 oniguruma WASM） | **`shiki@^4.6.0` + `@shikijs/langs@^4.6.0`**，同样用 JS 正则引擎；不引入 WASM |
| 主题 | 自定义 `OpenCodeTheme`（token → CSS 变量名），不是现成主题名；注册时第三参 `"zed"` 作为兜底 | `createCssVariablesTheme({ name: 'css-variables' })`，所有颜色走 `--shiki-*` | 自定义 css-variables 主题，把 `--shiki-*` 映射到我们的设计 token |
| 高亮在哪儿跑 | 浏览器；Markdown 围栏在 **Web Worker**（`markdown.worker.ts`），文件/diff 在 `@pierre/diffs` 的 **worker pool（2 个）** | 浏览器**主线程同步**，靠 `IntersectionObserver` 视口门控 + 懒加载语法 + `tokenizeMaxLineLength: 1000` 兜 | 已定稿内容：**Server Component 预渲染成 HTML**；流式内容：**客户端 Worker**（主线程同步会掉帧） |
| 流式高亮 | `LiveTokenizer`：只把「最后一行」当 unstable，前面都 stable，按 append 打补丁 | `StreamingHighlightSession`：`codeToTokensBase` + 传回 `grammarState`，只重新 tokenize 新增文本 | 抄 DSH 的 `grammarState` 链式增量，放进 Worker |
| 行号 | 文件/diff 查看器有；Markdown 围栏默认无 | `lineNumbers` 可选 prop，行号宽度按位数算 | 代码面板有；时间线内联代码块不做行号（省空间） |
| 超长行 | `overflow: "wrap"` 默认换行 + 「换行/不换行」按钮（宽度溢出时才出现） | 卡片工具栏上的 `wrap` 开关，默认换行（`CodeBlock` 默认 `true`，`DiffBlock` 默认 `false`） | 默认换行 + 溢出才显示切换按钮（抄 opencode 的 ResizeObserver 判定） |
| 代码面板结构 | 文件树（`@pierre/trees`）+ 多 tab + 单文件视图；文件内容整份读入 + 内存缓存 + watcher 失效 | 右栏 tab（`ui-sidebar-right`）+ 文档预览插件按后缀分派 | 右栏 tabs：文件 / diff / 产物预览三选一，文件树懒加载 |
| 大文件 | >200 行启用虚拟滚动；diff 两侧任一 >500,000 字符时关掉词级 diff 和 tokenize；媒体 >256MB 只留占位 | 单次比对最多渲染 **5000 行**，超出截断并提示；代码块本身不虚拟化 | 虚拟滚动阈值 200 行；diff 上限 5000 行；二进制不读进内存 |
| diff 算法 | 后端出 patch（hunk + additionLines/deletionLines），前端 `@pierre/diffs` 渲染（内部也是 `diff` 8.0.4） | 后端出 `WorkspaceDiffHunk`；前端 `diff@^9.0.0` 的 `structuredPatch` 兜底局部补齐 | **后端出 unified patch**；前端 `diff@9.0.0` 只做小范围补齐 |
| diff 形态 | 默认 `unified`，`diffStyle` 由调用方传；`diffIndicators: "bars"`（左侧 2px 色条），`lineDiffType: "word-line"` | 统一/并排可切（`data-review-view="split|unified"`），两侧行号对齐、**同步滚动** | 先 unified；并排第二批做，但行号对齐和同步滚动要一开始就设计进 DOM |
| 产物预览（HTML） | `<iframe src={blobUrl} sandbox="allow-scripts allow-popups allow-forms allow-modals" referrerPolicy="no-referrer">`（无 `allow-same-origin` → 不透明源） | 静态：`<iframe srcDoc sandbox="">`；交互（需手动开）：打包相对 js/css 成 Blob + `sandbox="allow-scripts"` | 静态 `srcDoc` + `sandbox=""`；交互 Blob + `sandbox="allow-scripts"` |
| 产物预览（图片） | `<img class="max-h-[60vh]">`（data URL 或服务端流式 URL）+ 缩放/平移 stage + lightbox | `ImagePreview` + `ImageLightbox`（`ui-primitives`） | `<img>` + 点击开 lightbox，先不做缩放 stage |
| 产物预览（PDF） | 原生 iframe + `#navpanes=0&view=FitH`，**不加 sandbox**（Chromium 内置阅读器） | 独立 `pdf/` 渲染器（同样走 iframe + 原生阅读器） | 原生 iframe，同参数 |
| 其他类型 | docx/xlsx/pptx 自研渲染器（worker 里排版，`gui-plugins/src/microsoft-office/*`）；音视频原生标签 | docx/xlsx/pptx/CSV 各有渲染器（`document/` `excel/` `office/`） | **先不做**；Office 只给下载 + 首屏缩略图 |
| 内置浏览器 | 仅桌面端：Electron `WebContentsView`（`sandbox: true`、`contextIsolation: true`、`devTools: false`、`webviewTag: false`、独立 `partition`），+ CDP 工具（快照/网络/灯塔） | Web 端：普通 `<iframe>` + 严格 sandbox；桌面端：Electron `<webview>` | 不做内置浏览器；"在浏览器打开"按钮即可 |

依赖来源：`packages/app/package.json`、`packages/ui/package.json`、`packages/session-ui/package.json`（opencode）；根 `package.json` 的 `workspaces.catalog`；`apps/web/package.json`、`packages/client/ui-primitives/package.json`、根 `package.json` 与 `pnpm-workspace.yaml`（DSH）。

---

## 三、代码高亮：两家到底怎么做的

### 3.1 opencode：把主题做成了「CSS 变量名表」

`packages/session-ui/src/pierre/index.ts:14`：

```ts
registerCustomTheme(OpenCodeTheme.name, () => Promise.resolve(OpenCodeTheme), "zed")
export const highlightOptions = { theme: OpenCodeTheme.name, preferredHighlighter: "highlights" } as const
```

主题本体在 `packages/ui/src/context/highlight-theme.tsx`，**一个颜色值都没有**，全是变量名：

```ts
export const OpenCodeTheme = {
  name: "OpenCode",
  appearance: "dark",
  cssVariables: { prefix: "--" },
  style: {
    "editor.background": "color-background-stronger",
    "editor.foreground": "text-base",
    syntax: {
      keyword: "syntax-keyword",
      string: "syntax-string",
      comment: "syntax-comment",
      function: "syntax-primitive",
      type: "syntax-type",
      property: "syntax-variable",
      punctuation: "syntax-punctuation",
      "diff.plus": "syntax-diff-add",
      "diff.minus": "syntax-diff-delete",
      // …
    },
  },
} satisfies Theme
```

真正的色值在主题 JSON：`packages/ui/src/theme/themes/oc-2.json`

```json
"syntax-comment": "var(--v2-text-text-muted)",
"syntax-keyword": "var(--v2-pink-800)",
"syntax-string":  "var(--v2-green-800)",
"syntax-primitive":"var(--v2-pink-800)",
"syntax-property":"var(--v2-orange-800)",
"syntax-constant":"#007b80",
"syntax-diff-add": "var(--v2-state-fg-success)",
"syntax-diff-delete": "var(--v2-state-fg-danger)"
```

**这套做法的价值**：换主题只改 JSON，高亮器完全不知道有主题这回事；深浅色用 `light-dark()` 在同一份变量里切换。

diff/文件查看器的默认参数（`index.ts:308-326`）：

```ts
{
  theme: "OpenCode", themeType: "system",
  disableLineNumbers: false,
  overflow: "wrap",
  diffStyle: style ?? "unified",
  diffIndicators: "bars",          // 左侧 2px 增删色条，不用 +/- 前缀
  lineHoverHighlight: "both",
  expansionLineCount: 20,          // 折叠的未变更区展开一次 20 行
  hunkSeparators: "line-info-basic",
  lineDiffType: "word-line",       // 词级高亮
  maxLineDiffLength: 1000,
  maxLineLengthForHighlighting: 1000,
  disableFileHeader: true,
}
```

尺寸也全部是变量（`index.ts:328-338`）：`--diffs-line-height: 24px`、`--diffs-tab-size: 2`、`--diffs-min-number-column-width: 3ch`，字号绑 `--font-size-small`。

### 3.2 opencode 的流式高亮：Worker 里只重算最后一行

`packages/session-ui/src/components/markdown.worker.ts:109-145`：

```ts
const previous = streams.get(request.key)
const reset = !previous || previous.language !== language || !request.text.startsWith(previous.source)
const stream = reset ? { language, source: "", tokenizer: new LiveTokenizer({ lang, theme: OpenCodeTheme }) } : previous

// 前面所有行都已定稿：lexer 只把状态往后带；只有最后一行还在长
const start = stream.tokenizer.lineCount - 1
stream.tokenizer.applyEdits([{ range: { start: end, end }, newText: request.text.slice(stream.source.length) }])

post({
  type: "highlight", reset,
  stable: /* start..last-1 行的 token，逐行带 "\n" */,
  unstable: /* 最后一行的 token */,
})
```

配套的两层优化：

- **块级**：`packages/session-ui/src/components/markdown-stream.ts` 用 `@opencode/patchmark`（自研 `createMarkdownStream`，`packages/patchmark`，`micromark` + `commonmark` 做测试对照）把 Markdown 切成稳定块，只重投影末尾两块。
- **DOM 级**：主线程只对新增的 `stable` 行做 append，`unstable` 行原地替换，不重建整棵树。

### 3.3 DSH：shiki 同步核心 + 视口门控 + grammarState 增量

`packages/client/ui-primitives/src/markdown/highlight.ts:21-25`：

```ts
import { createHighlighterCoreSync, createCssVariablesTheme } from 'shiki/core'
import { createJavaScriptRegexEngine, defaultJavaScriptRegexConstructor } from 'shiki/engine/javascript'
import langTs from '@shikijs/langs/typescript'
import langBash from '@shikijs/langs/shellscript'
import langJson from '@shikijs/langs/json'
```

三条明确的设计取舍（源码注释里写着的）：

1. **不用 oniguruma WASM**：JavaScript 正则引擎，"bundle-friendly"。代价是极少数语法有差异。
2. **语法白名单 + 懒加载**：启动只装 TS / shell / JSON，其余（python、go、rust、yaml、html、css、sql…共 30+ 个）走 `import()` 动态加载。没加载完时先按纯文本渲染，加载完通过 `subscribeGrammarLoaded` 触发重渲。未知语言**永远降级为纯文本，不报错**。
3. **视口门控**：`useViewportHighlighting.ts` 用一个**全文档共享的 IntersectionObserver**，代码块第一次进入视口才允许构建高亮树；被 `pre` 占位的纯文本先撑住布局。

增量实现（`highlight.ts`，`StreamingHighlightSession.updateFrame`）：

```ts
// 只对「新增的、以换行结尾的前缀」做 tokenize，把语法状态存下来给下一次用
const tokens = highlighter().codeToTokensBase(rest.slice(0, grownEnd), {
  lang: resolved, theme: 'css-variables',
  tokenizeMaxLineLength: 1000,
  ...(this.state === undefined ? {} : { grammarState: this.state }),
})
this.state = highlighter().getLastGrammarState(tokens)
// 最后一行始终重算（还没结束）
tail: this.tokenize(resolved, rest.slice(lastNewline + 1))
```

`CodeBlock.tsx` 再把它变成 DOM：**已完成的行按 32 行一组**放进 `<Fragment>`，React 只 reconcile 尾巴，DOM 结构逐行不变：

```ts
const STREAMING_LINE_GROUP_SIZE = 32
groups.push(<Fragment key={start}>{pending}</Fragment>)
```

非流式的块直接 `codeToHtml` + `dangerouslySetInnerHTML`（注释里明确说这是 shiki 官方认可的用法），并复刻 shiki 的 `<pre class="shiki css-variables" style="background:var(--shiki-background)">` 属性，保证「流式树 → 定稿树」切换不跳。

### 3.4 两家都做对的细节（值得抄）

| 细节 | opencode | DSH |
|---|---|---|
| 换行开关只在真的溢出时出现 | `markdown.tsx:281-305`，`ResizeObserver` 测量 `pre.scrollWidth > clientWidth` 才加 `data-code-overflow` | 工具栏常驻 `wrap` 按钮（更简单，但多一个控件） |
| 复制按钮复制的是 `pre.textContent` | 是 | 是（`rootRef.current?.querySelector('pre')?.textContent`） |
| 未知语言 | 保留 fence 的语言标签，按 `text` 高亮 | 同上，且用于判断 Mermaid |
| 超长行 | `tokenizeMaxLineLength` 之外还有 `overflow: wrap` + 横向滚动兜底 | `tokenizeMaxLineLength: 1000`，超出按单 token 处理 |

---

## 四、代码 / 文件面板

### 4.1 opencode：文件树 + tab + 整份读入 + 缓存

- **结构**：左文件树（`@pierre/trees@1.0.0-beta.6`，`packages/app/src/workspaces/files/tree-store.ts` 管懒加载），内容区按 `ArtifactKind` 分派查看器（`packages/util/src/artifact.ts` 15 种：`image|svg|audio|video|pdf|html|markdown|mermaid|table|font|document|spreadsheet|presentation|text`）。
- **加载**：`content-machine.ts` 是一个显式状态机（`loading|ready|refreshing|missing|error`），**整份文件一次读完**，没有分页；读取期间文件被改会记 `rerun`，读完再补一次；watcher 触发 `load.requested{force}`；LRU 淘汰（`cache.evicted`）。
- **大文件**：
  - >200 行 → `VirtualizedFile`（`pierre/virtualizer.ts:18`，视口上下各留 1000px overscan，约 100 行常驻）；
  - diff 任一侧 >500,000 字符 → `lineDiffType: "none"`、`maxLineDiffLength: 0`、`tokenizeMaxLineLength: 1`（`file.tsx` 的 `large()`）；
  - 二进制 >256MB → 只留占位（`workspaces/files/artifact.ts:58` `MAX_MEDIA_BYTES`）；
  - 图片/音视频用**服务端 URL 直接流式**，先 `HEAD` 拿 `content-length` 和 `last-modified` 拼版本号（`artifact.ts:26-50`），`<img>` 首帧先出。
- **只读**，但支持**按行选中 + 行内评论**（`data-selected-line`、`line-comment-*`、`Pierre` 的 `DiffLineAnnotation`）。

### 4.2 DSH：右栏 tab + 按后缀分派的文档预览

右栏是插件化的（`ui-sidebar-right` + `ui-sidebar-documentpreview`），预览器按目录分：`html/ pdf/ image/ markdown/ office/ excel/ code/ text/ zoom/`。文件内容同样是 Host 一次性给字节，前端渲染；代码预览复用 `ui-primitives` 的 `CodeBlock`。

### 4.3 给我们的结论

- 面板**不要做成"一整个 IDE"**。opencode 的文件树 + tab 是桌面端形态；我们右栏 360~420px，**单文件 + 顶部路径 + 上一处/下一处**就够，文件树用弹层。
- 文件内容**整份读**没问题，但要在前端做「按路径 + mtime」缓存，并且 >200 行必须虚拟滚动，否则 React 19 一次挂 3000 个带 span 的行会明显卡（opencode 的 200 行阈值是量过的，直接抄）。

---

## 五、diff 渲染

| 问题 | opencode | DSH |
|---|---|---|
| diff 谁算 | **后端**：每轮结束生成快照 diff，前端拿 `FileDiffInfo`（hunk + `additionLines`/`deletionLines`），按 tab 缓存（`turn-diffs.ts`） | **后端**：Host 出 `WorkspaceDiffHunk`（带 oldStart/newStart/lines）；前端只在「工具只给了新旧全文」时用 `diff` 的 `structuredPatch` 兜底 |
| 用什么库 | `@pierre/diffs`（内部依赖 `diff@9`/`shiki`），自研 shadow DOM 渲染 | `diff@^9.0.0`（`structuredPatch`，`context: 3`，`maxEditLength: 256`）+ 自研行渲染 |
| 形态 | 默认 unified，`diffStyle` 可传 `split`；`diffIndicators: "bars"` | unified / split **可切换**（`data-review-view`），split 时两列 `scrollLeft/scrollTop` 互相同步 |
| 行号 | 两侧行号列，宽度 `min-number-column-width: 3ch`，增行绿/删行红 | 两侧行号（`old`/`new` 独立编号），context 行两边都计数 |
| 折叠未变更 | `expansionLineCount: 20` + `hunkSeparators: "line-info-basic"`（点一下展开 20 行） | 小卡片做「头 8 行 + 尾 8 行 + `⋯`」，大视图按 5000 行截断 |
| 配色 | 增行底色 `green-600/0.05`（暗色 0.1），删行 `red-600/0.05`，行号文字 `green-900/green-400`；词级再加 `0.2/0.4` 强调 | 走 `--dsw-*` token；`data-diff-line="add|del|context"` |
| 大 diff | >500,000 字符关词级 diff；虚拟滚动（`Virtualizer`） |  >5000 行截断 + 提示 |

**关键差异**：opencode 把「折叠/虚拟滚动/词级 diff/行注释」全部交给 `@pierre/diffs`；DSH 自己写了约 300 行渲染，但拿到了「unified/split 切换 + 双侧行号 + 双侧同步滚动」的完全控制权。

**我们的建议**：先 `/api/*` 返回 unified patch 文本 → 前端 `parsePatch`（`diff@9` 自带）→ 自己渲染行（约 120 行）。等要做 split、行注释、超长 diff 虚拟滚动时，再评估换成 `@pierre/diffs@1.6.4`（它已经有 `./react` 导出，peer 支持 React 19，内部自带 shiki 4 + diff 9）。

---

## 六、产物直接预览（我们完全没有的部分）

### 6.1 opencode：一个 `ArtifactFrame` 解决 HTML / PDF

`packages/gui-plugins/src/file/artifact-view.tsx`（`ArtifactFrame`，约 718 行起）：

```tsx
const source = createMediaSource(() => (props.kind === "html" ? findableHtml(props.content) : props.content))
const src = () => (props.kind === "pdf" ? `${source().src}#navpanes=0&view=FitH` : source().src)

<iframe
  class="block h-full w-full flex-1 border-0 bg-white"
  src={src()}
  // PDF 用 Chromium 自带阅读器，不能 sandbox；HTML 跑在不透明源：拿不到 app 的 storage/cookie/带凭据请求
  sandbox={props.kind === "html" ? "allow-scripts allow-popups allow-forms allow-modals" : undefined}
  referrerPolicy="no-referrer"
/>
```

要点：

- **没有 `allow-same-origin`** → iframe 是不透明源，`localStorage`、cookie、`fetch` 带凭据全部拿不到；这正是"能跑 JS 但不能碰我们"的关键组合。
- 内容走 `blobUrlFromContent()`（`artifact.ts:100-117`），不是 `srcDoc`，所以**相对路径全废**——opencode 的做法是同时提供 `findableHtml()` 做文本内查找，不解决相对资源。
- 图片：`file-media.tsx` 里直接 `<img src={dataUrl} class="max-h-[60vh] max-w-full object-contain">`，点击进 `ImagePreview` 弹层；artifact 里另有一套可缩放/平移的 stage（`data-zoom="fit|actual"`）。
- 音视频：原生 `<audio controls>` / `<video controls>`，视频双击全屏（借 `popover` 规避 fullscreen API 限制）。
- Office：docx/xlsx/pptx **自研渲染器**（`gui-plugins/src/microsoft-office/`，worker 里做分页/排版/绘制），这是重武器，不建议我们短期碰。

### 6.2 DSH：静态与交互分离，资源内联

`packages/client/ui-sidebar-documentpreview/src/client/html/HtmlBody.tsx`：

```tsx
// 静态预览：srcDoc + 最严 sandbox（连脚本都不给）
<iframe srcDoc={html} sandbox="" />

// 交互预览（用户显式开启）：把相对 js/css 打包进一个文档，再给 Blob URL
<iframe src={frame.url} sandbox="allow-scripts" />
```

打包器（`html/pack.ts`）的边界写得很清楚：

```ts
const MAX_ASSET_BYTES = 4 * 1024 * 1024
const MAX_TOTAL_BYTES = 32 * 1024 * 1024
const MAX_ASSETS = 64
// 只处理「直接写在 HTML 里的 <script src="*.js"> 和 <link rel=stylesheet href="*.css">」
// 不支持 CSS 里的 url()/import、ES module、动态创建的资源
```

`bootstrap.ts` 在 iframe **内部**用 `Blob` 重建资源 URL，再 `document.write`——因为不透明源加载不了父页面创建的 blob URL。这个坑我们一定会踩，直接照抄。

### 6.3 内置浏览器：两家都只敢在桌面端做

| | opencode | DSH |
|---|---|---|
| 桌面 | Electron `WebContentsView`（`gui-plugins/src/browser/chromium.ts:217`），`partition` 隔离会话、`sandbox: true`、`contextIsolation: true`、`nodeIntegration: false`、`webviewTag: false`、`devTools: false`；URL 白名单只有 `http/https/about:blank` + 工作区内 `file:`（`browser/policy.ts:47-83`） | Electron `<webview>`，主进程审批 guest lease、导航与权限策略（`ui-sidebar-browser/README.md:12,71`） |
| Web | **没有**（浏览器面板是 desktop-only，`plugin-browser/README.md`） | `<iframe sandbox="allow-scripts allow-forms allow-same-origin allow-popups allow-popups-to-escape-sandbox">`，**不做代理**，因此受 `X-Frame-Options` / CSP / mixed content 限制；可临时关 sandbox 但会显示琥珀色警告（README:63） |

结论：**"内置浏览器"不是前端能做出来的东西，是 Electron 能力。** 我们在 Web 里最多做到「iframe 预览本地 dev server」，且必须承认大量站点会拒绝被嵌。

### 6.4 三种预览路径对比

| 方案 | 做法 | 安全边界 | 复杂度 | 适用 |
|---|---|---|---|---|
| **A. iframe `srcDoc`** | 后端把文件内容给前端，前端 `srcDoc={html}` + `sandbox=""` | 最严：不给脚本、不给同源；**不能**加载相对资源（CSS/JS/图片全断） | ★☆☆☆☆ | 纯静态 HTML 报告、单文件 demo |
| **B. 本地文件服务（推荐主路径）** | `GET /api/workspaces/:id/raw?path=...` 返回字节；HTML 预览时先抓 HTML，重写相对 URL 为绝对 API URL（或注入 `<base href="/api/.../">`），再 `srcDoc` / Blob | iframe 仍不透明源；API 只允许工作区内路径（规范化 + 前缀校验，防 `..`）；响应加 `Content-Security-Policy: sandbox` 与 `X-Content-Type-Options: nosniff`；**生成的页面不能读文件系统**，只能读同目录下我们放行的资源 | ★★★☆☆ | 多文件站点、带 CSS/JS/图片的产物 |
| **C. 独立端口（dev server）** | 起一个静态服务器（如 `:4173`）指向 workdir，iframe 直接 `http://localhost:4173/...` | 最弱：页面与我们的 app **同机但不同源**，能访问 `localhost` 上的一切（包括我们自己的 API），需要额外的网络策略；还要处理端口占用/生命周期/清理 | ★★★★★ | 需要真实 URL 行为（router、service worker、绝对路径）时；桌面端本地开发 |
| **D. Electron `WebContentsView`** | 桌面壳里挂真实浏览器 | 由主进程策略控制（可做到只允许 http/https + 工作区 file:） | ★★★★★ | 只有做桌面端才考虑 |

**给我们的落地顺序：A → B →（可选）C。**

- 第 1 周：A + 图片 + PDF，覆盖 80% 的产物（报告 HTML、截图、PDF）。
- 第 2 周：B，加一个 `/raw` 路由 + `<base>` 注入，解决相对 CSS/JS。
- C 只在"用户明确要跑 SPA"时做，且必须绑定 `127.0.0.1`、随机端口、会话结束即关。

**明确不要做**：把产物 HTML 直接 `dangerouslySetInnerHTML` 进主文档。opencode 和 DSH 都**没有**这么做（DSH 的 Markdown 渲染器甚至默认丢弃 raw HTML，见 `ui-primitives` README）。

---

## 七、可以直接照搬的组件结构

### 7.1 高亮层（3 个文件）

```
web/src/lib/highlight/
  theme.ts        // token -> CSS 变量名表（照抄 highlight-theme.tsx 的形状，去掉 Solid 依赖）
  server.ts       // 'server-only'：createHighlighterCore + 语言白名单，给 RSC 用
  client.ts       // 'use client'：Worker 入口 + useStreamingHighlight()
```

`theme.ts` 的关键是**不写颜色**：

```ts
export const codeTheme = {
  name: "kbg",
  appearance: "dark",
  cssVariables: { prefix: "--" },
  style: {
    "editor.background": "surface-code",
    "editor.foreground": "text-base",
    syntax: {
      keyword: "syntax-keyword", string: "syntax-string", comment: "syntax-comment",
      function: "syntax-function", type: "syntax-type", variable: "syntax-variable",
      constant: "syntax-constant", punctuation: "syntax-punctuation",
      "diff.plus": "syntax-diff-add", "diff.minus": "syntax-diff-delete",
    },
  },
}
```

Tailwind v4 侧（`globals.css` 的 `@theme` 或 `:root`）：

```css
:root {
  --syntax-keyword: var(--color-pink-700);
  --syntax-string:  var(--color-emerald-700);
  --syntax-comment: var(--color-neutral-500);
  /* … */
  --shiki-background: var(--color-surface-code);
  --shiki-foreground: var(--color-text-base);
}
.dark { --syntax-keyword: var(--color-pink-300); /* … */ }
```

> 注意：shiki 的 css-variables 主题会把 token 颜色输出成 `color: var(--shiki-token-keyword)` 这类**内联 style**，所以我们的变量名要按 shiki 的 `--shiki-` 前缀规则生成，或者干脆用 `cssVariables: { prefix: "--" }` 让它直接用 `--syntax-keyword`（opencode 就是这么干的）。

服务端（定稿内容）：

```ts
// web/src/lib/highlight/server.ts
import "server-only"
import { createHighlighterCore } from "shiki/core"
import { createJavaScriptRegexEngine } from "shiki/engine/javascript"
import langTs from "@shikijs/langs/typescript"
import langBash from "@shikijs/langs/shellscript"
import langJson from "@shikijs/langs/json"

let hl: Promise<HighlighterCore> | undefined
export const highlighter = () => (hl ??= createHighlighterCore({
  themes: [codeTheme], langs: [langTs, langBash, langJson],
  engine: createJavaScriptRegexEngine(),
}))

export async function highlightHtml(code: string, lang?: string) {
  const h = await highlighter()
  const resolved = lang && h.getLoadedLanguages().includes(lang) ? lang : "text"
  return h.codeToHtml(code, { lang: resolved, theme: "kbg", tokenizeMaxLineLength: 1000 })
}
```

流式（客户端 Worker，照抄 DSH 的 `grammarState` 链）：

```ts
// worker 内，每次收到增量文本
const tokens = h.codeToTokensBase(appendedText, { lang, theme: "kbg", grammarState: prev })
self.postMessage({ stable: tokens.slice(0, -1), tail: tokens.at(-1), state: h.getLastGrammarState(tokens) })
```

React 侧：`<CodeBlock>` 收 `{ code, lang, streaming }`，`streaming` 时按 **32 行一组** 缓存 `<Fragment key={lineStart}>`；`streaming` 结束切到服务端给的 HTML（或同一棵树不变），避免"高亮闪一下"。

### 7.2 代码面板

```
web/src/components/artifact-panel/
  ArtifactPanel.tsx      // 右栏容器：tabs(文件/diff/预览) + 路径 + 操作
  ArtifactFrame.tsx      // iframe 分派：html | pdf
  ArtifactImage.tsx      // img + lightbox
  ArtifactCode.tsx       // 读文件 -> CodeBlock + 行号 + wrap
  ArtifactDiff.tsx       // patch 文本 -> 行渲染
  ArtifactFallback.tsx   // 二进制/Office：图标 + 大小 + 下载
  kinds.ts               // 后缀 -> kind（照抄 util/src/artifact.ts，15 种收敛到 8 种）
```

`ArtifactFrame` 的大小（这是 opencode 的精华，不到 25 行）：

```tsx
export function ArtifactFrame({ kind, src }: { kind: "html" | "pdf"; src: string }) {
  if (kind === "pdf") {
    return <iframe src={`${src}#navpanes=0&view=FitH`} className="h-full w-full border-0 bg-white" />
  }
  return (
    <iframe
      src={src}
      className="h-full w-full border-0 bg-white"
      sandbox="allow-scripts allow-popups allow-forms allow-modals"
      referrerPolicy="no-referrer"
      title="预览"
    />
  )
}
```

### 7.3 时间线里的代码块（增量改造点）

现在时间线的工具调用是「逐行参数 + 前 6 行输出」。改造成：

```
ToolCallRow
├─ 参数区：JSON 按行 -> <CodeBlock lang="json" maxLines={12} />
├─ 输出区：<CodeBlock lang={detectFromTool()} maxLines={6} expandable streaming={isStreaming} />
└─ 底部：`查看文件` / `查看 diff` 按钮 -> 打开右栏面板（带 path + 行号锚点）
```

- `detectFromTool()`：`read`/`edit`/`write` 按文件后缀定语言；`shell` 用 `bash`；其他用 `text`。
- 输出区**不要**默认展开全部；`maxLines` 折叠 + 「展开」后进面板。
- 每个代码块必须**稳定 key**（`toolCallId + partIndex`），否则流式高亮每帧重建。

---

## 八、要装的依赖（具体版本，2026-11-19 npm 最新）

| 包 | 版本 | 用途 | 备注 |
|---|---|---|---|
| `shiki` | `^4.6.0` | 高亮核心 | DSH 用 `^4.4.3`，opencode catalog 是 `4.4.3`；4.6 的 `shiki/core` + `shiki/engine/javascript` API 一致 |
| `@shikijs/langs` | `^4.6.0` | 语法包 | 按需 `import("@shikijs/langs/python")`，不要整包引入 |
| `diff` | `^9.0.0` | `parsePatch` / `structuredPatch` | DSH 用 `^9.0.0`；opencode 通过 `@pierre/diffs` 用 9.0.0 |
| `@pierre/diffs` | `^1.6.4` | （可选）现成 diff 视图 + 虚拟滚动 + 双栏 | 自带 `./react` 导出，peer `react ^18.3.1 \|\| ^19`；依赖 `shiki ^4`、`diff 9.0.0`、`@pierre/highlights` |
| `@tanstack/react-virtual` | `^3.14.26` | （可选）文件 >200 行时虚拟滚动 | DSH 的 `apps/web` 就用这个 |
| `katex` | `^0.19.1` | （可选）公式 | DSH `^0.18.7`，opencode `0.18.11` |
| `marked` + `marked-shiki` | `^18.2.1` / `^1.2.1` | （可选）如果消息正文还没有 Markdown 渲染 | opencode 用 `marked@18.0.11` + 自写 renderer（不是 marked-shiki） |

**不要装**：`monaco-editor`（~5MB，我们要的是只读展示）、`codemirror`（同上）、`highlight.js`（主题体系和我们 token 对不上）、`prismjs`（同上）、`@pierre/highlights` 单用（0.5.1 是 WASM，体积和初始化成本都高于 shiki 的 JS 引擎；除非要 opencode 那种"每帧只算最后一行"的 `LiveTokenizer`）。

---

## 九、两家的取舍对照（写方案时可以直接引用）

| 决策点 | opencode 选择 | DSH 选择 | 我们的约束 → 结论 |
|---|---|---|---|
| 高亮引擎 | WASM（`@pierre/highlights`） | 纯 JS 正则（shiki core） | 我们的产物在浏览器里跑、要控制包体积 → **JS 引擎** |
| 高亮位置 | Worker（Markdown + diff 都是） | 主线程 + 视口门控 | Next 的 RSC 能省掉首屏 JS → **定稿走服务端，流式走 Worker** |
| 主题 | CSS 变量名表 + 主题 JSON | `createCssVariablesTheme` + `--shiki-*` | 我们已有 Tailwind token → **css-variables** |
| diff 计算 | 后端快照 diff | 后端 hunk，前端兜底 | 我们已有任务/步骤数据 → **后端出 unified patch** |
| diff 渲染 | 第三方（`@pierre/diffs`），功能全 | 自研，可控 | 先自研 120 行；需要 split/注释再换 |
| 预览隔离 | Blob + `sandbox`（无 same-origin） | 静态 `srcDoc` + `sandbox=""`；交互 Blob + `allow-scripts` | **默认最严，交互需用户显式开启** |
| 大文件 | 200 行虚拟化 / 500k 字符降级 / 256MB 上限 | 5000 行截断 | 两个阈值都抄，成本很低 |

---

## 十、未找到 / 存疑

- **opencode 全局的 unified/split 切换开关**：`diffStyle` 是 `createDefaultOptions(style)` 的入参，仓库内文件查看器固定传 `"unified"`（`file.tsx`）。是否有 UI 让用户切到 split，**未找到**（可能在 `session-review` 的 v2 组件里，未逐一读完）。
- **opencode 的 `shiki@4.4.3` / `marked-shiki@1.2.1` 实际调用点**：catalog 里有，`packages/app` 与 `packages/ui` 的 dependencies 里**没有**；推测是 `services/site`（官网/文档）在用，**未确认**。
- **DSH 代码块的虚拟滚动**：没找到；只有 `MAX_RENDERED_LINES = 5000` 的截断。
- **DSH 的内置浏览器是否代理本地端口**：README 明确写 "does not proxy or probe remote pages"（`ui-sidebar-browser/README.md:63`），所以 loopback 是 iframe 直连，受混合内容/私有网络策略限制。
- **opencode Web 端的内置浏览器**：没有。`plugin-browser` 的工具全部依赖桌面端 attach（`packages/plugin-browser/README.md`）。
- **两家的「产物」如何被识别**：opencode 靠 `artifactKind(path)`（后缀表）；DSH 靠 `ui-deliverables` 的 changed-files + presented files（turn 结束时由 Host 汇总）。我们如果只有"最后一步写了哪些文件"，建议**直接扫任务产出的文件写入事件**，不要猜。

---

## 附：关键源码索引

**opencode（`anomalyco/opencode@2.3.0`）**

| 主题 | 路径 |
|---|---|
| 高亮/diff 默认参数 | `packages/session-ui/src/pierre/index.ts` |
| 主题（token → 变量名） | `packages/ui/src/context/highlight-theme.tsx` |
| 主题色值 | `packages/ui/src/theme/themes/oc-2.json` |
| 流式高亮 Worker | `packages/session-ui/src/components/markdown.worker.ts` |
| 流式 Markdown 分块 | `packages/session-ui/src/components/markdown-stream.ts`、`packages/patchmark/` |
| 代码块 UI（复制/换行按钮注入） | `packages/session-ui/src/components/markdown.tsx` |
| 文件/diff 查看器 | `packages/session-ui/src/components/file.tsx` |
| 虚拟化阈值 | `packages/session-ui/src/pierre/virtualizer.ts` |
| Worker pool | `packages/session-ui/src/pierre/worker.ts` |
| 文件内容状态机 | `packages/app/src/workspaces/files/content-machine.ts` |
| 大文件/媒体策略 | `packages/app/src/workspaces/files/artifact.ts` |
| 产物 iframe | `packages/gui-plugins/src/file/artifact-view.tsx` |
| 产物类型表 | `packages/util/src/artifact.ts` |
| 内置浏览器（桌面） | `packages/gui-plugins/src/browser/chromium.ts`、`policy.ts` |
| 浏览器工具 | `packages/plugin-browser/` |

**DSH（`deepseek-ai/deepseek-harness@0.2.2-rc.5`）**

| 主题 | 路径 |
|---|---|
| 高亮核心 | `packages/client/ui-primitives/src/markdown/highlight.ts` |
| 代码块组件 | `packages/client/ui-primitives/src/markdown/CodeBlock.tsx` |
| 视口门控 | `packages/client/ui-primitives/src/markdown/useViewportHighlighting.ts` |
| 增量 Markdown | `packages/client/ui-primitives/src/markdown/incremental.ts` |
| 行内 diff 卡 | `packages/client/ui-primitives/src/DiffBlock.tsx` |
| 文件比对（unified/split） | `packages/client/ui-deliverables/src/client/FileDiff.tsx` |
| 变更文件卡 | `packages/client/ui-deliverables/src/client/ChangedFiles.tsx` |
| HTML 预览 | `packages/client/ui-sidebar-documentpreview/src/client/html/HtmlBody.tsx` |
| 资源打包 | `.../html/pack.ts`、`.../html/bootstrap.ts` |
| 内置浏览器 | `packages/client/ui-sidebar-browser/`（README 里写了 sandbox 与桌面差异） |
