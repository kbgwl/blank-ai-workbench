# 测试目录

## 结构约定

```
tests/
  unit/         → 纯函数、工具函数（lib/ 无 DOM 依赖的逻辑）
  integration/  → 跨组件交互、Router/Context 等含框架的测试
  e2e/          → Playwright / Puppeteer 端到端场景
```

## 现有 colocated 测试（不需要迁移）

以下 20 个 `.test.ts` 与被测模块放在一起，继续在原地维护：

- `src/lib/shortcuts.test.ts` — 快捷键解析
- `src/lib/http.test.ts` — HTTP 客户端
- `src/components/bot/markdown.test.ts` — Markdown 渲染
- `src/components/bot/image-policy.test.ts` — 图片策略
- `src/components/bot/picker.test.ts` — 文件选择器
- `src/components/bot/turns.test.ts` — 对话轮次
- `src/components/tasks/governance.test.ts` — 合规检查
- `src/components/tasks/verdict.test.ts` — 裁决逻辑
- `src/components/tasks/parallel.test.ts` — 并行执行
- `src/components/tasks/attribution.test.ts` — 归因
- `src/components/tasks/world.test.ts` — 世界状态
- `src/components/tasks/window.test.ts` — 窗口管理
- `src/components/tasks/usage.test.ts` — 用量统计
- `src/components/tasks/question-options.test.ts` — 问题选项
- `src/components/tasks/approval-options.test.ts` — 审批选项
- `src/components/tasks/boundaries.test.ts` — 边界条件
- `src/components/tasks/timeline.test.ts` — 时间线
- `src/components/team/format.test.ts` — 团队格式化
- `src/components/settings/api.test.ts` — 设置 API
- `src/components/reverse-lab/api.test.ts` — 反向实验室 API

## 本文档不参与构建

此目录和本文档仅用于开发约定参考，不影响 CI/CD。