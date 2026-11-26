# 空白格 AI 操作台 — 升级需求文档

> 版本：2026-11-26  
> 状态：**待开发**  
> 本文档面向开发工具/Agent 直接执行，每项均含精确文件路径、验收标准和注意事项。

---

## 总览

| 优先级 | 编号 | 标题 | 预估改动量 |
|--------|------|------|-----------|
| 🔴 P0 | UP-01 | 写文件工具只返回摘要，丢掉全文 diff | 小（约 20 行） |
| 🔴 P0 | UP-02 | 修复推测执行 `speculative` 属性兼容性 bug | 小（约 15 行） |
| 🔴 P0 | UP-03 | 测试套件归档与环境隔离 | 中（配置 + 移动文件） |
| 🟠 P1 | UP-04 | Windows 沙箱网络隔离与资源限制 | 大（新增模块） |
| 🟠 P1 | UP-05 | 前后端 SSE 心跳与 e2e 冒烟测试进 CI | 中 |
| 🟡 P2 | UP-06 | `done_when` 改为真正 blocking | 小（约 30 行） |
| 🟡 P2 | UP-07 | 钩子 `on_failure: block` 安全拦截 | 小（约 20 行） |
| 🟡 P2 | UP-08 | 评测任务集扩充与自动对比 | 大（内容建设） |

---

## UP-01 写文件工具只返回摘要（🔴 P0）

### 背景

`editing.py` 写文件后把整个新文件内容作为 diff 返回给模型。  
模型随后又把这份完整内容放进下一轮历史，造成上下文持续膨胀。  
实测：T09 类任务单次约 611K token，目标 ≤ 40K。

### 目标

写文件工具完成后，只向模型返回一行摘要，不再返回文件全文或完整 diff。

### 文件范围

```
services/api/src/blank_api/services/tools/editing.py
services/api/src/blank_api/services/agent/payload.py   （确认 DROP_DIFF_TOOLS 是否已启用）
```

### 具体改动

**`editing.py`** — 在工具返回结果的地方，把：

```python
# 现状（伪代码示意，以实际代码为准）
return ToolResult(content=full_new_content_or_diff)
```

改为：

```python
# 目标
return ToolResult(content=f"已写入 `{path}`（{changed_lines} 行变更）")
```

规则：
- 只返回：文件路径 + 变更行数（可选：新增行数 / 删除行数）
- 不返回：文件全文、完整 diff、patch 内容
- 如果写入失败，仍然返回完整错误信息（不受此限制）

### 验收标准

1. 单元测试：写入一个 100 行文件，工具返回字符串长度 < 100 字符
2. `services/api/tests/` 中与 `editing` 相关的现有测试全部通过
3. 真机跑 T09 任务，token 峰值较改前下降 ≥ 25%

### 注意事项

- `agent/payload.py` 中有 `DROP_DIFF_TOOLS` 常量，确认是否已经有类似截断逻辑，避免重复
- 不要修改工具的**输入参数**和**写盘行为**，只改**返回给模型的内容**
- Diff 面板（前端）的数据来源是 `file_changes` 表，不依赖工具返回值，不受影响

---

## UP-02 修复推测执行 `speculative` 属性兼容性 Bug（🔴 P0）

### 背景

`services/agent/graph.py` 中 `_call_adapter` 和 `_clear_speculation` 无条件访问 `session.speculative`，  
但测试 mock 的 `AgentSession` 对象没有该属性，导致 6 个 stream 测试抛 `AttributeError`。

### 目标

让 `_call_adapter` 和 `_clear_speculation` 在 `speculative` 属性不存在时优雅降级，不崩溃。

### 文件范围

```
services/api/src/blank_api/services/agent/graph.py
services/api/tests/（与 stream/speculative 相关的测试文件）
```

### 具体改动

**`graph.py`** — 把所有直接访问 `session.speculative` 的地方改为：

```python
# 现状
session.speculative = True

# 目标
if hasattr(session, "speculative"):
    session.speculative = True
```

