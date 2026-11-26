# E6 国内模型默认与包镜像 开发交接单

## 任务与范围
- 已实现：
  - 默认供应商从「名字排序的第一个」改为设置里显式锁定；本机已切到 **DeepSeek**
    （`settings.model.default_provider = deepseek 的 id`）。
  - 供应商列表新增 `is_default`：前端「默认模型」显示的是后端真正会用的那一家，
    不再拿列表第一项猜（此前界面写 deepseek-flash、实际可能跑星火）。
  - 子进程默认使用国内包镜像：pip / uv → 清华 PyPI，npm → npmmirror；
    用户显式设置过的镜像不覆盖，`BLANK_DOMESTIC_MIRRORS=0` 可关闭。
  - 编程场景提示：对同事交付的计划书/报告/方案默认 `.docx`（WPS/Office 可直接打开），
    代码仓库内的说明/README 仍用 Markdown。
- 明确未实现：
  - 不自动安装额外依赖；已装的文档库见 E4。
  - 镜像只作用于 Agent 的子进程，不影响应用自身网络请求。

## 修改内容
- `services/api/src/blank_api/services/shell_env.py`：
  - `DOMESTIC_MIRRORS` / `domestic_mirrors_enabled()` / `apply_domestic_mirrors()`；
  - `capture()` 与 `env_for()` 应用镜像默认值；镜像变量加入最小环境 allowlist。
- `services/api/src/blank_api/services/tools/shell.py`：`build_env()` 应用镜像默认值。
- `services/api/src/blank_api/schemas/model.py` / `api/routes/providers.py`：
  `ProviderOut.is_default`，列表按设置标记。
- `web/src/components/tasks/api.ts`：`Provider.is_default`。
- `web/src/components/bot/provider-choice.ts`：`defaultModelLabel()`；
  `bot-app.tsx` 改用它。
- `services/api/src/blank_api/services/agent/scenarios.py`：交付文档格式口径。
- 测试：`test_domestic_mirrors_v414.py`（5）、`test_provider_default_flag_v414.py`（2）、
  `provider-choice.test.ts`（+2）。

## 验证结果
| 命令 / 操作 | 结果 | 摘要 |
|---|---|---|
| `pytest test_provider_default_flag_v414.py test_domestic_mirrors_v414.py test_default_provider_v427.py -q` | 13 passed | 标记、镜像、既有默认逻辑 |
| `pytest test_handoff_v12.py test_effort_batch.py test_scenarios_v153.py -q` | 66 passed | 环境与场景提示 |
| 前端 `node --test ...` | 475 passed / 0 failed | 含默认模型名用例 |
| 前端 tsc / eslint | 退出码 0 | |
| `GET /api/v1/providers` | `deepseek` 的 `is_default=true` | 与设置一致 |
| 浏览器探针 | 模型选择器显示 `deepseek-flash`；console 0 | 界面与事实一致 |
| 子进程环境实测 | `PIP_INDEX_URL` / `UV_INDEX_URL` / `npm_config_registry` 均为境内源 | capture + build_env |

## 风险与需要审查的点
- 默认供应商写在本机 `data/settings.json`；换机器/重装后需重新选择（设置页目前没有
  默认供应商入口，属已知缺口）。
- 镜像默认值只补缺省；如果清华源不可达，用户可设 `PIP_INDEX_URL` 覆盖或
  `BLANK_DOMESTIC_MIRRORS=0` 关闭。
- 交付文档默认 `.docx` 只影响办公类文档；代码仓库说明仍是 Markdown。

## 建议人工验收
1. 新开一轮任务，确认右下角模型显示 `deepseek-flash`，且任务记录里的 provider 是 DeepSeek。
2. 让 Agent 跑一条 `python -m pip config list` 或装一个小包，确认走境内源。
3. 让它写一份「给同事看的方案」，确认默认产出 `.docx`。
