# @blank-ai/cli

空白格 AI 工程操作台的命令行入口。

```bash
npm install -g @blank-ai/cli
blank
```

> 当前仓库已完成 CLI 包结构和本地启动器；发布到 npm 前需要先在发布环境执行 `npm publish`。源码开发阶段可直接在仓库根目录运行下面的命令。

本地开发时可以在仓库根目录运行：

```bash
node packages/cli/src/index.mjs
node packages/cli/src/index.mjs doctor
```

可选功能按需启用：

```bash
blank add brainstorm
blank list
blank remove brainstorm
```
