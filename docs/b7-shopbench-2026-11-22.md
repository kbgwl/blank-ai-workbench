# B7 真实多文件任务复跑：shopbench LR10D（2026-11-22）

> 目的：不再用小任务推断大任务能力。用原 shopbench 商品资料任务，在干净工作区
> 从零搭一个可运行的 Web 运营工作台，记录成功/失败、步数、介入、token、文件、
> 测试和浏览器证据。
>
> 这一轮**不是**产品功能开发，不修改主仓库代码；只取证和定位失败点。

## 一、任务与方法

- 任务：基于附件 `LR10D非接触物位计V6721.pdf`，搭建可运行的 Web 工作台：
  展示商品信息与实时状态，支持接入 AI 大模型自动运营；页面能打开、有启动方式、
  代码完整；不要修改工作区之外的文件。
- 工作区：`_bench_workspaces/b7-shopbench-ours`（仓库外、干净目录）。
- 供应商/模型：DeepSeek `deepseek-flash`；权限 `bypass`，自动放行高风险，
  人工审批 0 次（这是自动化跑法，不等于正常审批体验）。
- 任务 ID：`0f0e423f-1afe-47a1-8367-3c61e6ef6c1e`
- 原始证据：`data/b7-evidence.json`、`data/b7-evidence-early.json`

## 二、第一轮 0 改动（已修复，也是 B7 的第一个发现）

第一次运行任务 `96c29b76-9966-482b-a9f4-0aa55a19f451`：

- **succeeded / 2 步 / 4 次调用 / 103,978 token / 0 文件改动**；
- 三条验收全是 `covered=false`，交付文本是“工作区为空，只做调研”；
- 根因不是模型不会做，而是任务末尾的
  **“不要修改工作区之外的文件”** 被 `_NO_WRITE` 当成“不要修改任何文件”，
  整轮锁成只读（`validation.skipped_reason=readonly`）。

修复：`directives.py` 增加 `_OUT_OF_SCOPE`，先移除“不要修改工作区之外”这类
**作用域约束**，再判 `_NO_WRITE`；新增回归测试“范围约束不是只读”。
修复后 `test_directives_v496.py` **13 passed**，重启 8000，重新跑第二轮。

## 三、结果：失败，但不是“不会写”

| 项 | 数值 |
|---|---|
| 任务状态 | **failed** |
| 失败原因 | `测试未通过：ui_check` |
| 步数 | 26 / 200 |
| 模型调用 | 29 次 |
| token | **1,741,293**（prompt 1,687,905 / completion 53,388 / cached 1,632,256） |
| 人工审批 | 0（bypass 自动化） |
| 文件变更 | 18 次事件 / 12 个不同文件 |
| 测试 | `node --test test/api.test.js` **9 pass / 0 fail** |
| 验收覆盖 | 3/3 条 `covered=true` |
| 后台预算 | 1.5M token，实际 1.74M；`budget.exceeded` |

交付物包括：`package.json`、`server.js`、`lib/product.js`、`lib/device.js`、
`lib/ai.js`、`lib/ai-tools.js`、`lib/ai-rules.js`、`lib/http-utils.js`、
`public/index.html`、`public/styles.css`、`public/app.js`、`test/api.test.js`。

## 四、独立浏览器复核

启动：`cd _bench_workspaces/b7-shopbench-ours && $env:PORT=4174; node server.js`

用 `services/api/scripts/browser_probe.mjs` 跑 13 个真实动作/断言：

- 商品信息、技术参数、实时指标渲染正常，页面无 `undefined`；
- 四个运行模式按钮可点击，切到“强制进料”生效；
- 注入 E01 后 DTC/告警列表更新；
- 点击“自动巡检”后，规则引擎返回巡检结果（当前未接大模型）；
- **13/13 断言/动作通过**；
- **唯一控制台错误：`/favicon.ico` 404**。

所以页面本身基本可用；任务失败在**交付前的 UI 自检流程**，不是页面完全打不开。

## 五、失败点

### 4.1 前端改动后没有调用 browser_check

- 工具表里有 `browser_check`（任务启动快照可见），但 25 次工具调用里一次都没用；
- `run_validation` 因此记 `ui.check_failed`：
  “本轮改了前端文件，但没有在最新代码上运行 browser_check”；
