# T-R2-02 / T-R2-03 质量验收单

## A. 测试
| 命令 | 结果 | 证据 / 摘要 |
|---|---|---|
| pytest 10 文件（快照/压缩/读后写） | **152 passed** | 9.77s |
| pytest 7 文件（Agent 会话/压缩集成） | **77 passed** | 48.31s |
| pytest 3 文件（提示词/集成/快照） | **78 passed** | 28.71s |
| 真机 T09 对照 ×2 | 见 C | 后端重启加载最新代码后跑 evals/compare.mjs --only T09 --agents ours |

## B. 代码审查
| 严重度 | 文件 | 发现 | 影响 | 建议 |
|---|---|---|---|---|
| 无阻塞 | file_state.py | _line_count 行数统计、render_snapshot 只列磁盘仍存在的文件（root 过滤）、按 mtime 可复现排序、export/restore 兼容旧检查点 | 实现正确 | 无需处理 |
| 无阻塞 | context.py | _call_paths 索引、_microcompact 折叠旧读结果为快照指针、micro:below 低于水位瘦身、MicroStats 计数 | 实现正确 | 无需处理 |
| 无阻塞 | graph.py | snapshot_paths 只含磁盘存在的文件、折叠发生后 _insert_handoff 注入快照 | 实现正确 | 无需处理 |
| 无阻塞 | test_claude_parity_v24.py | 提示词判据新增「不同文件的写/改」「直接用写入原文做 old_string」 | 覆盖到位 | 无需处理 |

## C. 业务逻辑验收矩阵（真机 T09 ×2）
| 验收标准 | 证据 | 结论 |
|---|---|---|
| 快照折叠真实生效 | 9 次 context.microcompacted 全部 micro:below；reads_folded 累计 8 次；chars_saved 累计 8,708 | 通过 |
| 调用次数下降（基线 19） | 第一次 15 轮、第二次 14 轮 | 部分达标：降 21-26%，目标 ≤12 未达 |
| token 下降（基线 487K） | 第一次 407K、第二次 350K | 部分达标：降 17-28%，目标 ≥30% 未达（第二次 28% 接近） |
| 任务正确性不降 | 两次 succeeded、测试 5 项通过、自证 true、命中 1 | 通过 |
| 耗时不升 | 基线 74.7s；第一次 116s（偏慢）、第二次 75s（持平） | 第二次通过 |

## 新发现（P1，非本次回归，需记录）
- T09 任务中模型跑 dir /s /b "%SystemDrive%" 全盘扫描 C 盘（seq=46 run_shell），系统缓存文件（%SystemDrive%/ProgramData/Microsoft/Windows/Caches/*.db）被变更统计算进 changed，导致 minimal=0（85 分）。
- 成因：模型工作区边界意识不足（为找文档全盘扫描）+ 变更统计把系统文件 mtime 变化计入。与 T-R2-02/03 改动无关（非本次回归），但影响评测分数与安全观感。

## 结论
- [x] 验收通过（功能正确、快照真实生效）
- 路线图判据未完全达成：调用 ≤12 未达、token ≥30% 未达（第二次 28% 接近）
- 建议：继续压调用次数第二轮（工具说明书瘦身 + 执行层合并 + 提示词强化）；模型全盘扫描问题单独记录处理。

## 给用户的手测步骤
1. 重跑 node evals/compare.mjs --only T09 --agents ours --cleanup，观察调用数/token/耗时。
2. 查任务事件 context.microcompacted，确认 micro:below 与 reads_folded。