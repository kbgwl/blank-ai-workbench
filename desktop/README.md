# 桌面壳（Electron）

把静态导出的前端装进一个 Windows 桌面应用。设计规格见 [`../docs/APP-DESIGN.md`](../docs/APP-DESIGN.md)。

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

不设 `ELECTRON_CACHE` 时二进制的默认缓存会落到 C 盘。

## 运行

```bash
# 1) 先出前端静态产物
BLANK_DESKTOP_BUILD=1 NEXT_PUBLIC_API_BASE=http://127.0.0.1:8000 pnpm -C web build

# 2) 起壳
cd desktop && npm start
```

## 环境变量

| 变量 | 作用 |
|---|---|
| `BLANK_SHOT=<png 路径>` | 自检：等界面渲染出内容后截图并退出 |
| `BLANK_PROBE=1` | 自检：从渲染进程直连一次后端，把结果打进日志（排查跨源） |
| `BLANK_DISABLE_GPU=1` | 无 GPU 环境（CI / 远程会话 / 部分虚拟化）改用软件渲染 |
| `BLANK_NO_SANDBOX=1` | 配合上一个；受限环境里 GPU 进程会反复崩溃 |
| `BLANK_BACKEND_HEALTH` | 覆盖后端健康检查地址 |

> ⚠️ **在本机用脚本启动时必须屏蔽 `ELECTRON_RUN_AS_NODE`** —— WorkBuddy 自身会注入它，
> 不屏蔽的话 Electron 会退化成纯 Node：没有 GUI、`require("electron")` 拿到的只是
> npm 包导出的路径字符串。用 `env -u ELECTRON_RUN_AS_NODE npm start`。

## 结构

- `main.cjs` —— 主进程。自定义 `app://` 协议承载静态站（Next 导出的资源是绝对路径，
  `file://` 会解析到盘符根目录；客户端路由也需要兜底到对应 `.html`），并做了目录穿越防护。
- `preload.cjs` —— 沙箱模式下 preload 必须是 CommonJS；只暴露壳自身元信息。
  后续「重启本地服务 / 看日志 / 选工作区目录」等壳能力加在这里。

## 尚未实现

后端进程守护与「服务未启动」专屏、托盘、全局快捷键、单实例、PyInstaller 打包后端、出安装包。
见 APP-DESIGN 第 9 节。
