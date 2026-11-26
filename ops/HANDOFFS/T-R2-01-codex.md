# T-R2-01 质量验收单

## A. 测试
| 命令 | 结果 | 证据 / 摘要 |
|---|---|---|
| pytest 9 个文件（parity/delivery/edit/token/done/closure/unverified） | **147 passed** | 6.84s，无失败 |
| pytest test_agent_v04 + test_notebook_edit_v37 | **41 passed** | 29.01s |
| pytest 9 个审查范围/交付门槛文件 | **64 passed** | 19.39s |
| 直接实测脚本（edit/write/append/老记录回退） | **通过** | EDIT 回执无 diff、added=2/removed=0/hunks=1；WRITE 无 diff、added=2；APPEND 内容正确、计数正确；老记录回退 _result_counts=(1,1,1) |
| git diff --check | **1 处（非本任务）** | docs/validation/acceptance-2026-11-21.md:1868 行尾空行 |

## B. 代码审查
| 严重度 | 文件:行 | 发现 | 影响 | 建议 |
|---|---|---|---|---|
| P2 | editing.py:291-309, 327, 331-356 | **超出交接单声明范围**：新增 write_file `append` 参数与 `_conform_append`，工具描述同步更新。功能实测正确、有测试覆盖，但交接单未提及 | 范围管理失真，后续追溯困难；不影响功能 | 用户确认保留，或补记进交接单 |
| P2 | docs/validation/acceptance-2026-11-21.md:1868 | 行尾空行（diff --check） | 无功能影响，非本任务文件 | 提交前统一清理 |
| 无阻塞 | payload.py:70, 237 | DROP_DIFF_TOOLS 确认存在且覆盖写类工具，模型上下文本已丢弃 diff | 交接单「本步不直接降低模型 token」属实，收益在事件/检查点体积与堵 repl 旁路 | 路线图第 2/3 项（快照/压调用次数）继续 |

## C. 业务逻辑验收矩阵
| 验收标准 | 证据 | 结论 |
|---|---|---|
| 交付四行仍显示正确文件/行数（无 diff 也能算） | test_delivery_changed_paths_v488 计数用例通过；_review_scope/_change_facts 计数优先 | 通过 |
| 证据页/变更面板仍能打开完整 diff | _record_change 仍写 FileChange.diff（editing.py:494, 525, 574）；前端仅从 FileChange 读 diff | 通过 |
| 事件流 tool.completed 的 data 不再出现整段 diff | 直接实测 EDIT/WRITE outcome.data 无 diff 键 | 通过 |
| 老记录回退，旧检查点不失效 | 直接实测 _result_counts 老记录 (1,1,1)；_review_scope/_change_facts 双路回退 | 通过 |
| append 追加语义（范围外新增功能） | 直接实测：追加内容正确、二进制文件追加被拒、覆盖提示按 append 收敛 | 通过（范围外，需用户确认保留） |

## 人工验收（Codex 代做，端到端实测）
- 后端以 SQLite 启动（127.0.0.1:8000），前端 3000 已在运行。
- 创建真实任务（E9验收演示项目，deepseek-flash）：write_file 新建 receipt_check.txt → edit_file 改第二行 → 读回确认。任务 **succeeded**：8 次模型调用 / 160K token / 6 步 / 2 次变更。
- 交付四行：前端任务详情页展示「完成：receipt_check.txt 已存在，共 2 行；第 2 行是 line-two-modified…改动：1 个文件（receipt_check.txt）、1 处 Diff」，与接口一致。
- 证据页/变更面板：GET /api/v1/tasks/{id}/diff 返回完整 diff（新建 2 行 + 修改 1 行）。
- 事件流：5 条 tool.completed 事件 payload 只有 summary（如「已写入文件：receipt_check.txt（新建，2 行变更）」），无 data、无 diff。
- 检查点 artifact：write_file 316 字节、edit_file 410 字节，data 仅计数（added/removed/hunks），无 diff。
- 工作区文件真实内容：receipt_check.txt = hello receipt / line-two-modified，与验收标准一致。
- 浏览器探针：任务详情页 console 0 错误、HTTP 4xx/5xx 0，4 项断言全过，截图 .tmp-test/ui-task-detail2.png。

## 结论
- [x] 验收通过（Codex 代做端到端人工验收）
- 无未解决 P0/P1；P2 两项（append 范围外、行尾空行）已记录


## 给用户的手测步骤
1. 在测试项目里让 agent 修改一个小文件，确认交付四行「改动」仍显示正确文件/行数。
2. 打开证据页/变更面板，确认完整 diff 仍能查看。
3. 查看事件流，确认 `tool.completed` 的 `data` 不再出现整段 diff。
4. 真机 T09 对照（需后端重启）：记录调用数、prompt tokens、耗时，与基线 19 / 487,764 / 74.7s 比。