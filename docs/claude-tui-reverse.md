# Claude Code 终端 UI 逆向（v2.1.271）

> 目标物：`E:/env/npm-global/node_modules/@anthropic-ai/claude-code/bin/claude.exe`
> （229,622,944 字节，PE + Bun 单文件；内嵌 JS 源码明文）
> 取证日期：2026-11-19　｜　实跑环境：Windows 11 + Git Bash + winpty + 智谱网关（glm-4.5-flash）

---

## 0. 结论摘要

1. **它的排版是「一列、两个锚点」**：所有内容挂在同一条左轴（助手行首 `●`、工具结果 `⎿`、思考块 `⎿`、任务 `◼`），靠**缩进 2 空格一档**分层，不靠边框和卡片；全屏只有分割线（`─`）和 diff 预览的虚线（`╌`）两种线。
2. **工具调用是「一行摘要 + 一条结果」**，摘要永远是 `名字(参数)` 且按可用宽度硬截断，状态用**后缀**表达（` · running` / ` · error` / ` · no result`），而不是换图标或换颜色块。
3. **折叠文案是三套固定模板**：`… 12 more lines`（工具输出，行优先）、`… +340 chars`（字符兜底）、`… (+3 more lines)`（内联卡片/审批卡），阈值写死为**参数 12 行、结果 6 行、卡片 12 行**。
4. **审批默认项是「允许」，但信任目录默认是「No, exit」**——动用户已有资产前默认拒绝，执行用户刚要求的动作时默认允许；拒绝的呈现不是红叉，而是把同一行结果改成 `User rejected write to X`。
5. **颜色是语义键不是红绿**：深色主题里 `diffAdded` 是 `rgb(34,92,43)`（暗绿**底**），文字色是 `diffAddedWord` `rgb(56,166,96)`；整套主题有 7 个变体（含 colorblind 与 ANSI-16 版），说明它从不直接写死 `green`。

---

## 1. 取证方法与环境

三种方法都跑通了（第 2 种在 TUI 上做了真实抓取，非推测）。

### 1.1 字符串提取（成功）

- 直接 `Buffer` 分块读（8MB/块），`toString('utf8')` 后正则扫 `[\x20-\x7E]{8,}` 与含 Unicode 的可见片段，去重得到 **278,737 条 ASCII 片段 / 508,650 条含 Unicode 片段**。
- 关键发现：**JS 源码以明文集中在文件尾部**，不是压缩字节码。
  - `function gr({call:` 命中偏移 `216,831,676`
  - `var Tr=` 在 `220.7MB / 221.6MB / 223.3MB / 224.5MB` 均命中
  - 因此把 `198,000,000 → EOF` 整段导出为 31.5MB 连续 JS，可顺序阅读（这比按行去重后 grep 有用得多——去重会破坏上下文相邻性）。
  - 另有 `120,000,000 → 198,000,000` 一段含**完整主题色表**。
- `grep -a` 交叉验证文案确实在二进制里（下文每条都标了函数名或常量名，便于复现）。

> 产出：`D:/kbg harness/_shots/claude-strings.txt`（按主题归类，非倾倒）

### 1.2 真跑起来（成功，含 TUI）

| 命令 | 结果 |
|---|---|
| `claude.exe --version` | `2.1.271 (Claude Code)` |
| `claude.exe --help` | 取到完整命令面，其中与 UI 直接相关的几条见下 |
| `claude.exe -p "say OK"` | 网关通，返回 `OK`（模型 glm-4.5-flash） |
| `echo "" \| claude.exe` | **失败**：`Error: Input contained only whitespace... when using --print` —— 非 TTY 下 stdin 被判成管道，自动降级 `--print`，**进不了 TUI** |
| `winpty claude.exe` | **成功**：拿到真实渲染字节（含 ANSI），完成 onboarding、进主界面、触发工具调用、触发审批、拒绝、diff 编辑 |

**结论：Windows 上要抓 TUI 必须给真 PTY。** `node-pty` 未安装，但 Git Bash 自带 `winpty` 足够：

```bash
{ sleep 8; printf '\r'; sleep 5; } | winpty -Xallow-non-tty -Xplain claude.exe
```

> 产出：`ref-claude-01…06`（`.txt` 为剥离 ANSI 的可读文本，`.ansi.bin` 为原始字节）

`--help` 里能反推出 UI 取舍的几条（原文）：

```
--ax-screen-reader     Render screen-reader friendly output (flat text, no decorative
                       borders or animations).
--brief                Enable SendUserMessage tool for agent-to-user communication
--effort <level>       Effort level for the current session (low, medium, high, xhigh, max)
--autocompact <auto|tokens>   Auto-compact window size (auto, or 100k–1M tokens)
--bg, --background     Start the session in the background and return immediately
--bare                 Minimal mode: skip hooks, LSP, plugin sync, ...
```

`--ax-screen-reader` 的存在本身就是证据：**装饰性边框和动画是这套 UI 的一等公民**，需要专门开一个开关才能关掉；`--effort` 对应主界面右上角那个 `◈ max · /effort`。

### 1.3 交叉验证（成功，但文档有版本差）

用 WebFetch 取 `code.claude.com/docs/en/interactive-mode`（`docs.claude.com` 会 301 到 `code.claude.com`）。
文档确认：`Esc` 中断、权限提示上 `Esc` 等同选 No、`Ctrl+O` 展开工具详情、"Called slack 3 times" 这类单行合并、任务清单一次最多 5 项。
**不一致处已标注**：本机 2.1.271 的折叠阈值是 3 条（`mr=3`），文档（面向 2.1.287+）写 5 条。以二进制常量为准，并注明差异。

### 1.4 图与不能保证的部分

- **没有操作系统级截图**：终端里只有文本流，本机没有可用的终端截图链路。
- 替代方案：把捕获字节按 claude.exe 的深色主题色值**重渲染**成 PNG（`ref-claude-tui-*.png`），并在图内注明「非屏幕截图」。这样读者看到的是**原文案 + 原色值**，只有像素化方式不同。
- **一个必须说清的偏差**：捕获文本里偶尔出现 `helloe` / `thirde` / `toke` / `thinkingg` 这种尾巴多一个字符的行。这是 Ink 增量重绘在我的 PTY 回放里留下的残影（同一帧稍早位置出现过正确文本），**不是原件文案**。出图时已挑掉残影帧，但报告里引用原文时按捕获原样保留，并在此声明。
- 截图目录里已有的 `ref-claude-code.png` / `ref-claude-app.png` 是别的代理截的 **Web 端**，不是本次终端取证，本报告不引用。