或统一用 `getattr(session, "speculative", False)` 读取。

同时，如果 `_call_adapter` 签名近期有变化（加了 `on_tool_call` 参数），  
需要同步更新相关测试文件中的 mock 调用签名。

### 验收标准

以下 6 个测试必须全部通过（零修改测试逻辑，只修产品代码）：

```
test_stream_fallback_budget_v173（×3 变体）
test_reasoning_only_stream_v174
test_skill_model_v52
test_truncated_tool_call_v175
```

运行命令：

```bash
cd services/api
uv run pytest tests/ -k "stream_fallback or reasoning_only_stream or skill_model_v52 or truncated_tool_call_v175" -v
```

### 注意事项

- **不要**改变推测执行的设计意图（提前跑只读工具、执行点仍走完整网关）
- 只补属性兼容，不改推测执行的触发条件和逻辑

---

## UP-03 测试套件归档与环境隔离（🔴 P0）

### 背景

`services/api/tests/` 现有 **417 个 `.py`** 测试文件、约 8MB，  
旧版本测试按版本号累积（`_v406`、`_v165`...）从不删除，  
导致全量跑有 35 个失败，分布在三类问题：

1. **worktree 环境残留污染**：前一个测试留下的 worktree 目录影响后续用例
2. **202 格式不兼容**：旧断言格式与新 schema 不匹配
3. **output_styles 未对齐**：样式枚举值发生变化但旧测试没更新

### 任务 A：归档旧测试

**步骤：**

1. 在 `services/api/tests/` 下新建 `_archive/` 目录
2. 把**无明确维护者且全量失败**的旧测试移入 `_archive/`（移动，不删除）
3. 在 `services/api/pyproject.toml`（或 `conftest.py`）的 `norecursedirs` 中添加 `_archive`

```toml
# pyproject.toml
[tool.pytest.ini_options]
norecursedirs = ["_archive", ".venv", "__pycache__", ".tmp-test"]
```

**判断"可归档"的标准（同时满足）：**
- 文件名带版本号后缀（如 `_v123`）
- 对应的新版本文件（更高版本号）已存在
- 在当前全量运行中持续失败

### 任务 B：worktree 测试隔离

**文件范围：**

```
services/api/conftest.py
services/api/tests/（所有使用 worktree 的测试 fixture）
```

**要求：**

在创建 worktree 的 pytest fixture 中，添加 `yield` 后的清理逻辑：

```python
@pytest.fixture
def temp_worktree(tmp_path):
    wt = create_worktree(tmp_path / "wt")
    yield wt
    # 确保清理，即使测试失败也执行
    try:
        remove_worktree(wt)
    except Exception:
        pass
```

每个 worktree 测试必须使用独立的临时目录（`tmp_path`），不共用全局路径。

### 任务 C：修复 202 格式失败

逐项修复以下测试文件（以实际失败列表为准，参考 `dev/NEXT-DEV-v5.md` P2 节）：

```
test_approval_card_v101    （×2）
test_permission_modes_v11  （×2）
test_handoff_e2e_v12       （×1）
test_unknown_facts_e2e_v147（×1）
test_output_styles_v47     （逐项归因）
```

修复原则：**只改断言和格式，不改产品接口行为**。

### 验收标准

```bash
cd services/api
uv run pytest tests/ --ignore=tests/_archive -x -q
# 结果：0 failed（或仅剩明确记录的环境限制项）
```

---

## UP-04 Windows 沙箱网络隔离与资源限制（🟠 P1）

### 背景

`services/api/src/blank_api/services/sandbox/windows_restricted.py` 实现了文件边界隔离（Restricted Token + 能力 SID），但：

- **网络未隔离**：代码注释明确写 `network_enforced=False`，Agent 执行的 `run_shell` 命令可自由访问外网
- **无 CPU/内存限制**：Job Object 已用于进程树管理，但未设置资源上限
- 产品安全承诺要求高风险操作（网络访问）必须经人工审批

### 任务 A：本机模式网络隔离警告

这是**最低要求**，不实现真正隔离时至少必须做到：

