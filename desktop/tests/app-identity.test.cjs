"use strict";

/**
 * 应用身份接线：桌面端「默认名字」散落在构建配置、窗口标题、托盘提示、
 * 启动屏字标、安装包文件名和安装脚本里，改一处漏一处就会出现
 * 「快捷方式叫 A、窗口标题叫 B、安装包叫 C」。这里把一致性钉死。
 */

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.join(__dirname, "..");
const read = (name) => fs.readFileSync(path.join(ROOT, name), "utf8");
const pkg = JSON.parse(read("package.json"));
const main = read("main.cjs");
const splash = read("splash.html");
const installer = read("install-local.cjs");

const APP_NAME = "Blank AI";

test("构建产物的应用名统一为 Blank AI", () => {
  assert.equal(pkg.build.productName, APP_NAME);
  assert.equal(pkg.build.nsis.shortcutName, APP_NAME);
  assert.equal(pkg.build.appId, "ai.blank.workbench", "appId 改了会被系统当成另一个应用");
});

test("主进程用同一个常量给窗口标题和托盘提示命名", () => {
  assert.match(main, new RegExp(`const APP_NAME = "${APP_NAME}"`));
  assert.match(main, /title: APP_NAME/);
  assert.match(main, /tray\.setToolTip\(APP_NAME\)/);
  assert.match(main, /title: `\$\{APP_NAME\} · 启动中`/);
  assert.doesNotMatch(main, /title: "空白格/, "窗口标题不能再硬编码中文老名字");
});

test("前端 HTML title 不能把窗口标题改回中文老名字", () => {
  // Electron 窗口标题取的是页面 <title>，会盖掉 BrowserWindow 的 title。
  // 之前只改 main.cjs 没用：任务栏仍显示「空白格 bot」。
  const layout = fs.readFileSync(
    path.join(ROOT, "..", "web", "src", "app", "layout.tsx"),
    "utf8",
  );
  assert.match(layout, new RegExp(`title: "${APP_NAME}"`));
});

test("启动屏字标与页面标题跟应用名一致，宣传语仍是自有的", () => {
  assert.match(splash, /<div class="wordmark">Blank AI<\/div>/);
  assert.match(splash, /<title>Blank AI · 启动中<\/title>/);
  assert.match(splash, /id="slogan">从空白开始，到完成为止</);
  assert.doesNotMatch(splash, /未至之境|未来之境|Into the Unknown/);
});

test("安装包文件名与 install-local.cjs 的查找规则保持同步", () => {
  // 版本号只从 package.json 读：写死一个字符串会让「发版改版本」这件事每次都
  // 配上一条假红，而它还偏偏长得像真的打包失败。
  const version = pkg.version;
  const installerName = pkg.build.nsis.artifactName.replace("${version}", version);
  const portableName = pkg.build.portable.artifactName.replace("${version}", version);
  const escaped = version.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  assert.match(installerName, new RegExp(`^Blank-AI-安装版-${escaped}\\.exe$`));
  assert.match(portableName, new RegExp(`^Blank-AI-便携版-${escaped}\\.exe$`));
  const rule = installer.match(/^\s*\.filter\(\(name\) => (\/.*?\/)\.test\(name\)/m);
  assert.ok(rule, "install-local.cjs 里要有按文件名找最新安装包的规则");
  // Node 侧直接构造同一个正则来验：改了 artifactName 却忘了改规则，这里会红。
  const literal = rule[1];
  const body = literal.slice(1, literal.lastIndexOf("/"));
  const flags = literal.slice(literal.lastIndexOf("/") + 1);
  const re = new RegExp(body, flags);
  assert.ok(re.test(installerName), "install-local.cjs 必须能找到新命名的安装包");
});