---

## 2. 消息流排版

### 2.1 只有一列，没有左右分栏

实跑（`ref-claude-06-diff-edit.txt`）：

```
> 用 Edit 工具把 D:/kbg harness/_shots/probe-note.txt 里的 world 改成 Claude

●I'll help you replace "world" with "Claude" in the file D:/kbg
  harness/_shots/probe-note.txt. Let me first read the file to see its current
  content, then make the replacement.

  Read 1 file

●Now I'll use the Edit tool to replace "world" with "Claude" in the file:

● Update(probe-note.txt)
```

拆出来三条规则：

| 元素 | 前缀 | 续行 |
|---|---|---|
| 用户说的话 | `> ` | 无（自己会折行） |
| 助手正文 | `●`（无空格） | **2 空格悬挂缩进** |
| 工具调用 | `● `（有空格） | 参数 2 空格、结果 `␣␣⎿␣␣` |
| 思考块 | `● Thinking for 3s…` | `␣␣⎿␣␣` |

注意 `●I'll help`（紧贴）和 `● Update(`（有空格）不是两条规则：`●` 永远在同一个图标列，工具行后跟一个空格再写名字，正文紧贴文字。原始字节里同一段正文在不同重绘帧中还出现过 `● Now` / `●Now` 两种，那是 Ink 覆盖重绘的产物，不是设计。

【证据】`claude.exe` 内 `Tr=M()==="macos"?"\u23FA":"\u25CF"` —— macOS 用 `⏺`，Windows/Linux 用 `●`。实跑确认 Windows 就是 `●`。

### 2.2 空行节奏：靠 margin，不靠空字符串

```
[logo 3 行]

> 用户消息
（空行）
●助手正文…
（空行）
● 工具行
  ⎿ 结果
（空行）
●下一段正文
```

【证据】`e(s,{marginTop:1,...})`、`marginY:1`、`marginBottom:1` 反复出现；例如压缩提示：
`e(s,{marginY:1,children:r(n,{dimColor:!0,children:[K$,"Conversation compacted (",_A," for history)"]})})`

也就是说：**块间距是 1 行，用 margin 表达，不是插空行**。这样折叠/展开时不会残留空行。

### 2.3 「谁在说话」靠三件事区分，不靠气泡

1. 行首符号（`>` vs `●`）
2. 颜色（用户输入在输入框里是普通色；助手正文默认色；工具行 `dimColor`）
3. 缩进层级（结果比调用缩进 2 空格）

整屏**没有气泡、没有头像、没有圆角卡片**。唯一的框出现在两种地方：审批卡（`─`×80 + `╌`×80）和输入框（上下两条 `─`×80）。

### 2.4 logo 与元信息

```
 ▐▛███▛█   Claude Code v2.1.271
▝▜██████▀  glm-4.5-flash · API Usage Billing
  ▝▝ ▝▝    D:\kbg harness\_shots
```
三行 block art，右侧三行分别对齐：版本 / 模型 · 计费方式 / 工作目录。

【证据】`ref-claude-03-main-repl.txt`

---

## 3. 工具调用呈现

### 3.1 一行的拼装算法（原文函数）

```js
function gr({call:o,state:l,width:a,indent:c}){
  let m,y=null;
  switch(l){
    case"running":    m={text:Tr,dimColor:!0}, y={text:" · running",dimColor:!0};break;
    case"unanswered": m={text:Mdn,dimColor:!0},y={text:" · no result",dimColor:!0};break;
    case"ok":         m={text:Tr,color:"success"};break;
    case"error":      m={text:Tr,color:"error"},y={text:" · error",color:"error"};break
  }
  let p=Math.max(1,a-ne(c)-ne(m.text)-1-(y?ne(y.text):0)),  // 剩余宽度
      M=qe(o.name),
      C=p-ne(M),
      P=C<0 ? Xe(M,p)                                       // 名字都放不下→硬截
            : o.summary&&C>3 ? `${M}(${Xe(qe(o.summary),C-2)})`  // 名字(摘要)
            : M;
  return [{text:c},m,{text:` ${P}`,dimColor:l!=="running"},...y?[y]:[]]
}
```

要点，按优先级：

1. **先扣掉状态后缀的宽度**，再算摘要预算——所以有 `· running` 时摘要会更早被截。
2. 摘要形态固定为 `Name(summary)`，且 `Name` 单独占预算；剩余 `>3` 列才放摘要，否则只留名字。
3. 截断用 `Xe()`（按显示宽度截，中文算 2 列），截断发生在**摘要内部**，括号始终闭合。
4. **运行中整行 dim**，完成后才转正常色。这是一条很轻的「注意力预算」规则：正在跑的东西不该抢眼，跑完的结果才值得看。

【证据】`claude-strings.txt` 二章；`_js_tail.js` 内 `function gr({call:`

### 3.2 块结构：调用 + 参数 + 结果

```js
function Gi({call:o,state:l,width:a,indent:c}){
  let m=[gr({call:o,state:l,width:a,indent:c})];
  if(Un.includes(o.name))return m;          // 某些工具不显示参数
  let y=`${c}  `;                            // 参数缩进 = indent + 2
  fc(m,o.fields,{indent:y,width:a,maxLines:cc});   // cc = 12
  let p=`${y}${ZMr}  `;                      // 结果前缀 = "  ⎿  "
  if(o.result&&o.result.head==="")
    m.push([{text:Xe(`${p}(empty)`,a),...}]);        // 空输出
  else if(o.result)
    Ui(m,o.result,{firstPrefix:p,restPrefix:" ".repeat(ne(p)),maxLines:dc,color:...});
  else if(l==="unanswered")
    m.push([{text:Xe(`${p}(none recorded)`,a),dimColor:!0}]);  // 没结果
}
```

渲染出来就是：

```
● Bash(echo hello)
  command: echo hello
  ⎿  hello
```