**文件：** `services/api/src/blank_api/services/sandbox/rules.py`（或沙箱初始化入口）

在本机模式启动时，向前端推送一个 `warning` 级别系统事件：

```json
{
  "event": "sandbox.warning",
  "message": "本机模式下网络访问未受限，Agent 执行的命令可访问外网",
  "level": "warning"
}
```

同时在任务创建响应中附加 `sandbox_constraints.network_isolated: false`。

### 任务 B：Job Object CPU/内存限制

**文件：** `services/api/src/blank_api/services/sandbox/windows_job.py`

在创建 Job Object 后，调用 `SetInformationJobObject` 设置 `JobObjectExtendedLimitInformation`：

```python
# 默认限制（可通过项目设置覆盖）
CPU_RATE_LIMIT = 80       # CPU 使用率上限 80%
MEMORY_LIMIT_MB = 2048    # 内存上限 2GB
PROCESS_COUNT_LIMIT = 32  # 进程数上限
```

如果设置失败（非管理员权限），记录警告日志，不阻断任务启动。

### 任务 C：Windows Firewall 规则绑定（可选，P1+ 级别）

通过 `netsh advfirewall` 或 Windows Filtering Platform API，  
对 Job Object 内的进程组设置出站规则，默认拒绝非白名单域名。

> ⚠️ 此项实现复杂度高，需要管理员权限，建议作为独立子任务评估可行性后再动工。  
> 优先完成 A 和 B。

### 验收标准

**任务 A：**
- 新建任务时，API 响应包含 `sandbox_constraints.network_isolated: false`
- 前端任务头部显示"⚠️ 网络未隔离"提示

**任务 B：**
- 在受限进程内运行内存分配脚本，超过 `MEMORY_LIMIT_MB` 时进程被 Kill
- `windows_job.py` 相关单元测试通过

---

## UP-05 前后端 SSE 心跳与 e2e 冒烟测试进 CI（🟠 P1）

### 背景

- 开发模式（`:8000/:3000`）和桌面版是两个独立进程，SSE 断开后前端无感知
- `scripts/smoke_flow.py` 有 15 项端到端检查，但不在 CI 中自动运行
- `check.sh` 14/15 有红项（审批留痕问题），说明端到端一致性仍有漏洞

### 任务 A：SSE 心跳与断线重连

**文件：** `web/src/components/tasks/use-task-events.ts`（或 SSE 连接管理模块）

要求：
1. 服务端每 **15 秒**发送一次 `event: ping` 心跳（后端 SSE 路由添加）
2. 客户端收到超过 **30 秒**没有任何消息时，自动重连（指数退避，最多 5 次）
3. 重连时附带 `Last-Event-ID`，服务端从该事件 ID 之后续传

**后端文件：** `services/api/src/blank_api/api/routes/monitor.py`（SSE 路由）

```python
# 心跳示例
async def sse_generator():
    async for event in task_events():
        yield event
    # 在合适位置插入心跳
    yield ServerSentEvent(event="ping", data="")
```

### 任务 B：e2e 冒烟测试进 CI

**文件：** `scripts/smoke_flow.py`（已有，15 项检查）

要求：
1. 在 `check.sh` 中添加冒烟测试步骤（在现有 tsc + 前端单测之后）
2. 冒烟测试必须能在 CI 环境中以 SQLite 模式启动后端（无需 Docker）
3. 修复当前红项：审批留痕（`approval.requested` 事件）确保在命令挂起时正确写库

### 验收标准

**任务 A：**
```javascript
// 测试：模拟 SSE 断开，30 秒内自动重连
// web/src/components/tasks/use-task-events.test.ts
test('SSE 断线后自动重连', ...)
```

**任务 B：**
```bash
./check.sh
# 结果：15/15 全绿
```

---

## UP-06 `done_when` 改为真正 blocking（🟡 P2）

### 背景

`done_when` 是任务验收条件判断机制。  
现状：判断没有通过只记录 `finding`，不真正拦截任务，任务仍标记为成功。  
这导致"验收合同"形同虚设——模型自己说完成了就算完成了。

