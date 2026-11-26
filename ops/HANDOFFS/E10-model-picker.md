# E10 模型选择只显示已配置供应商 开发交接单

## 任务与范围
- 已实现：
  - 新增共用判定 `configuredProviders()`：只保留 **启用且填了 API Key** 的供应商。
  - 应用到：聊天输入框模型下拉、全站模型切换器、任务创建、角色模型设置、
    头脑风暴、旧聊天首页、bot 恢复已存供应商、任务页左下角默认模型展示。
  - 未配置的供应商不再以禁用项出现在下拉里；一家都没配时显示「还没有配置模型」。
  - 设置页「模型」供应商配置列表保持全部显示——那里是用来补 Key 的地方。
- 明确未实现：
  - 没有给 Ollama 这类「不需要 Key 的本地供应商」单开例外；当前仍按“有 Key”判定。
  - 没有改后端供应商接口或配置结构。

## 修改内容
- `web/src/components/configured-providers.ts`：新增共用过滤与类型。
- `web/src/components/configured-providers.test.ts`：3 条判定用例。
- `web/src/components/model.tsx`：去掉“没 Key 时退回全部启用项”的兜底。
- `web/src/components/bot/composer.tsx`：删除未配置供应商的禁用选项。
- `web/src/components/bot/provider-choice.ts`：默认模型/默认供应商复用共用过滤。
- `web/src/components/bot/bot-app.tsx`：恢复已存供应商时只认已配置项。
- `web/src/components/tasks/task-list-pane.tsx`：任务创建下拉只列已配置项；
  旧的未配置选择提交时退回「跟随默认供应商」。
- `web/src/components/settings/role-model-settings.tsx`：角色模型只列已配置项。
- `web/src/app/brainstorm/page.tsx`：模型下拉只列已配置项。
- `web/src/components/chat/chat-home.tsx`：默认供应商只从已配置项里挑。
- `web/src/components/workbench/workbench.tsx`：左下角模型展示不再回落到未配置项。
- `web/src/components/tasks/transition-copy.ts`：登记后端新增的 `plan_incomplete` 跳转码。

## 验证结果
| 命令 / 操作 | 结果 | 摘要 |
|---|---|---|
| `pnpm -C web test` | **484 passed / 0 failed** | 含新增过滤与默认选择用例 |
| `pnpm -C web exec tsc --noEmit` | 退出码 0 | |
| `pnpm -C web lint` | 退出码 0 | |
| 浏览器探针（3000，模型下拉） | 3/3 断言通过，console 0 | 菜单只有默认模型 + DeepSeek + 讯飞星火；未配置的 anthropic/openai 等不出现 |

截图：`data/model-filter-only-configured.png`。

## 风险与需要审查的点
- 过滤口径是 `enabled && api_key_hint != ""`：Ollama 等本地供应商若没有 Key 也会被隐藏；
  如果以后要支持“无 Key 本地模型”，需要后端给出 `requires_key` 之类的字段，再改这一处。
- 设置页的供应商配置列表仍显示全部，这是有意保留的入口；不要在设置页也套这个过滤。

## 建议人工验收
1. 打开新对话，点输入框里的模型下拉：只应看到已填 Key 的供应商（当前是 DeepSeek 和讯飞星火）。
2. 到设置页清掉某个供应商的 Key，回到新对话：该供应商应从下拉里消失。
3. 进入设置 → 模型：供应商配置列表仍能看到全部（方便补 Key）。
