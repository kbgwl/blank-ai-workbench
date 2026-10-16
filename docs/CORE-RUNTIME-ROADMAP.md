# Blank AI Core Runtime Roadmap

目标：把 Blank AI 从“可调用工具的任务页面”补齐为可恢复、可审计、可扩展的 Agent Harness，能力目标对齐 Codex / Claude Code 这一类工程执行产品。

## 当前基线

已经存在：

- LangGraph 状态图：计划、执行、观察、验证、修复、总结
- SQLAlchemy checkpoint，可恢复中断的图状态
- ToolGateway：工具注册、参数校验、风险判断、脱敏和调用记录
- DockerSandbox 与本地开发回退模式
- 高风险工具审批、取消、任务事件和交付证据包
- 统一 ModelAdapter，支持 OpenAI 兼容、Anthropic、Ollama 等供应商

尚未达标：

- 任务执行仍由 HTTP 请求同步等待，缺少持久 Worker 队列
- 事件接口是查询式，缺少可恢复的实时事件流协议
- 本地执行回退不是真正隔离，生产环境必须显式授权
- 审批按工具名授权，尚未绑定具体参数、路径和过期时间
- 缺少上下文压缩、工具结果分层和任务级上下文预算
- 多模型只有供应商选择，尚未有路由、fallback、审查和并行协作
- Usage 记录存在，但还没有完整 trace/span、指标和评测闭环
- 日常习惯尚未形成“候选记忆 -> 用户确认 -> Skill 版本”的生命周期

## 阶段 1：Durable Run Kernel

### 交付

- 引入 `Run` 资源，区分 task、thread、run、step
- `POST /runs` 只创建任务并返回 `run_id`
- Worker 从数据库领取 pending run，进程重启后可继续领取
- 增加 run 状态：queued、running、waiting_approval、paused、succeeded、failed、cancelled
- 所有事件增加 `run_id`、`step_id`、`parent_event_id`
- 提供从 `after_seq` 恢复的 SSE/WebSocket 事件流
- 支持 retry、cancel、resume，且不会重复执行已完成的工具步骤

### 验收

- 请求返回后关闭浏览器，任务仍能完成
- API 进程重启后，未完成 run 能被重新领取
- 同一 run 的工具调用不会因重试重复执行
- 客户端从任意 `after_seq` 连接都能补齐事件
- 审批等待期间不会占用 HTTP 请求

## 阶段 2：Safe Compute

### 交付

- Docker/Podman 沙箱成为生产默认；本地执行必须显式开启
- 文件、网络、环境变量、CPU、内存、进程数和超时全部有策略
- 审批绑定 `approval_id + arguments_hash + path_scope + expires_at`
- 高风险命令按具体动作审批，而不是按工具名永久放行
- 每次文件变更前自动 snapshot，支持一键回滚

### 验收

- 沙箱内无法读取工作区外文件和未授权密钥
- 默认网络关闭，开启网络必须有任务级记录
- 批准 `pnpm test` 不会同时批准其他 Shell 命令
- 任务失败后能恢复到执行前 snapshot
- Docker 不可用时生产配置直接失败，不静默降级到宿主机

## 阶段 3：Context and Skill Kernel

### 交付

- 当前任务上下文、历史对话、工具结果和用户记忆分层
- 自动压缩上下文，保留目标、约束、已完成动作、失败原因和未决事项
- 工具大输出写入 artifact，只把摘要放进模型上下文
- 日常记忆采用候选、确认、撤销、版本化 Skill 流程
- Skill 只能由用户审核后启用，并支持回滚和效果记录

### 验收

- 长任务超过上下文预算后仍可继续执行
- 压缩前后目标、约束和未完成步骤一致
- 用户未确认的日常内容不会进入长期 Skill
- Skill 可禁用、回滚和查看来源

## 阶段 4：Model Runtime and Observability

### 交付

- 按能力路由 planner、coder、reviewer、tester
- 模型超时、限流和 5xx 支持有限重试与 fallback
- 支持同一任务的多模型审查和结果比较
- 增加 trace/span、token、延迟、成本、重试、审批等待等指标
- 增加固定评测集，覆盖文件修改、测试修复、审批拒绝、恢复执行和提示注入

### 验收

- 主模型不可用时可按策略切换备用模型
- 每个模型调用、工具调用和审批都有可关联 trace
- 任务成功率、平均修复轮次、成本和失败原因可查询
- 评测集结果作为发布门禁，而不是只看单元测试

## 实施顺序

必须按以下顺序推进：

```text
Durable Run Kernel
        ↓
Safe Compute
        ↓
Context / Skill Kernel
        ↓
Model Runtime / Observability
```

页面只消费这些内核资源，不在前端自行模拟任务状态、模型状态或记忆状态。

