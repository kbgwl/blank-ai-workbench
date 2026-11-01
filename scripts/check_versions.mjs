#!/usr/bin/env node
/**
 * 版本号只许有一个真源：`desktop/package.json`。
 *
 * 为什么是它：用户手上那个 exe 的版本、安装包文件名（`空白格-安装版-${version}.exe`）、
 * 托盘/关于面板读的都是它。README 里那句"当前发布版本"如果和它不一致，用户看到的就是
 * 两个版本；`packages/cli` 是另一个对外产物（`npm install -g blank-ai-cli`），
 * README 里连 tarball 名字都写死了 `blank-ai-cli-0.1.0.tgz`，它一升版本 README 就在骗人。
 *
 * 这个脚本把这两处对齐做进 CI，而不是靠"改版本时记得同步改文档"——那种默契历来不成立。
 *
 * 用法：node scripts/check_versions.mjs
 */
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";

const ROOT = path.resolve(import.meta.dirname, "..");
const problems = [];
const ok = (label, detail = "") => console.log(`  ok    ${label}${detail ? "  " + detail : ""}`);
const bad = (label, detail = "") => {
  problems.push(label);
  console.log(`  FAIL  ${label}${detail ? "  " + detail : ""}`);
};

function readJson(rel) {
  const full = path.join(ROOT, rel);
  if (!existsSync(full)) {
    bad(`${rel} 不存在`);
    return null;
  }
  try {
    return JSON.parse(readFileSync(full, "utf8"));
  } catch (error) {
    bad(`${rel} 不是合法 JSON`, String(error.message).split("\n")[0]);
    return null;
  }
}

const SEMVER = /^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/;

const desktop = readJson("desktop/package.json");
if (!desktop) process.exit(1);
const canonical = String(desktop.version ?? "");
SEMVER.test(canonical)
  ? ok("desktop/package.json 版本是合法 semver", canonical)
  : bad("desktop/package.json 版本不是 semver", JSON.stringify(canonical));

// 真源只有一处；根 package.json 里的那份是给工作区工具看的镜像，必须同值。
const rootPkg = readJson("package.json");
if (rootPkg) {
  rootPkg.version === canonical
    ? ok("根 package.json 与真源一致", canonical)
    : bad("根 package.json 版本与 desktop 不一致", `根 ${JSON.stringify(rootPkg.version)} / 真源 ${canonical}`);
}

// README 是用户第一眼看到的地方，它写的版本必须就是包里那个。
const readmePath = path.join(ROOT, "README.md");
if (!existsSync(readmePath)) {
  bad("README.md 不存在");
} else {
  const readme = readFileSync(readmePath, "utf8");
  const stated = readme.match(/当前发布版本：\*\*([^*]+)\*\*/);
  if (!stated) {
    bad("README 没有「当前发布版本：**x.y.z**」这一行", "版本口径必须写在一眼能看到的位置");
  } else if (stated[1].trim() !== canonical) {
    bad("README 写的版本与真源不一致", `README ${stated[1].trim()} / 真源 ${canonical}`);
  } else {
    ok("README 版本与真源一致", canonical);
  }
}

// CLI 是另一个对外产物：它的版本不出现在"当前发布版本"里，但 README 印了 tarball 文件名。
const cli = readJson("packages/cli/package.json");
if (cli) {
  const cliVersion = String(cli.version ?? "");
  ok("packages/cli 版本", cliVersion);
  const readme = existsSync(readmePath) ? readFileSync(readmePath, "utf8") : "";
  const tarball = readme.match(/blank-ai-cli-(\d+\.\d+\.\d+)(?:\.tgz)?/);
  if (!tarball) {
    ok("README 未写死 CLI tarball 版本", "跳过一致性判定");
  } else if (tarball[1] !== cliVersion) {
    bad("README 里的 CLI tarball 版本过期", `README ${tarball[1]} / cli ${cliVersion}`);
  } else {
    ok("README 里的 CLI tarball 版本与 packages/cli 一致", cliVersion);
  }
}

// 安装包文件名由 electron-builder 按 version 生成；写死版本号的地方必须能对上，
// 否则用户下到的文件名和"关于"里显示的不是一个版本。
const artifactNames = [desktop.build?.nsis?.artifactName, desktop.build?.portable?.artifactName].filter(Boolean);
for (const name of artifactNames) {
  if (!name.includes("${version}")) {
    bad("产物文件名没有用 ${version} 模板", name);
  }
}
if (artifactNames.length) ok("产物文件名按版本模板生成", artifactNames.join(" / "));

console.log("");
if (problems.length) {
  console.log(`未通过 ${problems.length} 项：${problems.join(" / ")}`);
  process.exit(1);
}
console.log(`版本口径一致（发布版本 ${canonical}）`);
