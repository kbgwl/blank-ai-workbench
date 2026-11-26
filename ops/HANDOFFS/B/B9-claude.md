# B9 开发交接单

## 任务与范围

- 目标：模型完全不调用 browser_check 时，验证节点自己启动工作区服务做最小
  页面冒烟，避免“页面能开但没自检”直接判死。
- 已实现：
  - `ui_smoke.py` 解析 `scripts.start = node <file>`，随机回环端口启动；
  - `browser_probe.mjs` 打开 + reload + 正文非空/无 undefined + 控制台零错误；
  - UI 闸门在**完全没有 browser_check 回执**时自动兜底；
  - 事件 `ui.check_auto_passed`；
  - 测试覆盖自动冒烟通过。
- 明确未实现：
  - 模型跑了 browser_check 但断言失败时，自动冒烟不覆盖（有意为之）；
  - 不支持 npm/shell 启动或非 Node 项目；
  - 未跑完整后端 5000+；B9 相关 60 条通过；
  - 没有 Claude/Codex 对照。

## 修改内容

- `services/api/src/blank_api/services/agent/ui_smoke.py`（新增）：
  `_start_script`、`auto_ui_check`、服务启动/HTTP 就绪/探针/清理。
- `services/api/src/blank_api/services/agent/graph.py`：
  UI 闸门在 `not browser_rows` 时调用自动冒烟；通过则记
  `ui.check_auto_passed` 并保留 `ui_check.auto=true`。
- `services/api/tests/test_ui_delivery_gate_v492.py`：
  新增自动冒烟兜底测试。
- 文档：`docs/b9-ui-smoke-2026-11-22.md`。

## 验证结果

| 检查 | 结果 | 摘要 |
|---|---|---|
| B9 相关后端回归 | **60 passed** | UI 闸门/工具预算/过渡/提示哈希 |
| 直接自动冒烟 | **passed** | B7 工作区：1 assert / 2 steps / 0 console |
| 完整 B7 从头复跑 | **failed** | 25 步 / 32 调用 / 1.709M token；模型 browser_check 缺 wait，断言 false；自动冒烟按设计不覆盖 |

## 风险与需要审查的点

- 自动冒烟只证明“页面能打开、正文非空、控制台无错”，不证明交互正确；
- 模型跑了但断言失败时仍会阻塞交付；本次 B7 的失败是 browser_check 步骤没有
  `wait`，属于检查脚本时序问题；
- 启动 `node <file>` 仍是执行工作区代码；已限制为 package.json start 且
  只用随机回环端口，但安全审查仍需确认；
- 服务可能打印大量日志/占用内存；已捕获 stderr、超时清理；
- 没有跑完整后端回归。

## 建议人工验收

1. 对 B7 工作区直接跑自动冒烟，确认 passed 且服务被清理。
2. 看 `data/b7-evidence.json` 中模型 browser_check 的步骤，确认失败是缺少
   wait，不是页面缺陷。
3. 下一步：给 browser_check 的 click/type 后加 wait 提示，或自动等待；
   再从头跑完整 B7。
