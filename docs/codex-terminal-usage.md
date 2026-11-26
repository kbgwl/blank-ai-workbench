# Codex 终端版使用指南（本机 v0.171.0）

> 适用：`E:\env\bin\codex.cmd`（Codex CLI 0.171.0，本机配置为
> `model=spark-x2.6`、`provider=spark`）。  
> 命令以本机 `codex --help` / `codex exec --help` 为准；TUI 里的斜杠命令
> 以输入 `/` 后的实时列表和 `?` 面板为准（版本间会增删）。

## 一、两种用法

### 1. 交互式 TUI（推荐日常使用）

```powershell
cd E:\projects\kbgai\kbgai
E:\env\bin\codex.cmd
```

也可以带初始任务：

```powershell
E:\env\bin\codex.cmd "检查这个项目的测试并给出结论"
```

常用启动参数：

| 参数 | 作用 |
|---|---|
| `-C <目录>` | 指定工作根目录 |
| `-m <模型>` | 临时换模型，例如 `-m spark-x2.6` |
| `-s read-only / workspace-write / danger-full-access` | 沙箱档位 |
| `-a on-request / never` | 何时要求人工审批 |
| `--search` | 打开内置联网搜索 |
| `--no-alt-screen` | 不用备用屏幕，保留终端滚动历史 |
| `--worktree` | 在独立 git worktree 里开会话 |
| `--add-dir <目录>` | 额外可写目录 |

### 2. 非交互 exec（脚本/自动化）

```powershell
E:\env\bin\codex.cmd exec "列出当前目录的文件，不要修改"
E:\env\bin\codex.cmd e "同上"                  # exec 别名
```

常用参数：

| 参数 | 作用 |
|---|---|
| `--json` | 输出 JSONL 事件，适合程序解析 |
| `-o <文件>` | 把最后一条回复写入文件 |
| `--skip-git-repo-check` | 允许在非 git 目录运行 |
| `--ephemeral` | 不落盘会话 |
| `-C <目录>` | 工作根目录 |
| `-s read-only` | 只读审查（不会改文件） |
| `-m <模型>` | 临时模型 |
| `-c key=value` | 临时覆盖 config.toml |
| `-` | 从 stdin 读 prompt（长 prompt 用这个） |

长 prompt 示例：

```powershell
Get-Content -Raw prompt.txt | codex.cmd exec --json -s read-only -C E:\projects\kbgai\kbgai -
```

## 二、TUI 里的基本操作

| 操作 | 说明 |
|---|---|
| `?` | 打开快捷键帮助面板 |
| `Esc` | 中断当前回合 |
| `Ctrl+C` | 中断/退出 |
| `Ctrl+G` | 打开 `$VISUAL`/`$EDITOR` 写长 prompt |
| `Ctrl+O` | 复制最近一次输出 |
| `Ctrl+L` | 清屏，会话不丢 |
| `↑` | 翻历史输入 |
| `Shift+Enter` | 多行输入（终端支持时） |
| `Tab` | 补全斜杠命令 |
| `!cmd` | 直接跑一条本地 shell 命令，不经过模型 |
| `@路径` | 模糊搜索工作区文件 |
| `←` | 打开 agents/会话面板（截图底部的 `← for agents`） |

## 三、TUI 常用斜杠命令

> 只输入在行首的 `/` 才会被识别；输入后可用 `Tab` 补全。不同版本可能
> 增减，以 `?` 面板为准。

