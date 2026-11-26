---
name: check-journey
description: 仅运行 kbgai 执行式界面验收 journey，并给出可复核的通过/失败结论
---

# /check-journey

1. 确认当前工作目录是 `E:\projects\kbgai\kbgai`。
2. 读取 `.qoder/skills/acceptance-journey/SKILL.md`，以其中命令为唯一权威入口。
3. 执行：

```bash
PYTHONUTF8=1 "E:/projects/kbgai/kbgai/services/api/.venv/Scripts/python.exe" scripts/acceptance.py --journey acceptance/kanban.json --page acceptance/fixtures/kanban.html
```

4. 记录退出码、断言条数，以及是否出现 `结论：通过：打开有东西，动作有变化，刷新还在`。
5. 失败时只报告第一条失败断言，不静默略过，也不替换成更容易通过的入口。