### 目标

`done_when` 判断不通过 → 任务状态设为 `FAILED`，不可设为 `SUCCEEDED`。

### 文件范围

```
services/api/src/blank_api/services/agent/goals.py
services/api/src/blank_api/services/agent/delivery.py
services/api/src/blank_api/services/agent/closure.py
```

### 具体改动

**`goals.py`（或验收判断主逻辑）：**

找到 `evaluate_done_when` 或类似函数，修改返回值语义：

```python
# 现状（伪代码）
def evaluate_done_when(criteria, evidence) -> DoneWhenResult:
    met = check_all(criteria, evidence)
    return DoneWhenResult(met=met, findings=findings)
    # 调用方只记录 findings，不阻断

# 目标
def evaluate_done_when(criteria, evidence) -> DoneWhenResult:
    met = check_all(criteria, evidence)
    if not met:
        raise DoneWhenNotMetError(unmet_criteria=unmet, findings=findings)
    return DoneWhenResult(met=True, findings=findings)
```

**`delivery.py` 或 `closure.py`：**

在任务收尾节点，捕获 `DoneWhenNotMetError`，将任务状态设为 `FAILED`，  
并在最终报告中列出未通过的验收条件。

### 验收标准

```python
# 新增测试（放在 tests/test_goals_blocking_v1.py）
def test_done_when_blocks_on_failure():
    """验收条件不满足时，任务必须以 FAILED 结束"""
    result = run_task_with_criteria(["测试通过率 100%"], actual_pass_rate=80)
    assert result.status == "FAILED"
    assert "测试通过率 100%" in result.unmet_criteria
```

---

## UP-07 钩子 `on_failure: block` 安全拦截（🟡 P2）

### 背景

安全类钩子（如权限检查、审批前置）失败时，当前行为不确定：  
部分情况下钩子崩溃会被忽略，工具调用照常执行。  
这违反了"高风险操作必须审批"的安全承诺。

### 目标

给 `HookSpec` 增加 `on_failure` 字段，安全类钩子配置 `on_failure: block` 后，  
钩子抛出任何异常都必须阻断后续工具调用。

### 文件范围

```
services/api/src/blank_api/services/agent/governance.py    （钩子执行器）
services/api/src/blank_api/services/permissions.py         （权限钩子定义）
services/api/src/blank_api/services/tools/gateway.py       （工具网关）
```

### 具体改动

**数据结构（`governance.py` 或 schema）：**

```python
class HookSpec(BaseModel):
    name: str
    on_failure: Literal["ignore", "warn", "block"] = "warn"
    # ... 其他字段
```

**执行器逻辑：**

```python
async def run_hook(hook: HookSpec, context: HookContext):
    try:
        return await hook.execute(context)
    except Exception as e:
        if hook.on_failure == "block":
            raise HookBlockError(hook=hook.name, cause=e)
        elif hook.on_failure == "warn":
            logger.warning(f"Hook {hook.name} failed: {e}")
            return None
        # ignore: 静默
```

**安全类钩子默认配置改为 `block`：**

```python
# permissions.py
APPROVAL_REQUIRED_HOOK = HookSpec(
    name="approval_required",
    on_failure="block",   # ← 改为 block
)
```

### 验收标准

```python
def test_security_hook_failure_blocks_tool():
    """权限钩子异常时，工具调用必须被阻断"""
    with mock_hook_raising(ApprovalHook, RuntimeError("审批服务不可用")):
        result = gateway.call_tool("delete_file", path="/important.txt")
    assert result.status == "BLOCKED"
    assert "审批服务不可用" in result.reason
```

---

## UP-08 评测任务集扩充与自动对比（🟡 P2）

### 背景

产品目标是质量超过 WorkBuddy/Qoder/Trae，但当前评测体系：
- `evals/` 目录存在但任务集不足
- 对比是手工跑，无自动化输出
- 没有统一的"质量分"定义

### 任务 A：扩充评测任务集

