/**
 * 预加载脚本：只暴露壳自身的元信息与少数必须回主进程的能力，不给渲染进程任何 Node 能力。
 * sandbox 模式下 preload 必须是 CommonJS，所以用 .cjs。
 * 后续「重启本地服务 / 查看日志」等壳能力继续加在这里。
 */
const { contextBridge, ipcRenderer } = require("electron");

/**
 * 后端基址在运行时取一次。静态导出的前端把构建时的地址烘焙进了包，
 * 换 `BLANK_BACKEND_PORT` / `BLANK_API_BASE` 后只能靠这里覆盖，否则界面会一直打旧端口。
 * 取不到就留空，让前端退回烘焙值（浏览器直接打开导出的页面时没有壳）。
 */
let apiBase = "";
try {
  apiBase = String(ipcRenderer.sendSync("blank:api-base") ?? "");
} catch {
  /* 主进程未注册该频道时保持空串 */
}

contextBridge.exposeInMainWorld("blankDesktop", {
  platform: process.platform,
  apiBase,
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
