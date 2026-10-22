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
  /**
   * 在系统文件管理器里打开一个路径。返回空串表示打开成功，非空是要展示给用户的错误。
   *
   * 和 pickDirectory 一样只暴露一个动作：一旦做成"任意频道转发"，sandbox 就白开了。
   */
  openPath: async (target) => {
    const result = await ipcRenderer.invoke("blank:open-path", { path: String(target ?? "") });
    return result?.ok ? "" : String(result?.reason ?? "打开失败");
  },
  pickDirectory: async (current) => {
    const result = await ipcRenderer.invoke("blank:pick-directory", {
      current: String(current ?? ""),
    });
    return String(result?.path ?? "");
  },
});
