/**
 * 预加载脚本：只暴露壳自身的元信息与少数必须回主进程的能力，不给渲染进程任何 Node 能力。
 * sandbox 模式下 preload 必须是 CommonJS，所以用 .cjs。
 * 后续「重启本地服务 / 查看日志」等壳能力继续加在这里。
 */
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("blankDesktop", {
  platform: process.platform,
  versions: {
    electron: process.versions.electron,
    chrome: process.versions.chrome,
  },
  /**
   * 打开系统自带的目录选择框，返回绝对路径；用户取消时返回空串。
   *
   * 只暴露"选一个目录"这一个动作，不接受任意频道名做参数：preload 一旦变成通用
   * 转发层，sandbox 就白开了——渲染进程等于又拿回了主进程的全部 IPC。
   */
  pickDirectory: async (current) => {
    const result = await ipcRenderer.invoke("blank:pick-directory", {
      current: String(current ?? ""),
    });
    return String(result?.path ?? "");
  },
});
