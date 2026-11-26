# T-R2-04 开发交接单：P1 修复（场景分流 + 慢扫描边界 + 系统缓存统计）

## 任务与范围

- 已实现：
  - **场景分流提示**：普通编程/文档/数据任务只查工作区，找文件用
    `list_files/glob/search_code`，不用 `dir /s` / `rglob` 扫 `C:\`、
    `%SystemDrive%`、`%SystemRoot%`；只有用户明确说「修复电脑 / 检查整机 /
    清理 C 盘」时才走整机盘点流程，且只查固定位置。
  - **慢扫描边界**：`slow_scan_hint()` 新增 `%SystemDrive%` / `%SystemRoot%` /
    `%windir%` / `$env:SystemDrive` 等系统盘根变量；整个盘根、C:\Users、
    C:\Windows 的递归仍返回换路提示；`C:\Windows\Temp` 这类具体子目录仍允许。
  - **统计口径**：`SnapshotStore` 在扫描、整树复制、`changed_since` 三处排除
    `%SystemDrive%` / `%SystemRoot%` / `%windir%` 与
    `ProgramData/Microsoft/Windows/Caches` 路径；`evals/compare.mjs` 的
    workspace 快照同样忽略这些路径，避免 T09 `minimal=0`。
- 明确未实现：
  - 没有新增独立的“整机检查通道”代码；复用 E12 已有的跨盘盘点/审批/固定位置
    规则，提示词只做场景分流。
  - 没有跑真机 T09 对照：后端当前在监听，但未确认已加载本次改动；需要重启后
    跑 `evals/compare.mjs --only T09 --agents ours --cleanup`。
  - NEXT-DEV-v3 任务 1（工具说明书瘦身 / 执行层合并 / 提示词强化）尚未开始。

## 修改内容

- `services/api/src/blank_api/services/agent/graph.py`：`_SYSTEM_TAIL` 与
  `DAILY_PROMPT` 增加普通任务只查工作区、整机检查需明确请求的规则。
- `services/api/src/blank_api/services/tools/shell.py`：`_BROAD_SCAN_ROOT`
  覆盖系统盘根变量；提示文案补 `%SystemDrive%`。
- `services/api/src/blank_api/services/sandbox/snapshot.py`：新增
  `_system_cache_path()`；`_scan`、`_prune`、`changed_since` 排除系统缓存副本。
- `evals/compare.mjs`：`snapshotTree` 忽略系统盘根变量与 Windows 缓存路径。
- `services/api/tests/test_outside_scope_v192.py`：系统盘根变量慢扫描断言。
- `services/api/tests/test_snapshot_restore_v110.py`：系统缓存不进快照/改动清单。
- `services/api/tests/test_claude_parity_v24.py`：场景分流提示判据。

## 验证结果

| 命令 / 操作 | 结果 | 摘要 |
|---|---|---|
| `pytest tests/test_outside_scope_v192.py tests/test_snapshot_restore_v110.py tests/test_claude_parity_v24.py tests/test_workspace_scope_v191.py tests/test_tool_budget_v427.py tests/test_delivery_snapshots_v153.py tests/test_agent_v04.py -q` | **96 passed** | 慢扫描/快照/提示词/工具预算/集成 |
| `node --check evals/compare.mjs` | 通过 | 语法 0 |
| 真机 T09 | 未跑 | 需重启后端加载本次改动 |

## 风险与需要审查的点

- 系统缓存过滤按明确路径形状识别（系统盘根变量 + Windows Caches 目录），
  不做文件名猜测；若用户工作区里真有一个叫 `%SystemDrive%` 的普通目录，
  它不会进快照/改动清单。按当前产品边界这是不可交付路径，风险可接受。
- `slow_scan_hint` 仍拦整个盘根/用户/Windows 根的递归；具体系统子目录允许。
  如果“修复电脑”确实需要整盘递归，应拆成固定位置/子目录逐个扫描，而不是
  恢复 30 秒必超时的单条命令。
- `compare.mjs` 的过滤只影响评测判分，不改产品证据；产品侧 `SnapshotStore`
  已同步过滤，两边口径一致。
- 未改 E12 的授权/审批链；跨盘写仍需逐次批准。

## 建议人工验收

1. 重启后端，跑 `node evals/compare.mjs --only T09 --agents ours --cleanup`，
   确认 `changed` 只有 `tools/links.py`、`tests/test_links.py`，`minimal=1`，
   分数从 85 恢复。
2. 发「修复电脑 / 清理 C 盘」类请求：确认它能走固定位置盘点，扫描
   `%TEMP%`、`C:\Windows\Temp` 等具体目录成功；整盘递归被换路提示拦住。
3. 普通文档任务里确认不再出现 `dir /s /b "%SystemDrive%"` 这类全盘扫描。