- 这是 D2 闸门**按设计拦住**，不是误报。最终交付也自己写了“未跑校验”。

### 4.2 UI 修复被预算立刻截断

时间线顺序：

```text
ui.check_failed
→ repair.started
→ review_result: budget_exhausted（步数预算耗尽，修不动了）
→ summarize_result: budget_exhausted（按现状收尾）
→ task.failed
```

实际 token 1.74M > 1.5M 预算。它没有机会去跑 browser_check 或修复 favicon；
**失败不是“修不了”，是“没预算修”**。

### 4.3 favicon 404 是独立的小闸门问题

即使模型补跑 browser_check，当前生成的页面仍会因为 `/favicon.ico` 404 被
“控制台零错误”判失败。需要：
- 生成页面自带 favicon（或 `data:` favicon）；
- 或在闸门里明确 favicon 404 是否属于可忽略噪声（当前口径是不忽略）。

### 4.4 成本

- 1.74M token / 29 次调用 / 24K-64K 的活跃上下文；
- 没有触发压缩（窗口 1M，水位 82%）；
- 大任务的主要成本仍是**每次调用重发工具说明 + 历史**，不是 completion
  （completion 只有 53K）。
- 这不是 B6 的小任务成本问题；真实多文件任务的预算/上下文策略需要单独设计。

### 4.5 交付里的占位值

最终文本承认有些内容是 `example.org` 占位（主要是 AI 接入默认值）。这不是
核心失败，但说明“可运行”和“可交付”之间还有真实配置差距。

## 六、结论

1. **能力面**：它能在干净目录从零写出 12 文件、9 个测试通过、页面可操作、
   AI 巡检规则引擎可用的工作台；不是“不会做”。
2. **自主面**：它没有在交付前自己打开页面走一遍；D2 闸门正确判失败。
3. **预算面**：1.5M token 不够这个 12 文件任务；修复被 `budget_exhausted`
   直接截断，导致“差一步”的失败。
4. **质量面**：favicon 404 是独立小问题；修完 UI 闸门后应能通过浏览器验收。
5. **指令面（已修）**：第一轮 0 改动是“不要修改工作区之外的文件”被误判成
   只读；已修 `directives.py` 并补回归，第二轮才真正开始搭页面。

## 七、建议的下一张卡

**B8：前端交付闸门从“拦住”变成“能自修”**，优先级高于继续扩大任务集：

1. 前端改动后，若没有 browser_check，验证节点要么**自动跑一次轻量页面探测**，
   要么给模型保留一次**UI 修复预算**（不是简单抬高全局预算）；
2. 生成页面模板补 favicon，避免 `/favicon.ico` 404 触发零错误闸门；
3. 用同一个 B7 任务复跑，断言：页面可开、控制台零错误、browser_check 有回执；
4. 再决定是否提高真实多文件任务的 token 预算，而不是盲目放宽。

## 八、边界

- 只跑了一个任务、一个模型（DeepSeek）、一次 bypass 自动化；没有测正常审批。
- 没有跑 Claude/Codex 同题对照；Codex TUI 可人工审查，但 exec 自动化仍不可靠。
- 生成的 app 保留在 `_bench_workspaces/b7-shopbench-ours` 供查看，未纳入主仓库。
- 本轮修改了产品代码的一处：`directives.py` 的“工作区之外”范围约束误判；
  其余产品代码未改；B7 其余结论是证据与下一步建议。

## 九、B10 后续复跑（2026-11-23）

B8 的预算保留、B9 的自动冒烟、B10 的 browser_check 滚动/等待/selector 断言
修完后，B7 同一任务从头复跑：

- 任务 `c328c027-1e79-4d4e-aef3-6605b1ef4719`：**succeeded**
- 28 步 / 32 调用 / **1,757,693 token**
- 16 次文件变更；`npm test` **17 passed**
- `browser_check` 2 次，页面/控制台检查通过
- 验收 **3/3 `covered=true`**
- 生成的页面独立可用（商品档案、实时状态、参数表、浏览器控制台 0 错误）

所以 B7 的最终结论应更新为：**在 B10 工具修复后，真实多文件任务可以一次
succeeded**；B7 正文保留先前失败分析，用于解释为什么会走到这一步。成本
1.76M 仍是下一阶段问题。