- 参数是 **`key: value` 逐行**，不是 JSON dump（`fc` 函数按 `P.key` 拼 `A=`${a}${key}: ``）。
- 键名超过半屏宽度 → 键名独占一行，值另起。
- 结果 `firstPrefix` = `  ⎿  `，`restPrefix` = 等宽空格 → **续行严格对齐文字列**。
- 空结果 `(empty)`、无结果 `(none recorded)`，都是 dim。

【证据】`claude-strings.txt` 二章 `function Gi({call:`；实跑 `ref-claude-05-permission-deny.txt`

### 3.3 连续调用怎么合并

- 同一轮里**多次同类调用不合并**，逐条列。
- 但**收尾会给一行统计**代替逐条复述：`Thought for 4s, ran 1 shell command`、`Read 1 file`。
- MCP 类调用在默认视图里合并成一行 `Called slack 3 times`（文档确认），细节留给 `Ctrl+O`。

【证据】实跑：`Thought for 4s, ran 1 shell command` / `Read 1 file`；文档 `interactive-mode`。

### 3.4 展开/折叠

- 默认**不折叠**单条工具结果，只在超过 `dc=6` 行时折叠尾巴。
- 全局展开键是 `Ctrl+O`（Transcript Viewer），提示文案 `(ctrl+o to expand)`。
- 鼠标场景另有一套：`Click to expand collapsed tool results`。

【证据】`(ctrl+o to expand)`、`(ctrl+o)`、`Click to expand collapsed tool results`

---

## 4. diff 画法

### 4.1 工具结果里的 diff（实跑原样）

```
● Update(probe-note.txt)
  ⎿  Added 1 line, removed 1 line
      1  hello
      2 -world
      2 +Claude
      3  third
```

逐列量（0 基下标）：

| 列 | 内容 |
|---|---|
| 0–1 | 2 空格 |
| 2 | `⎿` |
| 3–4 | 2 空格 |
| 5 | 摘要 `Added 1 line, removed 1 line` 起 |
| 0–5 | diff 行是 6 空格 |
| 6 | 行号（右对齐，宽度按最大行号） |
| 7 | 空格 |
| 8 | 标记列：空格 / `-` / `+` |
| 9 起 | 正文 |

也就是说：**diff 的行号起点比摘要文字再右 1 列**，形成「摘要 → 代码块」的次级缩进；行号列宽固定为 1 列（本例只有个位数），十行以上会自然加宽。

四条硬规则：

1. **没有 `@@` 头、没有 `---/+++`、没有 `diff --git`**。上下文直接给，用户不需要读 patch 元数据。
2. **未改动的行也带行号**，标记位是空格——所以视觉上是一列「有编号的代码」，不是「一团红绿」。
3. **删除行和新增行各自独立编号**（这里都是 `2`）。同号并列，读得出「第 2 行被替换」。
4. **摘要行先给数量**：`Added 1 line, removed 1 line`（单复数分开，`1 line` / `2 lines`）。

【证据】`ref-claude-06-diff-edit.txt`（完整原文含审批视图）、`ref-claude-tui-diff.png`（渲染图）；`Added ${...} line, removed ${...} line` 模板。

### 4.2 审批视图里的 diff

```
────────────────────────────────────────────────────────────────────────────────
 Edit file
 probe-note.txt
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 1  hello
 2 -world
 2 +Claude
 3  third
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
```

- 头部是**两行**：操作名（`Create file` / `Edit file`）+ 文件名，**不是** `⏺ Edit(path)`。
- 代码块用 `╌`（U+254C）铺满宽度做上下边界，中间不画左右竖线。
- 缩进收窄到 5 列（`␣1␣␣hello`），因为此时是「弹窗」，不是「行内」。

### 4.3 超长 diff 怎么收

三档阈值（原文常量）：

```js
var He=12, Qu=60, zt=He*Qu, Zt=4;   // 内联卡片：最多 12 行 / 60 列 / 720 格
var mr=3,  fr=100, cc=12, dc=6;     // cc=参数 12 行，dc=结果 6 行
```

内联卡片超 12 行时（`Ju` 函数）：

```js
let Lr = rows.length<=He;
let Uo = Lr?values:values.slice(0,He-2);          // 值优先保留
let Ir = Lr?lines :lines.slice(0,He-1-Uo.length); // 行其次
let qt = rows.length-Ir.length-Uo.length;
if(qt>0) → ["… (+",qt," more lines)"]
```

即：**先保参数/值，再保上下文行，最后才截行；尾巴单独一行 dim**。

---

## 5. 审批 / 权限确认

### 5.1 问句是模板，不是固定句

| 场景 | 原文 |
|---|---|
| 通用 | `Do you want to proceed?` |
| 新建文件 | `Do you want to create probe-note.txt?` |
| 编辑文件 | `Do you want to make this edit to probe-note.txt?` |
| 通用模板 | `` Do you want to ${verbPhrase} ${bold(fileName)}? `` |
| 抓网页 | `Do you want to allow Claude to fetch this content?` |
| 连接 | `Do you want to allow this connection?` |
| API Key | `Do you want to use this API key?` |

动词随工具变（create / make this edit / proceed），**问句里永远带具体对象名**，不出现「是否继续」这种无宾语句。

### 5.2 选项顺序与默认项

原文构造：

```js
Hjo = {label:"Allow", value:"allow"};                        // 第 1 项
if(domain) ZOt.push({label:XD.node, value:"allow-domain"});  // 第 2 项（仅域名场景）
ZOt.push({label:["Deny ", bold("(esc)")], value:"deny"});    // 最后一项，esc 写进标签
```

实跑（80 列）：

```
 Do you want to create probe-note.txt?
 > 1. Yes
   2. Yes, and switch to accept edits (auto-approve file edits and common file t
      commands) for this session (shift+tab)
   3. No

 Esc to cancel · Tabto amend
```

- `>` 标当前项，默认在**第 1 项**（回车=允许）。
- 每个选项带**编号 + 长描述**，长描述折行时与文字列对齐（不是顶格）。
- 「以后别再问」不是开关，而是一个**带范围的选项**：`Yes, and don't ask again for <rule>` / `... in <dir>`。
- 数字键 1/2/3 直接选（源码 `press 1-${n} or type your answer`）。

**但「信任目录」的默认项相反**：

```
 > No, exit
   Yes, I trust this folder

 Enter to confirm ·Esc to cancel
```

