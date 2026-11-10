#!/usr/bin/env node
/**
 * 生成发布产物的 CycloneDX 1.5 SBOM，并用**实际锁文件**自检。
 *
 * 口径：
 * * npm 组件来自 `desktop/package-lock.json` 的 `packages`（含传递依赖与构建依赖）；
 * * Python 组件来自 `services/api/uv.lock` 的 `[[package]]` 段；
 * * 安装包的 sha256/size 写进 metadata.properties，SBOM 与二进制因此能对上号。
 *
 * 为什么不用第三方 SBOM 工具：这一步要能进 CI、无网络、不依赖本机装了什么。
 * 锁文件是仓库里的真物；组件条目就是从它们逐行读出来的，不是"按 package.json 猜"。
 *
 * 用法：
 *   node scripts/make_sbom.mjs [--out desktop/dist/sbom.json] [--artifact <安装包路径>]
 *
 * 出口码：0=生成并通过自检；1=自检失败（不写文件）；2=锁文件/产物缺失。
 */
import { createHash } from "node:crypto";
import { existsSync, readFileSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";

const ROOT = path.resolve(import.meta.dirname, "..");
const args = process.argv.slice(2);
const argValue = (name) => {
  const index = args.indexOf(name);
  return index >= 0 && args[index + 1] ? args[index + 1] : "";
};

const desktopPkg = JSON.parse(readFileSync(path.join(ROOT, "desktop", "package.json"), "utf8"));
const version = String(desktopPkg.version ?? "");
const lockPath = path.join(ROOT, "desktop", "package-lock.json");
const uvPath = path.join(ROOT, "services", "api", "uv.lock");
const out = path.resolve(ROOT, argValue("--out") || path.join("desktop", "dist", "sbom.json"));
const artifact = path.resolve(
  ROOT,
  argValue("--artifact") || path.join("desktop", "dist", `Blank-AI-安装版-${version}.exe`)
);

const problems = [];
const ok = (label, detail = "") => console.log(`  ok    ${label}${detail ? "  " + detail : ""}`);
const bad = (label, detail = "") => {
  problems.push(label);
  console.log(`  FAIL  ${label}${detail ? "  " + detail : ""}`);
};

for (const [label, file] of [
  ["package-lock.json", lockPath],
  ["uv.lock", uvPath],
  ["安装包", artifact],
]) {
  if (!existsSync(file)) {
    console.error(`找不到${label}：${file}`);
    process.exit(2);
  }
}

function npmPurl(name, componentVersion) {
  const encoded = name.startsWith("@") ? "%40" + name.slice(1) : name;
  return `pkg:npm/${encoded}@${componentVersion}`;
}

function pypiPurl(name, componentVersion) {
  return `pkg:pypi/${name.toLowerCase().replace(/[-_.]+/g, "-")}@${componentVersion}`;
}

let components = [];

// ── npm：从锁文件读，不补、不猜 ────────────────────────────────────────────
const lock = JSON.parse(readFileSync(lockPath, "utf8"));
const packages = lock.packages && typeof lock.packages === "object" ? lock.packages : {};
for (const [key, entry] of Object.entries(packages)) {
  if (!key.startsWith("node_modules/") || !entry || typeof entry !== "object") continue;
  if (entry.link || !entry.version) continue;
  const name = String(entry.name || key.slice(key.lastIndexOf("node_modules/") + "node_modules/".length));
  components.push({
    type: "library",
    name,
    version: String(entry.version),
    purl: npmPurl(name, String(entry.version)),
    properties: [{ name: "blank:source", value: "desktop/package-lock.json" }],
  });
}

// ── Python：uv.lock 是 TOML，但这里只需要每段的 name/version ───────────────
const uvText = readFileSync(uvPath, "utf8");
for (const block of uvText.split(/^\[\[package\]\]\s*$/m).slice(1)) {
  const name = /^name\s*=\s*"([^"]+)"/m.exec(block)?.[1];
  const pkgVersion = /^version\s*=\s*"([^"]+)"/m.exec(block)?.[1];
  if (!name || !pkgVersion) continue;
  components.push({
    type: "library",
    name,
    version: pkgVersion,
    purl: pypiPurl(name, pkgVersion),
    properties: [{ name: "blank:source", value: "services/api/uv.lock" }],
  });
}