在 `evals/tasks/` 下新增以下类别的任务（每类至少 5 个）：

| 类别 | 示例任务 |
|------|---------|
| 编程修复 | 修复指定 bug，要求测试通过 |
| 功能新增 | 给接口增加参数校验 + 单元测试 |
| 文档同步 | 根据代码变更更新 README |
| 代码重构 | 提取公共函数，不改行为 |
| 多文件任务 | 跨 3 个以上文件的联动修改 |

每个任务文件格式：

```json
{
  "id": "task-001",
  "title": "给 login 接口增加邮箱格式校验",
  "workspace": "fixtures/sample-python-api",
  "prompt": "...",
  "acceptance_criteria": [
    "非法邮箱返回 HTTP 400",
    "合法邮箱正常通过",
    "新增至少 3 个测试用例",
    "原有测试全部通过"
  ],
  "max_turns": 10,
  "max_tokens": 50000
}
```

### 任务 B：自动对比脚本

**文件：** `evals/compare.mjs`（已存在，需扩充）

输出格式：

```
任务 ID       | kbgai | WorkBuddy | Qoder | 获胜
task-001      |  100% |      85%  |  90%  | kbgai
task-002      |   90% |      95%  |  88%  | WorkBuddy
...
平均           |  95%  |      88%  |  91%  | kbgai
```

指标定义：
- **通过率**：`done_when` 验收条件全部通过 = 100%，每少一项 -20%
- **耗时**：单任务平均耗时（秒）
- **token**：单任务平均消耗 token 数

### 验收标准

```bash
cd evals
node compare.mjs --tasks tasks/ --model kbgai --baseline workbuddy
# 输出包含：总任务数、各项平均分、获胜方
```

---

## 执行顺序建议

```
UP-02（修 bug，10 分钟）
  ↓
UP-01（减 token，改动最小收益最大）
  ↓
UP-03（清测试，让 CI 可信）
  ↓
UP-05 任务 B（冒烟进 CI）
  ↓
UP-06（done_when blocking）
  ↓
UP-07（hook fail-closed）
  ↓
UP-04（沙箱安全，改动大，单独排期）
  ↓
UP-08（评测体系，持续建设）
```

---

## 关键路径文件速查

```
后端入口
  services/api/src/blank_api/app.py

Agent 核心
  services/api/src/blank_api/services/agent/graph.py      ← UP-02
  services/api/src/blank_api/services/agent/goals.py      ← UP-06
  services/api/src/blank_api/services/agent/governance.py ← UP-07
  services/api/src/blank_api/services/agent/delivery.py   ← UP-06

工具层
  services/api/src/blank_api/services/tools/editing.py    ← UP-01
  services/api/src/blank_api/services/tools/gateway.py    ← UP-07
  services/api/src/blank_api/services/permissions.py      ← UP-07

沙箱
  services/api/src/blank_api/services/sandbox/windows_restricted.py ← UP-04
  services/api/src/blank_api/services/sandbox/windows_job.py        ← UP-04

前端事件
  web/src/components/tasks/use-task-events.ts             ← UP-05
  services/api/src/blank_api/api/routes/monitor.py        ← UP-05

测试与评测
  services/api/tests/                                     ← UP-03
  services/api/conftest.py                                ← UP-03
  scripts/smoke_flow.py                                   ← UP-05
  evals/                                                  ← UP-08
  check.sh                                                ← UP-05
```

---

## 通用约束（所有升级项共同遵守）

1. **不改产品接口行为**：所有 schema、HTTP 路由的入参/出参不变，只改内部实现
2. **不删测试，只归档**：旧测试移到 `_archive/`，不直接删除
3. **每项改完单独跑定向测试**：不要等全量跑，改完立刻跑受影响的测试文件
4. **改动必须有对应测试**：UP-01 到 UP-07 每项都需要新增或更新至少一个测试用例
5. **Windows 兼容性优先**：项目在 Windows 上运行，避免使用 Linux 专有 API（cgroups、`/proc` 等）