【证据】`ref-claude-tui-permission.png`（整卡渲染）、`ref-claude-05-permission-deny.txt`（原始文本），另见 `ref-claude-02-trust-dialog.txt`。

结论：**默认项的选法不是「安全优先」也不是「效率优先」，而是「这个动作是不是用户刚刚要求的」**。用户主动要求写文件 → 默认允许；系统要访问一个用户没提到的目录 → 默认拒绝。

### 5.3 键盘提示

```
Esc to cancel
Esc to cancel · Tab to amend
Enter to confirm ·Esc to cancel
↑/↓ to navigate · Enter to select · ←/→ to switch · Esc to cancel
Enter to approve · r to retry · ↑/↓ to navigate · Esc to cancel
```

【证据】`claude-strings.txt` 五章。注意实跑里 `Tabto amend`、`·Esc` 都是**缺空格的真实缺陷**——说明这行是拼接出来的，没有统一的分隔符格式器。

### 5.4 被拒绝后怎么呈现

```
● Write(probe-note.txt)
  ⎿  User rejected write to probe-note.txt
      1 hello
✻ Worked for 4s · done 10:55
```

- 不弹错误框、不标红叉、不中断整轮。
- 把 `⎿` 那一行的文字换掉（`User rejected write to X`），**并保留原内容预览**（`1 hello`），让用户确认「拒绝的就是这个」。
- 拒绝后模型继续说话（实跑里它接着解释会怎么做）。

【证据】`ref-claude-05-permission-deny.txt`；字符串 `User rejected write to `

---

## 6. 状态行

### 6.1 Spinner 有 188 个动词，且换时态

池子节选（完整 188 个见 `claude-strings.txt`）：

```
Accomplishing, Actioning, Actualizing, Architecting, Baking, Beaming,
Beboppin', Befuddling, Billowing, ... Cooking, Crafting, ... Sautéing,
Scampering, ... Thinking, Thundering, Tinkering, Tomfoolering, ... Zigzagging
```

三态：

| 阶段 | 形态 | 实例 |
|---|---|---|
| 进行中 | `✻ <动词>…` | `✻ Baking…` |
| 进行中 + 计时/计数 | `✻ <动词>…(3s · ↓ 2 tokens · thinking)` | |
| 收尾 | `✻ <动词过去式> for 4s · done 10:54` | `✻ Sautéed for 4s · done 10:54` |

星形帧 6 帧往复：`· ✢ ✳ ✶ ✻ ✻`（反向再走一遍），另有色盲安全的一套 `· ✢ ✳ ✶ ✻ ✽`（`_t`）。

### 6.2 状态串的信息密度

```
✻ Baking…(3s · ↓ 2 tokens · thinking)
✻ Sketching…(4s · ↓ 995 tokens · thought for 2s)
✻ Undulating…  (9s · 163 tokens)
```

拆开是四段，**能省则省**：

1. 耗时（必给）
2. `↓ N tokens`（有输出才给）
3. `thinking` / `thought for Ns`（思考过才给）
4. 出错时才附加 `· error` / `· will retry in 30s · check your network`

【证据】` \xB7 next try in ${He} \xB7 attempt ${X.attempt} \xB7 esc to interrupt`；`"Waiting for API response"` + `" \xB7 will retry in "` + `" \xB7 check your network"`。

### 6.3 底部条：三段式 + 宽度降级

```
⏸ manual mode on · ? for shortcuts · ← for agents
⏸ manual mode on ·esc to interrupt · ← for agents     ← 运行中换成中断提示
```

- 左：模式（`⏸ manual mode on`）
- 中：随状态切换的一句话（`? for shortcuts` ↔ `esc to interrupt`）
- 右：`← for agents`

【证据】`ref-claude-tui-tool.png` 底部三行；`ref-claude-03-main-repl.txt` 里 `? for shortcuts` ↔ `esc to interrupt` 的切换。

降级逻辑（原文）：

```js
Eeo = `${N.arrowUp}${N.arrowDown} scroll · v to ${Meo} · ? for shortcuts`;
qvn = 宽度([...].join(" · ")) < columns ? Eeo : "? for shortcuts";
```

**先测宽，放不下就整句退化成最短的那句**，不硬折行、不留半截。

### 6.4 右上一格

```
                                                               ◈ max · /effort
```

- `◈` + 当前 effort 档 + 可执行的命令名（点进去就是设置）。
- 右侧对齐，且**只在这一屏有输入框时才出现**。

### 6.5 todo 清单占位

- 位置：输入框**下方**（状态区），不占正文。
- 由 `Ctrl+T` 显隐。
- 折叠 3 条（`mr=3`），展开上限 100 条（`fr=100`）；文档（2.1.287+）写的是 5 条——**版本差异，别照抄文档**。
- 表头 `Tasks · 2/5 done`；折叠尾巴按状态分组：`… +2 in progress, 3 pending`。

---

## 7. 长输出折叠

四套模板，各有明确适用面，**不要混用**：

| 场景 | 模板 | 原文 |
|---|---|---|
| 工具输出超行 | `… 12 more lines` | `` `\u2026 ${c} more ${x(c,"line")}` `` |
| 只超字符不超行 | `… +340 chars` | `` `\u2026 +${m} chars` `` |
| 内联卡片 / 审批卡 | `… (+3 more lines)` | `` ["\u2026 (+",qt," more lines)"] `` |
| 任务折叠 | `… +2 in progress, 3 pending` | `` `${c}\u2026 +${w}` `` |

阈值（写死的常量）：

```js
var mr=3, fr=100, cc=12, dc=6;        // 任务 3/100，参数 12 行，结果 6 行
var He=12, Qu=60, zt=He*Qu, Zt=4;     // 内联卡 12 行 / 60 列
```

两条可抄的细节：

1. **行优先、字符兜底**：先按行数折，行数没超但字符超了才用 `+N chars`。因为「少 5 行」和「少 300 字」读者的心理预期不一样。
2. **折叠行是独立一行、整体 dim、带缩进**（`o.push([{text:Xe(`${l}${y}`,a),dimColor:!0}])`），不接在最后一行内容后面。

---

## 8. 颜色与字形

### 8.1 语义键 → 主题解析，从不直接写 green/red

主题表（深色，实跑用的就是这套）：

