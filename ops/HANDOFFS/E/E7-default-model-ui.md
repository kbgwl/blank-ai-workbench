# E7 默认模型设置入口 开发交接单

## 任务与范围
- 已实现：
  - 设置页「模型」面板新增「默认模型」选择器：只列启用且填了 Key 的供应商，
    可选项含「自动（有 Key 的第一家）」；修改立即写回 `settings.model.default_provider`。
  - 新增 `PUT /providers/default`：设置/清除默认供应商；未知、停用、没 Key 的一律拒绝。
  - 全站顶部模型切换器改为认 `is_default` 与「有 Key」：不再显示没配 Key 的模板厂商，
    用户存过的失效选择会被重置到默认供应商。
  - 供应商编辑面板首次进入落在默认供应商上，不再默认打开没配 Key 的 anthropic。
- 明确未实现：
  - 不改后端默认解析逻辑（仍以设置项优先、否则有 Key 的第一家）。
  - 不做「角色默认模型」的服务端持久化（RoleModelSettings 仍存 localStorage）。

## 修改内容
- `services/api/src/blank_api/schemas/model.py`：`ProviderDefaultIn`。
- `services/api/src/blank_api/api/routes/providers.py`：`PUT /providers/default`
  （校验存在/启用/有 Key；空串清除）。
- `web/src/components/settings/api.ts`：`Provider.is_default`、`setDefaultProvider()`。
- `web/src/components/settings/providers-view.tsx`：默认模型选择器 + 编辑对象初始落点。
- `web/src/components/bot/provider-choice.ts`：`defaultProviderId()`。
- `web/src/components/model.tsx`：全站模型切换器按默认/有 Key 选择。
- 测试：`test_provider_default_flag_v414.py`（+2）；`provider-choice.test.ts`（+1）。

## 验证结果
| 命令 / 操作 | 结果 | 摘要 |
|---|---|---|
| `pytest tests/test_provider_default_flag_v414.py -q` | 4 passed | 设置/清除/未知/无 Key/停用 |
| 前端 `node --test ...` | 476 passed / 0 failed | 含默认供应商选择 |
| 前端 tsc / eslint | 退出码 0 | |
| `PUT /providers/default` | 200 | 返回 deepseek id |
| 浏览器探针（/settings#models） | 全部断言通过 | 默认模型与顶部切换器都显示 DeepSeek；console 0 |

截图：`data/e7-default-selector3.png`（默认模型行 + 顶部 `deepseek · deepseek-flash`）。

## 风险与需要审查的点
- 默认供应商写在本机 settings 文件，换机需重设（现在有界面入口了）。
- 只允许有 Key 的供应商设为默认；用户若先设默认再删 Key，任务会退回下一家。
- 全站切换器过滤了没 Key 的供应商；一家都没有时仍列出启用项，便于用户去补 Key。

## 建议人工验收
1. 设置 → 模型与供应商：默认模型选「自动」，再选回 DeepSeek，观察列表与顶部切换器同步。
2. 把默认换成另一家有 Key 的供应商，回会话页确认「默认模型」显示跟着变。
