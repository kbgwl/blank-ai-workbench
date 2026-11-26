# B3a 开发交接单

## 任务与范围

- 已实现：
  - 产物视图搜索（按路径，大小写不敏感）。
  - 按类型分组：页面 / 代码 / 数据 / 图片 / 文档 / 其他；空组不出现，顺序固定。
  - 每张产物卡片可「复制路径」，复制后 1.6 秒提示。
  - 导出清单：Markdown（按组列出）与 JSON（路径 + 变更类型）。
- 明确未实现：
  - B3b 单文件内嵌预览（HTML sandbox iframe、Markdown 渲染、JSON/CSV 视图）。
  - B3c 多文件应用预览（安全启动工作区服务、端口与生命周期、审批）。
  - 产物排序偏好持久化与按任务/时间筛选。

## 修改内容

- `web/src/components/bot/artifacts.ts`（新增）：扩展名分类、搜索过滤、分组、导出。
- `web/src/components/bot/artifacts.test.ts`（新增）：5 条判据。
- `web/src/components/bot/panel.tsx`：产物视图接入搜索、分组、复制路径、导出。

## 验证结果

| 命令 | 结果 | 摘要 |
|---|---|---|
| `node --test ... "src/**/*.test.ts" scripts/csp.test.mjs` | **452 passed / 0 failed** | 原 447 + 新增 5 |
| `tsc --noEmit` | 退出码 0 | 无输出 |
| `eslint src --max-warnings=0` | 退出码 0 | 无输出 |
| `scripts/acceptance.py --journey acceptance/kanban.json` | 退出码 0 | 结论：通过：打开有东西，动作有变化，刷新还在 |

## 风险与需要审查的点

- 导出只含路径与变更类型，不含 diff；diff 仍以文件与证据包为准。
- 复制路径依赖 `navigator.clipboard`；非安全上下文（非 localhost/https）会静默失败，页面不报错。
- 分类按扩展名，无扩展名文件进「其他」；不读文件内容判断类型。
- 产物定义沿用现有口径：`changeType === "created"` 或 `diff` 以 `diff --git` 开头。
- 未跑完整后端；本卡未改后端。

## 建议人工验收

1. 打开一个有改动的任务，切到「产物」Tab。
2. 搜索一个路径片段，确认计数与分组同步收窄；清空恢复。
3. 点「复制路径」，粘贴确认是完整工作区相对路径。
4. 点「导出 MD」「JSON」，确认下载文件内容与页面分组一致。
5. 切到无产物任务，确认空状态仍是「还没有产物」。