```
claude      rgb(215,119,87)     #D77757  Claude 橙
success     rgb(78,186,101)
error       rgb(255,107,128)
warning     rgb(255,193,7)
permission  rgb(177,185,249)    蓝紫
subtle      rgb(80,80,80)
inactive    rgb(153,153,153)
text        rgb(255,255,255)
diffAdded       rgb(34,92,43)   ← 暗绿「底」
diffRemoved     rgb(122,41,54)  ← 暗红「底」
diffAddedWord   rgb(56,166,96)  ← 亮绿「字」
diffRemovedWord rgb(179,89,107)
```

浅色主题同名键换了值（`diffAdded rgb(105,219,124)`），但**键名一个没变**——所以换肤只是换表，不改组件。

背景：`{light:"#f9f9f7", dark:"#1f1f1e"}`。

### 8.2 是否依赖 256 色：不依赖

onboarding 第一屏就让你选主题，7 个选项：

```
 1. Auto (match terminal)
 2. Dark mode
 3. Light mode
 4. Dark mode (colorblind-friendly)
 5. Light mode (colorblind-friendly)
 6. Dark mode (ANSI colors only)
 7. Light mode (ANSI colors only)
```

源码里对应两套解析：

```js
if(e.startsWith("ansi:")){let s=be.get(e.slice(5)); ...}   // ansi:cyan 这类走 16 色表
```

以及第三套 colorblind（`diffAdded rgb(153,204,255)`，偏蓝）。**结论：主用 24bit RGB，但每个颜色都有 ANSI-16 与色盲安全两张兜底表。**

### 8.3 字形分工

| 字形 | 码点 | 语义 |
|---|---|---|
| `●` / `⏺` | U+25CF / U+23FA | 助手行首、工具行首、运行中（dim） |
| `⎿` | U+23BF | 工具结果、思考块、diff 体 |
| `> ` | — | 用户输入、当前选项 |
| `❯` | U+276F | 列表选中指针 |
| `✻` | U+273B | Claude 品牌星、spinner 主帧、压缩提示行首 |
| `✔` / `✘` | U+2714 / U+2718 | 完成 / 失败 |
| `◼` / `◻` | U+25FC / U+25FB | 进行中 / 待办 |
| `◌` | U+25CC | 排队、未回答 |
| `▸` | U+25B8 | 折叠指示 |
| `─` | U+2500 | 整屏分割线、审批卡上下边 |
| `╌` | U+254C | diff/代码块上下边（虚线） |
| `╭╮╰╯` | U+256D… | 圆角框（输入框、弹窗） |

**选择原则**：所有字形都是**单字符宽**（East Asian Width = 1），且都来自 Unicode 的几何/技术符号区，不依赖 Nerd Font。中文界面里能对齐格。

### 8.4 边框：只有两种线，且不用竖线

- `─`×80：全宽分割（审批卡顶/对话分节/输入框上下）
- `╌`×80：内容块上下（diff、代码预览）
- 圆角框 `╭╮╰╯` 只在真正的「容器」上（审批弹窗、输入框）

实跑里输入框只有上下两条横线、**左右没有竖线**：

```
────────────────────────────────────────────────────────────────────────────────
> 
────────────────────────────────────────────────────────────────────────────────
```

【证据】`ref-claude-03-main-repl.txt`；源码 `<Box borderStyle:"round" borderColor:{...} borderDimColor paddingX:1 minHeight:5>`（窄屏/无焦点时边框降为 dim，视觉上就只剩上下线）。

---

## 9. 可落地方案

> 目标文件已通读：`web/src/components/bot/{turn.tsx, steps.tsx, bot-app.tsx, goal-status-bar.tsx}`。
> 排序原则：**投入小收益大 → 投入大**，前 4 条都是纯文案/排版级。

### 9.1 【文案级】把「在想怎么做…」换成会变的动词 + 真实计数

**现状**　`turn.tsx:429-434`

```tsx
{!props.streaming && !props.reply && running && props.stepCount === 0 && (
  <p className="flex items-center gap-2 py-1 text-[13px] text-muted">
    <span className="size-2 animate-pulse rounded-full bg-brand" />
    在想怎么做…
  </p>
)}
```

一句话配一个呼吸点，跑 30 秒和跑 3 分钟长得一模一样；用户判断不了「它卡了」还是「它在想」。

**Claude Code 怎么做**　188 个动词池（`Baking` → `Sautéed`），且**换时态**、**带计时**：

```
✻ Baking…(3s · ↓ 2 tokens · thinking)
✻ Sautéed for 4s · done 10:54
```

关键是它不只换词，还给了「已经花了多久 / 已经产出多少」两个可验证的量。

**我们怎么改**　新建 `web/src/components/bot/turn-thinking.tsx`（或在 turn.tsx 内加小函数）：

```tsx
const VERBS = ["在想", "在核对", "在翻代码", "在理顺", "在拆解", "在比对"];

// 用 createdAt 当种子：同一轮固定一个词，重渲染/刷新都不会跳词
const seed = Math.floor(Date.parse(props.createdAt) / 1000) || 0;
const verb = VERBS[seed % VERBS.length];
```

渲染：

```tsx
<p className="flex items-baseline gap-2 py-1 text-[13px]">
  <span className="text-brand">✻</span>
  <span className="text-text">{verb}…</span>
  <span className="font-mono text-[11px] text-faint tabular-nums">{elapsed}s</span>
  {props.stepCount > 0 && <span className="text-[11px] text-faint">· 已走 {props.stepCount} 步</span>}
</p>
```

`elapsed` 用 `useEffect` + `setInterval(1000)` 累加，卸载时 `clearInterval`。**不要**抄 188 个词——6 个足够覆盖前 6 分钟，再长用户也不看词了。

---

### 9.2 【排版级】依据条：把耗时挪成右对齐的定宽列

**现状**　`turn.tsx:350-377` 一行七段全部 `gap-1.5` 顺排：

```tsx
{evidence.looked > 0 && <span className="shrink-0">看了 {evidence.looked}</span>}
{evidence.files > 0 && <span className="shrink-0">· 改了 {evidence.files} 个文件</span>}
{evidence.commandCount > 0 && <span className="shrink-0">· 跑了 {evidence.commandCount} 条命令</span>}
...
{evidence.durationMs > 0 && <span className="ml-1 shrink-0 font-mono">· {formatDuration(evidence.durationMs)}</span>}
```