// 同一个 name@version 会在锁文件里出现多次（嵌套 node_modules 里的同一份拷贝）。
// SBOM 的组件是"库"不是"安装位置"：按 purl 去重，保留第一次出现的条目。
const seenPurls = new Map();
for (const item of components) {
  if (!seenPurls.has(item.purl)) seenPurls.set(item.purl, item);
}
components = [...seenPurls.values()];

const artifactBuffer = readFileSync(artifact);
const artifactSha = createHash("sha256").update(artifactBuffer).digest("hex");
const artifactSize = statSync(artifact).size;

// ── 自检 ────────────────────────────────────────────────────────────────────
const purls = components.map((item) => item.purl);
new Set(purls).size === purls.length
  ? ok("组件 purl 无重复", `${purls.length} 个`)
  : bad("组件 purl 有重复", `${purls.length} → ${new Set(purls).size}`);

const broken = components.filter(
  (item) => !item.name || !item.version || !item.purl || !/^pkg:/.test(item.purl)
);
broken.length === 0
  ? ok("每个组件都有 name/version/purl")
  : bad("有组件字段不完整", JSON.stringify(broken.slice(0, 3)));

for (const name of Object.keys({ ...desktopPkg.dependencies, ...desktopPkg.devDependencies })) {
  const hit = components.some((item) => item.name === name);
  hit ? ok(`直接依赖在 SBOM 里：${name}`) : bad(`直接依赖缺失：${name}`);
}

components.some((item) => item.name === "electron")
  ? ok("electron 在组件清单里")
  : bad("electron 不在组件清单里");

const pythonCount = components.filter((item) => item.purl.startsWith("pkg:pypi/")).length;
const npmCount = components.length - pythonCount;
pythonCount > 0 && npmCount > 0
  ? ok("npm 与 Python 两侧都有组件", `npm ${npmCount} / pypi ${pythonCount}`)
  : bad("有一侧没有组件", `npm ${npmCount} / pypi ${pythonCount}`);

// 序列号按内容算，构建可复现：同样的组件集合得到同样的 serialNumber。
const digest = createHash("sha256").update(JSON.stringify(components)).digest("hex");
const serial = `urn:uuid:${digest.slice(0, 8)}-${digest.slice(8, 12)}-${digest.slice(12, 16)}-${digest.slice(16, 20)}-${digest.slice(20, 32)}`;

const bom = {
  bomFormat: "CycloneDX",
  specVersion: "1.5",
  serialNumber: serial,
  version: 1,
  metadata: {
    timestamp: new Date().toISOString(),
    component: {
      type: "application",
      name: desktopPkg.name,
      version,
    },
    properties: [
      { name: "blank:artifact:name", value: path.basename(artifact) },
      { name: "blank:artifact:sha256", value: artifactSha },
      { name: "blank:artifact:size", value: String(artifactSize) },
    ],
  },
  components,
};

for (const required of ["bomFormat", "specVersion", "serialNumber", "components"]) {
  required in bom ? null : bad(`SBOM 缺字段 ${required}`);
}
JSON.stringify(bom).includes(artifactSha)
  ? ok("安装包摘要进了 SBOM", artifactSha.slice(0, 12) + "…")
  : bad("安装包摘要没进 SBOM");

console.log(
  `\n组件：npm ${npmCount} + pypi ${pythonCount} = ${components.length}` +
    `\n产物：${path.basename(artifact)}  ${artifactSize} 字节  sha256=${artifactSha}`
);

if (problems.length) {
  console.log(`\n自检失败 ${problems.length} 项，未写文件。`);
  process.exit(1);
}

writeFileSync(out, JSON.stringify(bom, null, 2) + "\n", "utf8");
console.log(`\n已写入 ${out}`);
console.log("自检全部通过");
