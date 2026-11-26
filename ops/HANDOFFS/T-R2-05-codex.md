# T-R2-05 质量验收单（Codex）

> 验收对象：NEXT-DEV-v4 任务 1a — 工具说明书瘦身
> 开发交接：ops/HANDOFFS/T-R2-05-claude.md
> 验收日期：2026-11-25
> 方式：三关卡实测，不凭文档自述

## A. 测试
| 命令 / 操作 | 结果 | 证据 / 摘要 |
|---|---|---|
| 定向 pytest（9 文件） | 219 passed | 工具预算/技能/任务/审查/Shell/Agent 集成，98.56s |
| 真机 T09 对照（重启后端加载新代码） | **94 分**，succeeded | token **372K（降约 39%，达成 ≥30% 目标）**、15 轮、3 文件改动、自证=是；**minimal=0.6（扣 6 分）** |
| 真机 T01 对照 | **100 分**，succeeded | 156K、8 轮、2 文件、自证=是，工具调用正确性未被瘦身破坏 |

注：首次 T09 曾遇 deepseek 临时抖动，重跑恢复，非代码问题。

## B. 代码审查
| 严重度 | 文件 | 发现 | 影响 | 建议 |
|---|---|---|---|---|
| 无阻塞 | registry.py | Skill 市场四工具（propose_skills/find_skills/install_market_skill/list_curated_skills）进 DEFERRED_TOOLS，tool_search 可拉回；SCHEMA_BUDGET_CHARS 22,700→20,000 | 按需加载正确，预算收敛有测试钉着 | 无需处理 |
| 无阻塞 | shell.py/ask.py/filesystem.py/editing.py/plan.py/tasks.py/skill_proposals.py/review.py | 文案压缩只删重复与冗词，保留行为约束与参数说明 | 219 测试 + T01 满分间接验证未破坏工具调用 | 无需处理 |

代码审查结论：**T-R2-05 范围内改动无 P0/P1 阻塞发现。**

## C. 业务逻辑验收矩阵
| 验收标准 | 证据 | 结论 |
|---|---|---|
| 降 token（核心目标） | T09 611K→372K，降幅约 39%，超 ≥30% 目标 | 通过 |
| 工具调用正确性未被瘦身破坏 | T01 100 分、succeeded；定向 219 passed | 通过 |
| Skill 四工具按需可拉回 | DEFERRED_TOOLS + test_skill_market_search_v406 覆盖 | 通过 |
| 分数不降（无回归） | **T09 100→94，minimal=0.6** | **未通过（扣分风险）** |

## 关键发现：T09 minimal 扣分（需决策）
- T09 任务要求"在 tools 下提供可执行入口"。模型建了 `tools/check_links.py` 作入口，
  **满足验收标准（入口脚本可运行）**，但评分 `expect_files` 只认 `tools/links.py`+`tests/`，
  多出的入口被算"多余改动"，`minimal=0.6`，总分 100→94。
- **不是功能错误**：任务 succeeded、测试通过、自证=是；是**模型实现路径变化**与**评分口径**不一致。
- 对比：T-R2-04 时模型未建独立入口（仅 links.py+test_links.py，100 分）；瘦身后模型改建 check_links.py。

## 结论
- [x] 测试通过（定向 + 真机 T09/T01）
- [x] 代码审查无未解决 P0/P1
- [x] 业务逻辑验收矩阵完整
- [ ] **待人工验收 + 决策 minimal 扣分处理**

**任务 1a 达成核心目标（降 token），但引入 T09 分数扣分（94），需你决策处理方式。**

## 给用户的手测步骤
1. 重启后端，跑 `node evals/compare.mjs --only T09 --agents ours`，确认 token 下降、分数 94、minimal=0.6。
2. 跑 `--only T01`，确认工具调用正确性（100 分）。
3. 在聊天里说"找技能/装技能"，确认 tool_search 能把 Skill 市场工具拉回来。

## 待决策：minimal 扣分处理（三选一）
- **方案 A**：调提示词，让模型不建多余入口（可能限制合理实现）。
- **方案 B**：更新 T09 `expect_files`，把合理的可执行入口纳入评分口径（承认模型实现合理）。
- **方案 C**：接受 94 分，记录为已知扣分，推进后续任务。