# T-BA 质量验收单
## A. 测试
| 命令 | 结果 | 证据 / 摘要 |
|---|---|---|
| `.venv/Scripts/python.exe -m pytest tests/test_threads.py -q` | 通过 | 28 passed |
| `.venv/Scripts/python.exe -m pytest tests/test_threads.py tests/test_thread_status_v151.py tests/test_thread_rewind_v117.py -q` | **失败** | 1 failed / 41 passed；失败用例 `test_thread_list_filters_by_status_before_limit` |
| `.venv/Scripts/python.exe -m pytest tests/test_threads.py tests/test_thread_status_v151.py -q` | 通过 | 34 passed（去掉 rewind 文件后通过，定位为共享 DB 相互影响） |
| `.venv/Scripts/python.exe -m pytest tests/test_threads.py::test_thread_list_filters_by_status_before_limit -q` | 通过 | 单独复跑通过，组合运行失败为排序不稳定 |
| `node --test batch-archive/turns/nav.test.ts` | 通过 | 65 passed |
| `node --test src/**/*.test.ts` | 通过 | 492 passed / 0 fail |
| `tsc --noEmit` | 通过 | 退出码 0 |

## B. 代码审查
| 严重度 | 文件:行 | 发现 | 影响 | 建议 |
|---|---|---|---|---|
| P0 | `web/src/components/bot/bot-app.tsx`（`archive`）+ `web/src/components/tasks/api.ts`（`archiveThread`） | 单条「归档」调用 `DELETE /threads/{id}`，后端该接口为**物理删除**（删会话+全部轮次任务/事件/检查点，不可恢复）；弹窗却承诺「已有的轮次全部保留」 | 用户归档时历史数据被静默销毁，数据丢失；与批量归档（只改 status、可取消）严重不一致 | `archive` 改用 `updateThread(thread.id, { status: "archived" })`（PATCH），与批量归档同一语义 |
| P1 | `ops/HANDOFFS/T-BA-claude.md`；`services/api/src/blank_api/api/routes/threads.py`（`batch_archive_threads` docstring）；`web/src/components/tasks/api.ts`（`archiveThreads` 注释） | 称「批量归档语义与单条 DELETE /threads/{id} 完全一致」，但 DELETE 是物理删除，批量归档只改 status | 误导验收与后续开发 | 修正为：批量归档与 `PATCH status=archived` 一致；`DELETE /threads/{id}` 是物理删除 |
| P2 | `services/api/tests/test_threads.py`（`test_thread_list_filters_by_status_before_limit`） | 组合运行时失败：两条活跃会话 `updated_at` 相同，`ORDER BY updated_at DESC, created_at DESC` 对相等值排序不稳定 | 交接单「28+14 passed」的组合跑法实际不成立 | 给活跃会话设置不同 `updated_at`，或断言集合而非顺序 |
| P2 | `web/src/components/bot/sidebar.tsx`（已归档组） | 已归档超过 200 条只展示最近 200 条，页面不提示还有更早数据 | 更早的归档会话用户不可见 | 交接单已记录，需产品确认补提示或分页 |

## C. 业务逻辑验收矩阵
| 验收标准 | 证据 | 结论 |
|---|---|---|
| 侧栏「管理」选择态 + 整批一次确认 | `sidebar.tsx`：管理态、复选框、操作条、确认文案、Escape/收起退出 | 通过 |
| `POST /threads/batch-archive` 三桶结果、去重、空/超限 422、幂等 | `threads.py::batch_archive_threads` + `test_threads.py` 用例 | 通过 |
| 列表接口 `status=all|active|archived` 先过滤后 limit | `threads.py::list_threads`/`list_project_threads` + 用例 | 通过（实现正确；排序稳定性测试有缺陷，见 P2） |
| 已归档折叠组 + 取消归档 | `sidebar.tsx` + `updateThread` PATCH（已补 content-type） | 通过 |
| 首页/全站左栏/任务页只取活跃会话 | `app-rail.tsx`、`task-list-pane.tsx` `status=active` | 通过 |
| 单条「归档」保留轮次、可取消归档 | 实际走 `DELETE /threads/{id}` 物理删除，不可恢复 | **失败（P0）** |

## 结论
- [x] 退回开发
- [ ] BLOCKED，需要产品决定
- [ ] 待人工验收

## 给用户的手测步骤
1. 打开首页，点任意会话菜单「归档」→ 弹窗承诺「已有的轮次全部保留」→ 确认后该会话连同全部历史轮次被物理删除且不可恢复（对比「删除」入口的「删除后不可恢复」文案）。
2. 用批量归档归档 2 条会话 → 刷新 → 在「已归档」组可见，点「取消归档」可回到活跃（与单条归档行为不一致，可直接对照）。

## 开发整改回复（2026-11-24）

- **P0 事实核对**：`DELETE /threads/{id}` 在当前代码里只把 `status` 改成
  `archived`，不删线程/轮次/事件/检查点；物理删除是独立的
  `DELETE /threads/{id}/permanent`。验收单里「归档会物理删除」的判断不成立，
  没有发生数据丢失。为消除调用歧义，前端单条归档仍改为显式
  `PATCH /threads/{id}` 带 `{status:"archived"}`。
- **P1**：`batch_archive_threads` docstring、`archiveThreads` 注释、T-BA 交接单
  已统一为「PATCH 改 status；物理删除走 /permanent」。
- **P2 测试**：给两条活跃会话设置不同 `updated_at`，消除同秒 `created_at`
  带来的不稳定排序；组合运行 42 passed。
- **P2 已归档 200 条提示**：产品确认选 A，已在 `sidebar.tsx` 加
  「仅显示最近 200 条已归档会话」提示（达到取数上限 200 时显示）；不改接口，
  分页留作后续。
- 复跑：`pytest tests/test_threads.py tests/test_thread_status_v151.py
  tests/test_thread_rewind_v117.py -q` → 42 passed；`tsc --noEmit` → 0；
  前端单测 → 492 passed / 0 failed；eslint → 0。