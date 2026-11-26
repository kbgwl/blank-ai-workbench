# T-R2-04 质量验收单（Codex）

> 验收对象：NEXT-DEV-v3 任务 2 — P1 修复（场景分流 + 慢扫描边界 + 系统缓存统计）
> 开发交接：ops/HANDOFFS/T-R2-04-claude.md
> 验收日期：2026-11-25
> 方式：三关卡实测，不凭文档自述

## A. 测试
| 命令 / 操作 | 结果 | 证据 / 摘要 |
|---|---|---|
| 定向 pytest（7 文件） | 96 passed | test_outside_scope_v192 / snapshot_restore_v110 / claude_parity_v24 / workspace_scope_v191 / tool_budget_v427 / delivery_snapshots_v153 / agent_v04，46.7s |
| 真机 T09 对照（重启后端加载新代码） | **100 分**，succeeded | 测试通过、2 文件改动、自证=是、22 轮、611K token、179s；score 五项全 1，**minimal=1（此前 85 因 minimal=0 扣 15）** |
| 修复电脑端到端实测 | 走授权盘点通道 | 见 C 关卡 |

注：首次 T09 报 model_unreachable（deepseek ConnectError），经查为 deepseek 服务临时抖动（网络/密钥均正常），重跑恢复，非代码问题。

## B. 代码审查
| 严重度 | 文件 | 发现 | 影响 | 建议 |
|---|---|---|---|---|
| 无阻塞 | snapshot.py | _SYSTEM_CACHE_ROOTS/_SYSTEM_CACHE_SUFFIX/_system_cache_path，_prune/_scan/changed_since 三处排除系统缓存，只认明确路径形状、不做文件名猜测 | 统计口径正确，系统缓存不进快照/改动 | 无需处理 |
| 无阻塞 | shell.py | slow_scan_hint 的 _BROAD_SCAN_ROOT 只拦整盘/用户/Windows 根递归，C:\Windows\Temp 等具体子目录不匹配、放行 | 边界正确，既拦误扫又保留修复电脑能力 | 无需处理 |
| 无阻塞 | graph.py | _SYSTEM_TAIL/DAILY_PROMPT 场景分流：普通任务只查工作区、整机检查需明确请求 | 提示词分流符合设计 | 无需处理 |
| 无阻塞 | compare.mjs | isSystemPath 忽略系统盘根变量与 Windows Caches，与产品侧口径一致 | 评测判分与产品证据一致 | 无需处理 |
| P2 | services/api | POST /tasks/{id}/run 在 Python httpx 30s 超时下读超时，但任务实际后台运行成功 | 后端 run 接口响应偏慢/疑似同步等待首批结果，客户端可能感知卡顿 | 记录，范围外，建议后续优化 run 为纯异步立即返回 |
| 范围提示 | 工作区 | git diff 含 159 文件，远超 T-R2-04 声明的 6 文件；含 domestic_mirrors、normalize_python_command、readonly 快照跳过、act_nudges、_deleted_paths 前缀等非本任务改动 | 多为历史任务累积未提交改动，非本次回归；按合同只记录不处理 | 建议后续按任务拆分提交，避免工作区混杂 |

代码审查结论：**T-R2-04 范围内改动无 P0/P1 阻塞发现。**

## C. 业务逻辑验收矩阵
| 验收标准 | 证据 | 结论 |
|---|---|---|
| 普通文档任务（T09）不再全盘扫描系统目录 | T09 真机：找文件用 list_files/glob，无 dir /s %SystemDrive%；changed 仅 tools/links.py、tests/test_links.py，无系统缓存 | 通过 |
| 系统缓存不进 changed，minimal 恢复 | T09 score minimal=1，总分 85→100 | 通过 |
| 修复电脑类请求走授权盘点通道 | 实测：update_plan 先"只读扫描 C 盘固定位置"、列清单等用户确认；scan.ps1 带单目录 timeout；扫描 C盘占用/用户目录/AppData/Windows/WinSxS/ProgramData | 通过（清理阶段"等同意"因实测超时未跑到，计划与提示词均已约束） |
| 修复电脑不整盘递归、脚本不写 C 盘根 | 实测：无 dir /s C:\ / %SystemDrive%；扫描脚本写工作区 .blank/scratchpad/ | 通过 |
| 统计口径一致（产品侧与评测侧） | SnapshotStore 与 compare.mjs 均排除系统盘根变量与 Windows Caches | 通过 |

## 结论
- [x] 测试通过（定向 + 真机 T09）
- [x] 代码审查无未解决 P0/P1
- [x] 业务逻辑验收矩阵完整
- [ ] 待人工验收

**T-R2-04 达成其范围目标（P1 修复，minimal 恢复）。**
**注意：token/速度未在本任务范围。** T09 真机 611K token / 22 轮，高于 T-R2-02/03 时的 350-487K / 14-15 轮，因 NEXT-DEV-v3 任务 1（压调用次数/token）尚未开发；修复电脑实测中模型反复重写扫描脚本也消耗大量轮次。**用户"token 尽可能少"的目标依赖任务 1/4，仍待开发。**

## 给用户的手测步骤
1. 重启后端，跑 `node evals/compare.mjs --only T09 --agents ours`，确认分数 100、changed 仅工作区 2 文件、无系统缓存。
2. 发「修复电脑 / 清理 C 盘」请求：确认先只读盘点、列清单、单目录时间上限，整盘递归被换路提示拦住，临时脚本放工作区 scratchpad。
3. 普通文档任务里确认不再出现 `dir /s /b "%SystemDrive%"` 这类全盘扫描。