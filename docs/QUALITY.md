# 评测判分口径（evals/compare.mjs）

> 这里解释“分数怎么来的”，避免把不同模型、不同环境的差异误读成产品能力。

## 一、五项权重

| 项 | 权重 | 机器怎么判 |
|---|---:|---|
| 正确性 | 45% | 样例的隐藏 `checks` 全部退出码 0，且 agent 至少改了一个文件 |
| 期望文件命中 | 20% | `expect_files` 是否都被改动；以 `/` 结尾的项按目录前缀匹配 |
| 最小改动 | 15% | 改动文件里有多少不在期望集合内；0 个多余得满分 |
| 自证 | 10% | agent 是否真的执行过验证命令（不是只在文本里说“已修复”） |
| 成本 | 10% | 同一题里 token 最少者得满分，其余按 `最少/本家` 折算 |

总分 100。`unavailable`（模型/账号/CLI 不可用）不参与平均分和通过率，
表格里显示“—”，不能当成 0 分。

## 二、隐藏验收

样例可带：

```json
"hidden_files": { "tests/test_hidden_x.py": "..." },
"checks": ["python -m pytest -q"]
```

- `hidden_files` 在 agent 结束、改动快照之后才写入工作区，agent 看不到；
- `checks` 由评测器执行，不看 agent 自述；
- 有 `checks` 时以它为准；没有时才回落 `verify`；
- 隐藏测试不计入 agent 的改动文件，避免“自己写测试自己过”。

## 三、环境隔离

- 工作区放在仓库外，避免 Claude/Codex 向上读取本项目的 `CLAUDE.md` /
  `AGENTS.md`；
- Claude Code 使用 `--safe-mode` 关闭用户级 CLAUDE.md、skills、hooks；
- Codex 显式 `-s workspace-write`，否则非交互默认沙箱可能只读；
- `.blank/`、`.blank_backups/`、`node_modules/` 等目录不计改动。

## 四、已知局限

- Claude Code 的 JSON 输出不含工具明细，自证只能从最终文本判断，可能低估；
- 自证阈值按 `verify` 命令首词匹配，复杂命令需要更细的解析；
- 成本只算 token，不算缓存命中后的实际计费和重试；
- 小样例通过不等于大型项目能交付；浏览器/桌面任务仍需独立的 browser_check。
