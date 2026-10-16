# 桌面壳（Electron）

把静态导出的前端装进一个 Windows 桌面应用，并守护本机后端。设计规格见
[`../docs/APP-DESIGN.md`](../docs/APP-DESIGN.md)。

## 为什么是 Electron

见 APP-DESIGN 第 2 节。简要：本机没有 Rust 工具链，而 Windows 上 Tauri 还需要
MSVC C++ Build Tools（多 GB、大量写 C 盘）；Electron 零新工具链，且自带 Chromium，
目标机没有渲染依赖。

## 为什么用 npm 而不是 pnpm，且刻意不进 workspace

`desktop/` 不加入 `pnpm-workspace.yaml`：否则 CI 的 `pnpm install --frozen-lockfile`
每次都会拉 ~150MB 的 Electron。它用 npm 独立管理依赖，与主工作区互不干扰。

## 安装依赖（缓存必须落 E 盘）

```bash
cd desktop
ELECTRON_CACHE=E:/env/electron-cache \
ELECTRON_BUILDER_CACHE=E:/env/electron-builder-cache \
npm install
```

不设这两个变量时，二进制与打包工具链的默认缓存会落到 C 盘。

## 开发态运行

```bash
# 1) 先出前端静态产物
BLANK_DESKTOP_BUILD=1 NEXT_PUBLIC_API_BASE=http://127.0.0.1:8000 pnpm -C web build

# 2) 起壳（后端由壳自己探测/拉起）
cd desktop && npm start
```

## 出包

```bash
cd desktop
npm run dist            # 便携版 + 安装包
npm run dist:portable   # 只要便携版
```

产物在 `desktop/dist/`：

| 文件 | 说明 |
|---|---|
| `空白格-便携版-<版本>.exe` | 单文件，双击即用（约 105MB） |
| `空白格-安装版-<版本>.exe` | NSIS 安装包，可换安装目录（约 106MB） |
| `win-unpacked/` | 免打包目录，调试用 |

进包内容：`resources/web`（前端静态站）、`resources/backend/api`（后端源码，
已排除 `.venv` / `data` / `tests`）、`resources/backend/runtime`（**自带的 Python 运行时**）。

> ⚠️ **出包前必须清掉 `NODE_OPTIONS`**：本机 WorkBuddy 会注入
> `NODE_OPTIONS=--require=...node-language-shim.cjs`，它挂钩了文件删除，会让
> electron-builder 清理旧产物时失败（报 `node-safe-delete-shim.cjs` 的栈）。
> 用 `env -u NODE_OPTIONS npm run dist` 或先手工删掉 `dist/`。

## 后端的解释器解析优先级

壳启动时按顺序找，选第一个可用的：

1. `BLANK_BACKEND_CMD`（显式指定，调试用）
2. **包内自带 Python**（`resources/backend/runtime/python.exe`）
3. 包内 / 项目内 `.venv`
4. 本机 `uv`（会为后端目录建 venv，首次较慢）
5. 本机 `python`

打包态数据库落 `%APPDATA%\blank-ai-workbench-desktop\blank.db`，首次启动自动跑 alembic 迁移。

实测：在**把 Python 与 uv 从 PATH 里完全移除**的环境下，壳用包内自带运行时启动了后端，
并在全新的 `%APPDATA%` 数据库上把 10 个迁移从头跑完 —— 这就是新用户首次打开的经历。

## 重建自带的 Python 运行时

运行时是构建产物（约 125MB），**不入库**（`.gitignore` 里排除了 `desktop/build/runtime/`）。
需要重新生成时：

```bash
# 1) 用 uv 拉一个可搬运的 CPython（python-build-standalone）
UV_PYTHON_INSTALL_DIR=E:/env/kbgai-runtime uv python install 3.12

# 2) 去掉 PEP 668 标记 —— 随包分发的运行时不需要它，否则 uv/pip 拒绝安装
rm -f E:/env/kbgai-runtime/cpython-3.12.14-windows-x86_64-none/Lib/EXTERNALLY-MANAGED

# 3) 把后端本身（含全部依赖）装进这个运行时
uv pip install \
  --python E:/env/kbgai-runtime/cpython-3.12.14-windows-x86_64-none/python.exe \
  services/api

# 4) 拷进 build/ 供 electron-builder 打包
cp -r E:/env/kbgai-runtime/cpython-3.12.14-windows-x86_64-none desktop/build/runtime
```

注意版本号（`3.12.14`）会随 uv 更新变化，按实际目录名替换。
后端 `requires-python` 是 `>=3.12`，升到 3.13 也可以。

## 环境变量

| 变量 | 作用 |
|---|---|
| `BLANK_SHOT=<png 路径>` | 自检：等界面渲染出内容后截图并退出 |
| `BLANK_SHOT_DELAY=<ms>` | 截图前再等一会，用于观察需要时间恢复的状态（默认 800） |
| `BLANK_PROBE=1` | 自检：从渲染进程直连一次后端并打日志；截图前后各读一次健康徽标文字 |
| `BLANK_DISABLE_GPU=1` | 无 GPU 环境（CI / 远程会话 / 部分虚拟化）改用软件渲染 |
| `BLANK_NO_SANDBOX=1` | 配合上一个；受限环境里 GPU 进程会反复崩溃 |
| `BLANK_BACKEND_CMD` | 覆盖后端可执行文件（配 `-m uvicorn ...` 参数使用） |
| `BLANK_BACKEND_PORT` | 后端端口，默认 8000 |
| `BLANK_BACKEND_HEALTH` | 覆盖健康检查地址 |

> ⚠️ **在本机用脚本启动时必须屏蔽 `ELECTRON_RUN_AS_NODE`** —— WorkBuddy 自身会注入它，
> 不屏蔽的话 Electron 会退化成纯 Node：没有 GUI、`require("electron")` 拿到的只是
> npm 包导出的路径字符串。用 `env -u ELECTRON_RUN_AS_NODE npm start`。

## 结构

- `main.cjs` —— 主进程：`app://` 协议承载静态站（Next 导出的资源是绝对路径，
  `file://` 会解析到盘符根目录；客户端路由也需要兜底到对应 `.html`），带目录穿越防护；
  后端守护（探测 → 迁移 → 拉起 → 健康检查 → 故障对话框 → 退出回收）；自检开关。
- `preload.cjs` —— 沙箱模式下 preload 必须是 CommonJS；只暴露壳自身元信息。
- `build/icon.svg` + `build/icon.ico` —— 品牌图标（藏青底 + 2×2 网格点亮一格）。
  `icon.ico` 由 `icon.svg` 渲染 9 个尺寸后打包而成，生成脚本见交付说明。

## 尚未实现

托盘、全局快捷键、单实例、自动更新、代码签名。见 APP-DESIGN 第 9、10 节。

> 目前未配代码签名证书，打出来的 exe 会被 Windows SmartScreen 拦成"未知发布者"，
> 用户需要点"仍要运行"。正式分发前要解决 —— 见 APP-DESIGN 第 10.4 节。