问题：数字位数一变（`9 秒` → `1 分 20 秒`），后面的内容整体左右跳；多个轮次上下排列时扫不出规律。

**Claude Code 怎么做**　`zs()` 把状态行切成四段，时间单独一段并 `padStart(6)` 右对齐：

```js
let w = p ? P(p,l).padStart(Math.min(da,l)) : "";   // da = 6
let A = w ? l-ne(w)-1 : l;                          // 给时间列预留宽度
return [{text:v},{text:" · "},{text:K},{text:" ".repeat(j)},{text:w}];
```

**我们怎么改**　`turn.tsx` 依据条外层加 `w-full`，把耗时拆出来：

```tsx
<button className="mb-1 flex w-full max-w-full items-center gap-1.5 rounded-md px-1 py-0.5 ...">
  {/* …计数部分不动… */}
  <span className="ml-auto w-12 shrink-0 text-right font-mono text-[11px] text-faint tabular-nums">
    {evidence.durationMs > 0 ? formatDuration(evidence.durationMs) : ""}
  </span>
</button>
```

`w-12` + `tabular-nums` + `text-right` 三件套，让所有轮次的耗时在同一列对齐。分隔符也统一：现在混用「看了 3」和「· 改了 2」，全改成前缀 `·` 或全去掉。

---

### 9.3 【文案级】空结果 / 无记录，用最短的括号文案

**现状**　`turn.tsx:407`、`turn.tsx:476`

```tsx
{shown.length === 0 && <p className="px-1.5 py-1 text-[12px] text-faint">这一步没有可显示的内容。</p>}
...
<p className="text-[13px] text-faint">这一轮没有留下可显示的内容。</p>
```

两句 12–14 个字，占一整行，而且「可显示的内容」是系统视角（我们没渲染出来），不是用户视角（这一步确实什么都没做）。

**Claude Code 怎么做**　全部压成括号短语，且**贴在 `⎿` 后面**：

```
  ⎿  (empty)
  ⎿  (none recorded)
 · no result
```

**我们怎么改**　两处文案改成 `（无输出）` / `（没有记录）`，并且**从独立 `<p>` 改成挂在工具行尾部**：

```tsx
// steps.tsx ToolStepRow 内，状态点之前
{step.output === "" && step.status !== "running" && (
  <span className="shrink-0 text-[11px] text-faint">· 无输出</span>
)}
```

独立空态 `<p>` 只在「整轮真的一条 step 都没有」时保留，文案改成 `（这一轮没有过程记录）`——把「没有」说清楚，别让用户怀疑是加载失败。

---

### 9.4 【排版级】工具详情：参数别 dump JSON，超行数用统一尾巴

**现状**　`steps.tsx:111-120`

```tsx
{Object.keys(step.args).length > 0 && (
  <pre className="max-h-52 overflow-auto ...">{JSON.stringify(step.args, null, 2)}</pre>
)}
{step.output && <pre className="max-h-72 overflow-auto ...">{step.output}</pre>}
```

两个问题：① `JSON.stringify` 把 `{"command":"echo hello"}` 摊成 4 行带引号带花括号，读起来比原命令还费劲；② 只限高不限行——`max-h-72` 配 `text-[12px] leading-relaxed` 大约能塞 14 行，展开一次滚一屏。

**Claude Code 怎么做**　`key: value` 逐行（`fc()`），且**行数硬上限**：参数 12 行、结果 6 行，超出尾巴一行 `… 12 more lines`。

```js
let A = `${a}${qe(P.key)}: `;      // "  command: "
Ui(o,P,{firstPrefix:A,restPrefix:`${a}  `,maxLines:y,...});
Bi(o,a,c,p,M);                     // → `… ${c} more ${x(c,"line")}`
```

**我们怎么改**　

```tsx
// steps.tsx：参数从 JSON 改成 key: value 列表
<dl className="space-y-0.5 font-mono text-[12px] leading-relaxed">
  {Object.entries(step.args).slice(0, 12).map(([key, value]) => (
    <div key={key} className="flex gap-2">
      <dt className="shrink-0 text-faint">{key}:</dt>
      <dd className="min-w-0 break-all text-muted">{String(value)}</dd>
    </div>
  ))}
</dl>
{Object.keys(step.args).length > 12 && (
  <p className="font-mono text-[11px] text-faint">… 还有 {Object.keys(step.args).length - 12} 项</p>
)}
```

输出块按行切：

```tsx
const lines = step.output.split("\n");
const LIMIT = 6;
...
{lines.slice(0, LIMIT).join("\n")}
{lines.length > LIMIT && (
  <p className="pt-1 font-mono text-[11px] text-faint">… 还有 {lines.length - LIMIT} 行</p>
)}
```

注意两点：阈值用 **6/12**（和 CLI 一致，不是因为好看）；尾巴用 `…` 开头（和其他折叠提示保持同一套符号），不要用「查看更多」按钮——按钮要点击，提示不需要。

---

### 9.5 【组件级】工具行三态：从「三个圆点」改成「一个字 + 后缀」

**现状**　`steps.tsx:43-58`

```tsx
if (status === "running") return (<span className="relative flex size-2 ...animate-ping..." />);
return <span className={`size-1.5 shrink-0 rounded-full ${status === "failed" ? "bg-danger" : "bg-accent"}`} />;
```

三种状态共用「圆点」这一形状，只靠颜色和动画区分——色觉差异用户、截图、黑白打印都会失效。而且运行中是 `size-2` + ping 动画、结束后缩成 `size-1.5`，行高会跟着跳一下。

**Claude Code 怎么做**　形状+颜色+后缀三重编码：

```js
running  → ● dim      + " · running"
ok       → ● success  + (无后缀)
error    → ● error    + " · error"
队列     → ◌ subtle   + " · no result"
```

成功的行**不加后缀**——这是最省字的一条：只有异常才说话。

**我们怎么改**　`steps.tsx` 的 `StatusDot` 换成 `StatusGlyph`：

```tsx
function StatusGlyph({ status }: { status: "running" | "ok" | "failed" }) {
  const tone =
    status === "running" ? "text-faint" : status === "failed" ? "text-danger" : "text-accent";
  return (
    <span aria-hidden className={`shrink-0 font-mono text-[11px] leading-none ${tone}`}>
      ●
    </span>
  );
}
```

