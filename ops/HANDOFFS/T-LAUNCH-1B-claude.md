# T-LAUNCH-1B 开发交接单（MASTER-LAUNCH-PLAN Step 1 批次 1B）

## 任务与范围

按 `docs/MASTER-LAUNCH-PLAN.md` 第九节 Step 1 的剩余 P0 项执行：

- 已实现：
  - **BUG-01 隔离/broker 下读图失效**：
    - `workspace.read_binary_file()`：与 `read_text_file()` 共用
      `resolve_inside + open_verified`，按 base64 返回原始字节；超过 `max_bytes`
      只回尺寸与 `too_large`，不把大文件读进内存。
    - `fs_broker` 新增 `read_bytes` 操作，broker 子进程直接调用
      `read_binary_file`，与宿主同一份路径/字节语义。
    - `filesystem._read_image()` 改为 async：`_routed(context)` 时走 broker，
      否则走宿主 `read_binary_file`；`_read_file` 不再因 `not _routed(context)`
      跳过读图。
  - **BUG-05 先起进程再挂 Job 的竞态窗口**：
    - `LocalSandbox.run` 以 `CREATE_SUSPENDED` 起进程 → `assign_pid` 挂 Job →
      `windows_job.resume_process(pid)` 枚举该进程线程并 `ResumeThread`。
    - 进程在挂起期间不会派生子进程，子进程从出生起就在 Job 里。
    - 恢复失败时杀进程树、在 `enforcement["resume_error"]` 与 notes 里如实记录，
      不把失败念成成功。
- 明确未实现：
  - `foreign_keys=ON`（ARCH-06b）：见 1A 交接单的证据与计划。
  - 3 项 `browser_check` 环境红：提权 pytest 临时目录 ACL，非本批代码回归。
  - MASTER-LAUNCH-PLAN Phase 2–4。

## 修改内容

- `services/api/src/blank_api/services/workspace.py`：
  新增 `read_binary_file()`；导入 `base64`。
- `services/api/src/blank_api/services/tools/fs_broker.py`：
  `_op_dispatch` 新增 `read_bytes`。
- `services/api/src/blank_api/services/tools/filesystem.py`：
  `_read_image()` 改 async、按 `_routed` 走 broker/宿主；删除未使用的 `base64` 导入。
- `services/api/src/blank_api/services/sandbox/windows_job.py`：
  新增 `CREATE_SUSPENDED` / `THREAD_SUSPEND_RESUME` / `TH32CS_SNAPTHREAD`、
  `_ThreadEntry32`、`CreateToolhelp32Snapshot/Thread32*/OpenThread/ResumeThread`
  签名与 `resume_process(pid)`。
- `services/api/src/blank_api/services/sandbox/runner.py`：
  `LocalSandbox.run` 使用 `CREATE_SUSPENDED`，先挂 Job 再恢复；新增
  `resume_error` 记账与 notes；恢复失败时杀进程并返回错误 stderr。
- `services/api/tests/test_fs_broker_v93.py`：
  新增 `test_image_read_goes_through_the_broker_with_the_same_bytes`。

## 验证结果

| 命令 | 结果 | 摘要 |
|---|---|---|
| `tests/test_fs_broker_v93.py tests/test_image_passthrough_v427.py` | **27 passed** | 图片经 broker 与宿主字节一致 |
| 沙箱相关 13 文件（Job/CPU/IO/磁盘/边界/矩阵/审计/拒绝/绕过/网络） | **170 passed** | 含真实命令、内存/进程数/进程树 |
| 文件读取/路径边界 5 文件 | **25 passed** | offset、workspace scope、外部化状态 |
| 全量 `pytest -q` | **3 failed / 5163 passed / 24 skipped** | 17:13，`logs/pytest-1b-2026-11-27.log`；3 项均为 browser_check 环境红 |
| `git diff --check` | **0** | 无空白/格式错误 |

**3 项环境红（同上批，非本批回归）：**
`test_browser_check_v427.py` 的 3 个真跑用例。pytest 以管理员权限运行时把
`tmp_path` 收紧为 owner=Administrators 的保护 DACL，Chrome 非提权沙箱读不到
临时页面，报 `net::ERR_FILE_NOT_FOUND`。同一探针打开仓库内
`acceptance/fixtures/kanban.html` 与 `E:\env\blank-tmp\probe.html` 均 `ok:true`。

## 风险与需要审查的点

- **broker 读图的 IPC 体积**：图片 base64 后最大约 5.3MB（4MB 原图），会走
  broker 的长度前缀 JSON 帧；默认调用超时 20s，慢磁盘/大图可能贴近上限。
  如果真机遇到超时，考虑单独调大 `read_bytes` 的超时或先缩图。
- **`resume_process` 按线程枚举**：只对我们刚创建、尚未 resume 的进程调用；
  若系统在 CreateProcess 与快照之间额外创建了线程，会全部 resume。正常
  `CREATE_SUSPENDED` 下只有主线程。
- **挂 Job 仍按 pid**：进程保持挂起，因此不存在“先派生再挂”的窗口；
  没有改成 `assign_handle` 是因为 asyncio 不暴露进程/线程句柄。语义上等价。
- **恢复失败路径**：会 `taskkill /T /F` 并返回 stderr；`exit_code` 可能为
  `None`，调用方应看 `enforcement["resume_error"]` 与 notes，不要把它当正常退出。
- **图片路径错误形状**：broker 侧 `ApiError` 被 `_read_image` 统一转成
  工具失败（`fail("error", ...)`），与旧宿主读图路径一致；不会静默返回空图。

## 建议人工验收

1. 开启 `features.fs_broker`，在真实任务里读一张 PNG，确认工具结果带
   `fs_broker.op=read_bytes` 且模型能看到图；同时确认 `fs_broker_latency` 有读数。
2. 在本机沙箱里跑一个会派生子进程的命令（例如 `python -c "import subprocess;..."`），
   确认子进程确实在 Job 里、结束后整棵树被带走，`enforcement` 没有 `resume_error`。
3. 真机跑一次超时命令，确认挂起/恢复改造没有影响超时杀进程树。
4. 非 Windows 环境确认 `resume_process` 不会被调用（`LocalSandbox` 分支）。
5. 进入 ARCH-06b：开 `foreign_keys=ON` 前先按 1A 交接单的证据修测试夹具。
