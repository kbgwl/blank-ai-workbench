# 贡献指南

感谢你参与空白格。项目当前处于早期开发阶段，欢迎提交 bug、设计建议、文档改进和代码贡献。

## 开始开发

```bash
pnpm install
pnpm dev
```

前端检查：

```bash
pnpm typecheck:web
pnpm lint:web
pnpm build:web
```

后端测试需要 `uv` 和 Docker，详见 [README](README.md)。

团队链路（Bot、审批、上下文管家、全文检索）有独立的端到端门禁：

```bash
pnpm e2e:team            # 起隔离的 stub 模型 + 后端 + 前端，跑 S0–S16 与 C1–C9
pnpm e2e:team --skip-web # 没有 Chrome 的机器：只跑后端链路，报告里会写明 UI 阶段未执行
```

它自己拉三个端口（默认 8097/8055/3005）和一个隔离 SQLite 库，产物落在
`evals/team_e2e/.work/`（已 gitignore）；端口被占用会直接退出，不会去动你正在用的实例。
退出码 0 全通过、1 有判定失败、3 有阶段被跳过。改这几块代码时要求跑通它，
理由写在 `evals/team_e2e/run.mjs` 头部：这类结论要跨进程才成立，单测全绿也能藏住。
CI 的 `team-e2e` job 跑的是 `--skip-web` 那一档，浏览器点验仍在本地补。

## 提交规范

- 一个 Pull Request 聚焦一个问题。
- 不要提交 `.env`、API Key、数据库文件、运行日志或生成目录。
- 涉及 Agent、工具权限、沙箱和文件写入的改动必须补充测试或说明风险。
- UI 改动请说明验证过的浏览器和视口尺寸。

## Pull Request 内容

请写清楚：改了什么、为什么改、如何验证、是否影响安全边界或数据格式。