形状固定为 `●`，靠**颜色 + 后缀**区分——这正是 CLI 的做法（`ok` 与 `error` 用的是同一个 `Tr`）。

配套后缀（放在 `step.name` 之后、`hint` 之前）：

```tsx
{step.status === "running" && <span className="shrink-0 text-[11px] text-faint">· 运行中</span>}
{step.status === "failed" && <span className="shrink-0 text-[11px] text-danger">· 失败</span>}
```

如果要保留「圆点」的视觉重量，就用 `●` 一种形状 + 三种颜色 + 后缀，不要在形状上做区分——CLI 自己也是这么做的（`ok` 和 `error` 都是 `Tr`，只有颜色和后缀不同）。

---

### 9.6 【组件级】审批卡：从「按钮组」改成「问句 + 编号选项 + 默认项」

**现状**　`turn.tsx:78-132`

```tsx
<p className="text-[13px] font-medium text-warn">需要你批准：{approval.tool}</p>
<pre>{command}</pre>
<div className="mt-2.5 flex flex-wrap items-center gap-1.5">
  {grants.map((option) => <Button variant={option.scope === "once" ? "primary" : undefined} ...>{option.label}</Button>)}
  <Button variant="danger" ...>拒绝</Button>
</div>
```

四个问题：
1. 标题是**名词短语**（「需要你批准：bash」），不是问句——用户得自己在脑子里翻译成「我要不要让它跑这个」。
2. 选项是**平铺按钮**，没有顺序语义（哪个是默认？哪个是「以后都这样」？）。
3. 「拒绝」用了 `variant="danger"` 红色按钮——红色在这里暗示「危险动作」，但拒绝才是安全动作，颜色语义反了。
4. 没有键盘：`Esc` 不能拒，`1/2/3` 不能选。

**Claude Code 怎么做**　

```
 Do you want to create probe-note.txt?
 > 1. Yes
   2. Yes, and switch to accept edits (...) for this session (shift+tab)
   3. No

 Esc to cancel · Tab to amend
```

- 问句带对象名，动词随操作变；
- `>` 标默认项，编号 1/2/3 可直接按；
- 破坏性最小的是第 1 项（一次），范围最大的是第 2 项（本会话）；
- 拒绝是**普通选项**，不是红色危险按钮；
- 底部一行键位提示。

**我们怎么改**　

```tsx
<p className="text-[13px] text-text">
  {approval.tool === "bash" ? "要让它跑这条命令吗？" : `要让它改 ${approval.args.path ?? "这个文件"} 吗？`}
</p>
<ul className="mt-2 space-y-1" role="listbox" aria-label="审批选项">
  {grants.map((option, index) => (
    <li key={option.scope}>
      <button
        type="button"
        role="option"
        aria-selected={index === 0}
        className={`flex w-full items-start gap-2 rounded-md px-2 py-1.5 text-left text-[13px] transition-colors
          ${index === 0 ? "bg-brand/[0.06] text-text" : "text-muted hover:bg-surface-2 hover:text-text"}`}
      >
        <span className="shrink-0 font-mono text-[11px] text-faint">{index === 0 ? ">" : " "}{index + 1}.</span>
        <span className="min-w-0">{option.label}</span>
      </button>
    </li>
  ))}
  <li>
    <button ... className="...text-muted">
      <span className="font-mono text-[11px] text-faint">  {grants.length + 1}.</span>
      <span>不允许<span className="ml-1 text-faint">（Esc）</span></span>
    </button>
  </li>
</ul>
<p className="mt-2 font-mono text-[11px] text-faint">Esc 取消 · Enter 确认默认项 · 1–{grants.length + 1} 直接选</p>
```

Esc 绑定：

```tsx
useEffect(() => {
  if (busy) return;
  const onKey = (event: KeyboardEvent) => {
    if (event.key === "Escape") { event.preventDefault(); onDecide("deny", "once"); }
    const n = Number(event.key);
    if (n >= 1 && n <= grants.length + 1) {
      event.preventDefault();
      if (n === grants.length + 1) onDecide("deny", "once");
      else onDecide("approve", grants[n - 1].scope);
    }
  };
  window.addEventListener("keydown", onKey);
  return () => window.removeEventListener("keydown", onKey);
}, [busy, grants, onDecide]);
```

按钮文案也要改：`拒绝` → `不允许`（现在这个动词读起来像「我拒绝你」，主语错了）；`option.label` 若是「只这一次」就改成「允许一次」，和 CLI 的 `Yes` 同档。

---

### 9.7 【组件级】计划卡：折叠到 3 条，尾巴按状态分组

**现状**　`turn.tsx:498-535` 全量渲染 `plan.items.map(...)`，三态用 `✓ / ▸ / ○`，还加了删除线和进度条。

已经有 `✓ / ▸ / ○` 的字形意识（这点比大部分产品好），但两个问题：① 计划一长（10 项）就把正文推下去一屏；② 尾巴没有「还有多少」，用户不知道是全部还是截断。

**Claude Code 怎么做**　

```js
var mr=3;
let w=[...in_progress, ...pending, ...completed];      // 排序：进行中优先
M = o.filter(I => new Set(w.slice(0,mr)).has(I));
m.push([{text:`${c}… +${w}`,a},dimColor]);             // … +2 in progress, 3 pending
```

**我们怎么改**　

```tsx
const visible = plan.items.length > 3
  ? [...plan.items].sort((a, b) => rank(a.status) - rank(b.status)).slice(0, 3)
  : plan.items;
const hidden = plan.items.length - visible.length;
```

尾巴：

```tsx
{hidden > 0 && (
  <li className="pl-[18px] font-mono text-[11px] text-faint">
    … 还有 {hidden} 项
    {countBy(plan.items, "in_progress") > 0 && `（${countBy(plan.items, "in_progress")} 项在进行）`}
  </li>
)}
```

排序 `rank`: `in_progress=0, pending=1, completed=2`——把正在做的顶到前面，已完成的沉底，这样折叠掉的永远是「最不重要」的那几条。

---

### 9.8 【大改】diff 渲染：先做「行号 + 标记」，再补上下文，不要做完整 patch