| 命令 | 用途 |
|---|---|
| `/model` | 切换模型 |
| `/status` | 查看当前会话/模型/用量状态 |
| `/usage` | 查看用量 |
| `/permissions`、`/approvals` | 调整权限/审批策略 |
| `/statusline`、`/theme`、`/keymap`、`/vim` | 界面、主题、键位、Vim 模式 |
| `/compact` | 压缩上下文 |
| `/clear` | 清空当前上下文 |
| `/new` | 新会话 |
| `/diff` | 查看当前改动 |
| `/mention` | 引用文件/内容 |
| `/ide` | 连接 IDE |
| `/copy` | 复制输出 |
| `/plan` | 进入计划模式 |
| `/goal` | 设定/查看目标 |
| `/review` | 代码审查 |
| `/agents`、`/subagents` | 查看/切换代理 |
| `/mcp`、`/apps`、`/plugins`、`/skills`、`/hooks` | MCP、应用、插件、技能、钩子 |
| `/ps`、`/stop` | 查看/停止后台终端任务 |
| `/export` | 导出会话为 Markdown（截图提示） |
| `/exit`、`/quit` | 退出 |

## 四、代码审查

交互式：

```text
在 TUI 输入 /review
```

非交互：

```powershell
# 审查未提交改动
codex.cmd review --uncommitted -C E:\projects\kbgai\kbgai

# 对比某个分支
codex.cmd review --base main -C E:\projects\kbgai\kbgai

# 审查某个提交
codex.cmd review --commit <SHA> -C E:\projects\kbgai\kbgai

# 自定义审查要求
codex.cmd review "只读审查 B6 改动，按会出错/应该改/可忽略输出"
```

## 五、会话管理

```powershell
codex.cmd agents                 # 打开会话/代理面板
codex.cmd resume                 # 选择历史会话恢复
codex.cmd resume --last          # 恢复最近一次
codex.cmd resume <session-id>    # 恢复指定会话
codex.cmd fork --last            # 从最近会话分叉
codex.cmd queue "补充要求"        # 给已有会话排队一条消息
codex.cmd archive <id|name>      # 归档
codex.cmd delete <id|name>       # 永久删除
```

## 六、本机配置与坑

- `codex.cmd` 会设置 `CODEX_HOME=E:\env\codex\home`；模型配置在
  `E:\env\codex\home\config.toml`，当前是 `spark-x2.6` + `provider=spark`。
- **TUI 与 `exec` 不是同一套工具**：TUI 里可见 `node_repl` 等插件工具；
  `codex exec` 走 PowerShell 工具，中文输出可能乱码，`apply_patch` 也可能
  因 here-string/沙箱问题失败。不要在 `exec` 失败时直接断言“Codex 不能用”。
- 工作目录要在启动前 `cd` 到项目根；在会话里 `cd` 不等同于启动目录。
- 需要看截图/图片时，使用支持 image input 的模型；当前 spark 的 Responses
  端点不支持图片（按本机 README 记录）。
- `--dangerously-bypass-approvals-and-sandbox` 只在外部已隔离的临时环境使用；
  普通项目优先 `workspace-write` 或 `read-only`。
- 终端中文乱码时先检查代码页；PowerShell 5.1 的输出编码与 UTF-8 文件不是
  一回事。

## 七、本项目的推荐用法

1. **日常编码/审查**：在项目根启动 TUI，先 `?` 看键位，再用 `/model` 确认
   `spark-x2.6`、`/permissions` 确认审批档位。
2. **只读质量审查**：用 `codex review --uncommitted` 或 TUI `/review`；
   不要让它改文件。
3. **自动化脚本**：用 `codex exec --json`，并显式 `-s read-only` 或
   `-s workspace-write`；长 prompt 从 stdin 传。
4. **不要把 TUI 的成功当成 exec 自动化也成功**；两者的工具/编码链路不同，
   评测时要分别记录。

## 参考

- 本机 `codex --help` / `codex exec --help` / `codex review --help`（v0.171.0）
- [Codex CLI slash commands（ChatGPT Learn）](https://learn.chatgpt.com/docs/cli/slash-commands)
- [中文镜像：Codex CLI 斜杠命令](https://github.com/crasuna/openai-dev-docs-cn-mirror/blob/main/docs/mirror/codex/cli/slash-commands.md)
- [Codex commands cheat sheet](https://github.com/jqueryscript/codex-commands-cheat-sheet/blob/main/README.md)
