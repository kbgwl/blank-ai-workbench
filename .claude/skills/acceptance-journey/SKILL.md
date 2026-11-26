---
name: acceptance-journey
description: 运行 kbgai 执行式界面验收 journey，验证首屏、动作与刷新持久化后再交付
---

# 执行式界面验收 journey

## 触发条件

当改动涉及 `web/src/**`、`scripts/acceptance.py`、`acceptance/**`、`check.sh`、CI 工作流，或准备交付时使用本 Skill。

## 步骤

1. 确认当前目录是仓库根目录 `E:\projects\kbgai\kbgai`。
2. 在仓库根目录执行权威入口命令，不要临时拼接：

```bash
PYTHONUTF8=1 "E:/projects/kbgai/kbgai/services/api/.venv/Scripts/python.exe" scripts/acceptance.py --journey acceptance/kanban.json --page acceptance/fixtures/kanban.html
```

3. 需要完整交付门禁时执行 `bash check.sh`；若环境无真实 bash，逐条执行脚本内命令并记录未执行项。

## Output

输出必须是一份可复核的验收记录：退出码、断言条数、第一条失败项（如有），以及是否出现
`结论：通过：打开有东西，动作有变化，刷新还在`。交付说明里必须引用这份记录，不能只写“已测试”。

## Validation

- 打开页面后必须有可见断言结果，不能只是 HTTP 200。
- journey 中的添加、拖拽、刷新、过滤动作必须逐条产生变化记录。
- 刷新后状态必须仍存在。
- 期望输出包含 `结论：通过：打开有东西，动作有变化，刷新还在`。
- 任一断言失败即不得声称验收通过，记录第一条失败项后再定位。

## 与快速自检的关系

`check.sh` 默认调用同一 journey；`--no-acceptance` 仅用于明确排除界面改动的最小检查。交付前不得跳过。
