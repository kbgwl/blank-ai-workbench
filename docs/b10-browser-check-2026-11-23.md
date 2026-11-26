# B10 browser_check 动作可靠化（2026-11-23）

> 背景：B9 复跑里模型已经会调用 browser_check，但检查脚本本身有三处假失败：
> 点击长页面按钮没先滚进视口、点击/输入后没等异步渲染、steps 的
> `assert {selector,min}` 只读了 expr。B10 修的是**检查工具本身**。

## 一、改动

- `services/api/scripts/browser_probe.mjs`：
  - `click/type/press/drag` 后自动等待 `settle`（默认 600ms）；
  - `centerOf` 先 `scrollIntoView({block:'center'})`，再取点击坐标；
  - `assert` 支持 `{selector, min}`，按匹配元素数量判定；只有 expr 时仍按原逻辑。
- `services/api/src/blank_api/services/tools/browser.py`：
  - `browser_check` 增加 `settle_ms`（默认 600），传给探针；
  - schema 说明动作后自动等待；长请求仍可加 `wait`。
- `services/api/src/blank_api/services/tools/registry.py`：
  - 工具说明预算从 22,500 调到 22,700，理由是本轮 `settle_ms` 约 +100 字符，
    仍低于收敛前的 25,355。
- 单测：工具预算通过；UI 闸门/过渡/提示哈希保持通过。

## 二、验证

### 2.1 模型同款步骤（无 wait）

用 B7 模型之前失败的那组步骤（点击 AI 分析、输入、发送，然后立即断言）：

```json
{"ok":true,"asserts":5,"steps":7,"console":0}
```

5 个 expect、7 个动作全部通过；这组步骤在 B10 之前会因滚动/等待/selector
断言三处问题误报失败。

### 2.2 完整 B7 从头复跑（成功）

任务 `c328c027-1e79-4d4e-aef3-6605b1ef4719`：

| 项 | 结果 |
|---|---|
| 状态 | **succeeded** |
| 步数 / 调用 | 28 / 32 |
| token | **1,757,693** |
| 文件 | 16 次变更 |
| 测试 | `npm test` **17 passed** |
| browser_check | 2 次，页面/控制台检查通过 |
| 验收 | 3/3 `covered=true` |
| 耗时 | 约 4.6 分钟 |

这是 B7 真实多文件任务第一次在完整链路里 **succeeded**。

## 三、结论

1. B10 之前，browser_check 的失败不全是页面问题，很多是工具自己的假失败；
2. 修完滚动/等待/selector 断言后，模型同款步骤可直接通过；
3. 完整 B7 从零搭建 + `npm test` 17 项 + browser_check + 验收 3/3 成功；
4. 成本仍高（1.76M token），大任务成本是下一阶段问题，不影响本次交付。

## 四、边界

- 最终页面的独立通用探测里，点击 AI 巡检后结果未在 600ms 内更新（任务自己的
  browser_check 已通过页面/主交互，但 AI 巡检的独立复核仍需单独做）；
- 自动 settle 默认 600ms，慢接口仍需显式 `wait`；
- 只验证 Node 服务 + Chromium；没有 Claude/Codex 对照；
- 完整后端 5000+ 未在 B10 后重跑，合入前需补。
