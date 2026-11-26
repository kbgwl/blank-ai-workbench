# T-R2-06/07 质量验收单（Codex）

> 验收对象：NEXT-DEV-v4 任务 1b/1c、方案 B、任务 3、任务 4、任务 5（部分）
> 开发交接：ops/HANDOFFS/T-R2-06-claude.md、ops/HANDOFFS/T-R2-07-claude.md
> 验收日期：2026-11-25
> 方式：三关卡实测，不凭文档自述（T-R2-07 自述未跑 pytest，由我补跑）

## A. 测试
| 命令 / 操作 | 结果 | 证据 / 摘要 |
|---|---|---|
| T-R2-07 定向（任务 3/4/5，12 文件） | **122 passed / 3 failed** | 3 失败：test_handoff_e2e_v12×2、test_unknown_facts_e2e_v147×1（审批 202 + settle 后响应结构读取 KeyError） |
| 真机 T09（重启后端加载新代码） | **100 分**，succeeded | token **360K（降约 41%，达成 ≥30%）**、**15 轮（目标 ≤12，未达）**、2 文件、自证=是；方案 B 生效，minimal 恢复 |
| 全量 pytest | **5117 passed / 35 failed / 28 skipped** | 38 分 18 秒。对比上次 5097/37：+20 新增、-2；但**任务 4 引入 6 个新 stream 失败、lsp 13 失败复现** |

## B. 代码审查
| 严重度 | 文件 | 发现 | 影响 | 建议 |
|---|---|---|---|---|
| **P1** | graph.py（任务 4） | `_call_adapter` 无条件调用 `_clear_speculation(self)`，访问 `session.speculative`；测试 mock 的 AgentSession 无该属性 → `AttributeError`。推测执行对 session 属性无兼容 | 6 个 stream 测试失败（stream_fallback_budget×3、reasoning_only_stream、skill_model、truncated_tool_call） | 开发修复：`_clear_speculation` / `_call_adapter` 对缺 `speculative` 属性做兼容（getattr 兜底），并同步 `_call_adapter` 签名测试断言 |
| 无阻塞 | hooks.py（任务 3-a） | `HookSpec.on_failure`（open/block），只对阻断型事件生效，无效值按 open 兜底 | 钩子 fail-closed 实现正确 | 无需处理 |
| 无阻塞 | graph.py（任务 3-b） | `_review_done_when` 确定性对账：仅 status=met 且 evidence 非空算证明，任一未证明即 approved=false，解析失败也按未通过 | done_when blocking 实现正确 | 无需处理 |
| 无阻塞 | graph.py（任务 3-c） | 审查范围用 git baseSha + 四组路径，只给路径不读 diff | 审查确定性范围实现正确 | 无需处理 |
| 范围提示 | 工作区 | git diff 含大量历史累积未提交改动，非本次回归 | 按合同只记录不处理 | 建议按任务拆分提交 |

## C. 业务逻辑验收矩阵
| 验收标准 | 证据 | 结论 |
|---|---|---|
| 方案 B：T09 分数回到 100、minimal=1 | T09 真机 100 分、2 文件、minimal 恢复 | 通过 |
| 降 token ≥30% | T09 611K→360K，降约 41% | 通过 |
| 调用次数 ≤12 | T09 15 轮 | **未通过** |
| 任务 3 质量底线（done_when/钩子/范围） | 定向 122 passed + 代码审查正确 | 通过 |
| 任务 4 推测执行无回归 | 全量 6 个 stream 失败，`AttributeError: speculative` | **未通过（引入回归）** |
| 任务 2 lsp 修复 | 全量 lsp 13 失败复现 | **未通过（未真正修复）** |
| 任务 5 测试清理 | 部分 202 断言已修，handoff_e2e_v12/unknown_facts_e2e_v147 仍失败 | **部分未完成** |

## 关键发现
1. **任务 4（推测执行）引入回归**：`_clear_speculation` 访问 `session.speculative` 无兼容，导致 6 个 stream 测试 `AttributeError`。这是本次验收最重要的 P1，必须修复。
2. **lsp 13 失败真实复现**：T-R2-07 称"以 2026-11-24b 日志 0 失败结案"，但本次全量复现 13 个 lsp 失败，**任务 2 未真正修复**，不能结案。
3. **调用次数未达**：1b/1c 未把 T09 从 15 轮压到 ≤12，token 已达但轮次目标未达。
4. **任务 5 部分未完成**：handoff_e2e_v12、unknown_facts_e2e_v147 的 202 断言仍有问题。

## 结论
- [x] 测试通过（定向 + 真机 T09 + 全量已跑）
- [x] 代码审查完成（发现 P1：任务 4 回归）
- [x] 业务逻辑验收矩阵完整
- [ ] **退回开发（P1 未解决 + lsp 未修复 + 调用未达）**

**任务 1b/1c、方案 B、任务 3 达成；任务 4 引入 P1 回归、任务 2 lsp 未真正修复、调用次数未达，需退回开发。**

## 给用户的手测步骤
1. 重启后端，跑 `node evals/compare.mjs --only T09 --agents ours`：确认 100 分、360K、15 轮（轮次未达 ≤12）。
2. 跑全量 `pytest tests/ -q`：确认 lsp 13 失败、6 个 stream 失败（`AttributeError: speculative`）。
3. 修复后复跑：确认 stream 失败消失、lsp 13 消失、T09 轮次 ≤12。