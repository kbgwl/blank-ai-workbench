/**
 * 本机覆盖安装（重装 / 自测用）。
 *
 * 为什么要有这个脚本：NSIS 的 `/D=<目录>` 必须原样传给安装器，而 bash 和 cmd 都会改它。
 *   - git-bash（MSYS）：参数里的反斜杠会被吃掉，`/D=D:\a\b` 变成 `/D=D:ab`，
 *     安装器于是在 D 盘上创建一个叫 `ab` 的畸形目录；
 *   - `start /wait ... /D=...`：`/D` 会被 start 自己截走当工作目录，安装器收不到。
 * 两种都试过，都真的装错了地方。node 是原生 Windows 进程，参数透传不改，所以用它。
 *
 * 用法：
 *   node install-local.cjs                       # 用 dist 里最新的安装版，目录取下面的配置
 *   node install-local.cjs <setup.exe> <目标目录>
 *   BLANK_INSTALL_DIR=D:\path node install-local.cjs
 *
 * 注意 NSIS 只认反斜杠：`/D=D:/a/b` 会被它把斜杠整个吃掉，装到 `D:ab` 去。
 * 而反斜杠从 git-bash 传进来又会被吃掉——两条路都通不了。所以正常用法是写
 * `install-target.local.json`（`{"dir": "D:\\a\\b"}`，JSON 里的转义是给解析器的，
 * node 读出来就是真反斜杠），脚本自己读这个文件，不经过任何 shell。
 */
const { spawnSync } = require("node:child_process");
const { existsSync, readdirSync, readFileSync, statSync } = require("node:fs");
const path = require("node:path");

function latestInstaller() {
  const dir = path.join(__dirname, "dist");
  if (!existsSync(dir)) return null;
  const candidates = readdirSync(dir)
    .filter((name) => /^空白格-安装版-.*\.exe$/.test(name) && !name.includes("__uninstaller"))
    .map((name) => path.join(dir, name))
    .sort((a, b) => statSync(b).mtimeMs - statSync(a).mtimeMs);
  return candidates[0] ?? null;
}

/** 目标目录也可以写在 install-target.local.json 里，避开 shell 对反斜杠的处理。 */
function fileTarget() {
  const file = path.join(__dirname, "install-target.local.json");
  if (!existsSync(file)) return null;
  try {
    return JSON.parse(readFileSync(file, "utf8")).dir ?? null;
  } catch (error) {
    console.error(`读 ${file} 失败：${error.message}`);
    return null;
  }
}

const setup = process.argv[2] || latestInstaller();
const target = process.argv[3] || process.env.BLANK_INSTALL_DIR || fileTarget();

if (!setup || !existsSync(setup)) {
  console.error("找不到安装包：用 dist 里的安装版，或把路径作为第一个参数传进来。");
  process.exit(2);
}
if (!target || !path.isAbsolute(target)) {
  console.error("请给出绝对路径的安装目录（第二个参数 / BLANK_INSTALL_DIR / install-target.local.json）。");
  console.error("覆盖安装会先删掉该目录下的旧文件，所以这里不接受相对路径。");
  process.exit(2);
}

console.log(`安装包 ${setup}`);
console.log(`目标   ${target}`);

const result = spawnSync(setup, ["/S", `/D=${target}`], { stdio: "inherit", windowsHide: true });
if (result.status !== 0) {
  console.error(`安装器退出码 ${result.status}${result.error ? " " + result.error.message : ""}`);
  process.exit(result.status ?? 1);
}
console.log("安装完成");