**现状**　`steps.tsx:177-191` 只有一行：

```tsx
<span>{path}</span>
<span className={CHANGE_TONE[changeType]}>{CHANGE_LABEL[changeType]}</span>
```

「改了 `web/src/foo.tsx` 修改」——用户完全不知道改了什么，只能去翻 git。而 `turns.ts` 的 `BotStep` 里**没有 patch 字段**（只有 `output` 和 `args`）。

**Claude Code 怎么做**　

```
  ⎿  Added 1 line, removed 1 line
      1  hello
      2 -world
      2 +Claude
      3  third
```

- 没有 `@@`、没有 `---/+++`；
- 每行 `行号 + 标记 + 内容`，未改行也带行号；
- 深色主题用**暗底**（`diffAdded rgb(34,92,43)`）+ 亮字（`diffAddedWord`），不是亮绿字。

**我们怎么改**　分两步，第一步不依赖后端：

**第一步（纯前端，可行）**：Edit 类工具的 `args` 里通常有 `old_string` / `new_string`，直接生成最小 diff。**注意：这条路只有「改前/改后」两段，没有上下文行**——上下文要另外从 Read 结果或后端 patch 拿，先别硬凑。

```tsx
// steps.tsx
function miniDiff(args: Record<string, unknown>) {
  const before = String(args.old_string ?? "");
  const after = String(args.new_string ?? "");
  if (!before && !after) return null;
  const removed = before.split("\n");
  const added = after.split("\n");
  return { removed, added };
}
```

渲染（对齐 CLI 的列宽）：

```tsx
<div className="mt-1 font-mono text-[12px] leading-[1.5]">
  <p className="text-[11px] text-faint">
    增加 {added.length} 行，删除 {removed.length} 行
  </p>
  {removed.map((line, i) => (
    <div key={`r${i}`} className="flex gap-2">
      <span className="w-8 shrink-0 text-right text-faint">{i + 1}</span>
      <span className="w-3 shrink-0 text-danger">-</span>
      <span className="min-w-0 whitespace-pre-wrap bg-danger/[0.08] text-danger/90">{line}</span>
    </div>
  ))}
  {added.map((line, i) => (
    <div key={`a${i}`} className="flex gap-2">
      <span className="w-8 shrink-0 text-right text-faint">{i + 1}</span>
      <span className="w-3 shrink-0 text-accent">+</span>
      <span className="min-w-0 whitespace-pre-wrap bg-accent/[0.08] text-accent/90">{line}</span>
    </div>
  ))}
</div>
```

三个必须和 CLI 对齐的细节：`w-8 text-right`（行号右对齐）、`w-3`（标记列定宽，保证 `+`/`-`/空格三态不错位）、`bg-*/[0.08]`（**底色而不是亮字**，这是 CLI 深色主题的做法）。

**第二步（需要后端）**：给 `file.changed` 事件补一个 `patch` 或 `unified_diff` 字段，前端只做渲染，不在浏览器里算 diff 算法。**不要**在前端引 diff 库——几万行的文件会卡主线程。

超长收口：套 9.4 的规则，diff 超过 **12 行**折叠，尾巴 `… 还有 N 行`（和 CLI 的 `He=12` 一致）。

---

### 9.9 【小改】状态条：运行中补一句「怎么停」

**现状**　`goal-status-bar.tsx` 一行 chips（目标 + 后台任务），`statusBarVisible` 决定整条是否渲染。运行中没有任何「如何中断」的提示，用户只能去猜输入框旁的方块按钮。

**Claude Code 怎么做**　底部固定三段，运行中把中段换成中断提示：

```
⏸ manual mode on · esc to interrupt · ← for agents
```

**我们怎么改**　`goal-status-bar.tsx` 末尾（`!data.serviceOnline` 那块之前）加：

```tsx
{data.running && (
  <span className="ml-auto shrink-0 font-mono text-[11px] text-faint">Esc 停止</span>
)}
```

前提是 `GoalStatusBarData` 里有 `running`（见 `goal-status.ts:16-27`，现在只有 `goal / goalStatus / backgroundCount / backgroundLabel? / backgroundStatus? / serviceOnline`，需要在 `bot-app.tsx:1027` 的 `statusBarData` 多传一个字段），并在 `bot-app.tsx` 绑定全局 `Escape` 到现有的停止逻辑。

**为什么这条不违反 `goal-status.ts` 开头的原则**：那里写的是「场景、模型、权限档不在这里出现——它们本来就在输入框工具条上各有一个可点的选择器」。那些是**设置项**；而「现在可以按 Esc 停」是**状态**，和「等你批准」同一类——都属于「这一轮现在处于什么处境」。CLI 也是把它放在状态条而不是设置区。

**这条收益不在省一次点击，而在告诉用户「随时可以停」——这是敢不敢让它跑长任务的心理前提。**

---

## 附：证据索引

| 文件 | 内容 |
|---|---|
| `D:/kbg harness/_shots/claude-strings.txt` | 按 9 个主题归类的原文片段（含函数名、常量名） |
| `ref-claude-tui-permission.png` | Write 审批卡全貌（`─`/`╌` 两种线、编号选项、`>` 默认项、footer） |
| `ref-claude-tui-diff.png` | Edit 卡的 diff 区（行号列、`+/-` 标记列、暗底配色） |
| `ref-claude-tui-tool.png` | 收尾统计 `Thought for 4s, ran 1 shell command` + 底部三段式状态条 |
| `ref-claude-01-theme-onboarding.txt` | 主题选择页 + diff 预览卡（`╌` 边框、行号、`+/-`） |
| `ref-claude-02-trust-dialog.txt` | 信任目录弹窗（`> No, exit` 默认拒绝） |
| `ref-claude-03-main-repl.txt` | 主界面：logo / 输入框 / 底部条 / spinner 帧序列 |
| `ref-claude-04-tool-call-bash.txt` | Bash 工具调用、`⎿` 结果、思考块、收尾统计 |
| `ref-claude-05-permission-deny.txt` | 完整审批卡 + 拒绝后的呈现 |
| `ref-claude-06-diff-edit.txt` | diff 审批视图 + diff 结果（行号/标记/上下文） |
| `ref-claude-0*.ansi.bin` | 原始 ANSI 字节（可复现、可重渲染） |
