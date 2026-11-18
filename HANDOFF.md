# 交接单（2026-11-18 · 项目暂停点）

> 新会话从这里接手。**这份文件是唯一入口**，不要靠聊天记录回忆。

## 一、唯一目标

> 一次真实的编程任务，从你说出口，到看见 diff、测试结果、谁批准执行，全程在你眼前走完。

三步，按顺序，不做别的：

| 步 | 内容 | 判据 |
|---|---|---|
| 1 | 审批留痕：拦住时发 `approval.requested`，拒绝时发 `approval.decided` | `./check.sh` 15/15 |
| 2 | 同源：桌面版与开发服务共用同一份数据 | 我发一句，用户在自己窗口看得到那条会话与那次批准 |
| 3 | 真实任务：修一个坏函数 → 跑测试 → 出 diff | 红→绿 + diff + EXIT 码，不是模型自述 |

## 二、当前状态（已核实）

- `./check.sh`：**14/15**。唯一红项 = 审批未留痕（命令被拦住了，但没有 `approval.requested`，也没建待审记录）
- 后端：`4332 passed`；前端：`330 passed`、`tsc` 0 错误
- 项目数据：只剩 `kbgai`（`E:/projects/kbgai/kbgai`，branch=main），测试项目已清空
- DeepSeek provider 已配 key（掩码 `****1558`），端到端可调通
- 已完成并落地：技能库 12 条内置、技能市场（GitHub 搜索 + 本地镜像）、会话分叉、项目置顶/排序、上下文条显示 `项目·目录·分支`

## 三、冻结清单（不准做）

技能市场扩展、REA 全量、Codex 内核接入、Artifact、多 IM、语音、UI 打磨。
新想法先丢「待办池」，当周三步全绿后才允许取一件。

## 四、已踩过的坑（别重犯）

1. **断言写得比事实宽**：冒烟曾只匹配 `run_shell` 字样就判定"该进审批"，其实模型跑的是 `echo`（medium，不问）。断言必须匹配**具体命令**，并区分"没调"与"调了没拦"。
2. **"更严"≠"更对"**：给高危加必问闸时，一刀切拦掉了用户显式授权（`settings:allow`、`session:prefix`、hook），当场 18 个测试变红。正确做法是按 `Decision.rule` 区分"有没有人给过依据"（见 `enforce_strict_review`）。
3. **双后端**：开发服务（:8000/:3000）与桌面版是两个库，用户看不到我跑的东西。这是"感觉乱"的主因。
4. **手写构造漏字段**：`project_out()` 手拼 `ProjectOut`，新字段有默认值 → 永远显示 False/0，实测踩过。

## 五、关键路径

```
services/api/                     后端（FastAPI，4332 测试）
  src/blank_api/services/tools/gateway.py     ← 审批门（本轮改动点）
  src/blank_api/services/permissions.py       ← Decision / 规则
  scripts/smoke_flow.py                       ← 端到端冒烟（15 项）
web/                              前端（Next.js，330 测试）
check.sh                          一键自检（tsc + 前端单测 + 冒烟）
claude-reverse/report/            三家桌面端逆向档案（Codex/Claude/WorkBuddy）
```

## 六、测试文件现状（待用户确认再动）

- `services/api/tests/`：**319 个 .py，18MB**，按版本号分组（`_v406` 7 个、`_v165` 7 个…），43 个无版本号
- 建议：**归档不删** —— 移到 `tests/_archive/` 并加 `norecursedirs`，回归网保留可随时跑；确认重复的文件再单独删
