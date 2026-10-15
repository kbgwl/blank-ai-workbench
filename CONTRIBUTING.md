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

## 提交规范

- 一个 Pull Request 聚焦一个问题。
- 不要提交 `.env`、API Key、数据库文件、运行日志或生成目录。
- 涉及 Agent、工具权限、沙箱和文件写入的改动必须补充测试或说明风险。
- UI 改动请说明验证过的浏览器和视口尺寸。

## Pull Request 内容

请写清楚：改了什么、为什么改、如何验证、是否影响安全边界或数据格式。
