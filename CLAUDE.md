# Claude Code 项目入口

本仓库的跨工具权威规则是根目录 `AGENTS.md`，Claude Code 必须先读它，不要只依赖本文件。
本文件只补充 Claude 专属入口；与 `AGENTS.md` 冲突时以 `AGENTS.md` 为准。

## Claude 专属资产

- Skill：`.claude/skills/acceptance-journey/SKILL.md`
- Command：`.claude/commands/check-journey.md`
- Subagent：`.claude/agents/acceptance-reviewer.md`
- Hook：`.claude/settings.json` + `.claude/hooks/acceptance-record-check.mjs`
- 留存验收记录：`docs/validation/acceptance-2026-11-21.md`

## 交付前必走

涉及 `web/src/**`、`scripts/acceptance.py`、`acceptance/**`、`check.sh`、CI 工作流或准备交付时：

1. 用 `/check-journey` 或对应 Skill 跑执行式界面验收 journey。
2. 让 `acceptance-reviewer` 复核留存记录；没有晚于最后改动的通过记录，不得声称已验收。
3. Stop Hook 会检查留存记录结论；exit 1 表示交付记录缺失或不完整。

完整的跨工具检查表、失败定位规则和本地工具链路径都在 `AGENTS.md`。
