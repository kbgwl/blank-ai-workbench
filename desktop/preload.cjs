/**
 * 预加载脚本：只暴露壳自身的元信息，不给渲染进程任何 Node 能力。
 * sandbox 模式下 preload 必须是 CommonJS，所以用 .cjs。
 * 后续「重启本地服务 / 查看日志 / 选择工作区目录」等壳能力都加在这里。
 */
const { contextBridge } = require("electron");

contextBridge.exposeInMainWorld("blankDesktop", {
  platform: process.platform,
  versions: {
    electron: process.versions.electron,
    chrome: process.versions.chrome,
  },
});
