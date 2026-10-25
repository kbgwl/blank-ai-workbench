# blank-ai-cli

空白格 AI 工程操作台的命令行入口。

```bash
npm install -g blank-ai-cli
blank
```

> 当前仓库已完成 CLI 包结构和本地启动器；发布到 npm 前需要先在发布环境执行 `npm publish`。源码开发阶段可直接在仓库根目录运行下面的命令。

本地开发时可以在仓库根目录运行：

```bash
node packages/cli/src/index.mjs
node packages/cli/src/index.mjs doctor
node packages/cli/src/index.mjs doctor --sandbox
```

`doctor --sandbox` 会用后端 Python 真跑八条边界探针（读/写越界、注册表、出网、
进程数、自放大、进程树继承、证据包同源），并把结果和这次执行的账本逐条对质。
本机没有 Docker 时，要测 `job_only` 边界需设置 `BLANK_SANDBOX_ALLOW_LOCAL=1`。

可选功能按需启用：

```bash
blank add brainstorm
blank list
blank remove brainstorm
```
