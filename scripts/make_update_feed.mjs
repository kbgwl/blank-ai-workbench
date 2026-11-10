#!/usr/bin/env node
/**
 * 生成桌面壳的更新源（feed）文件，并用 `desktop/shell-policy.cjs` 的纯函数自检：
 * 生成的这份 JSON 必须真的能被壳读懂，而不是"看起来像"。
 *
 * 用法：
 *   BLANK_UPDATE_BASE_URL=https://dl.example.com/blank \
 *   BLANK_UPDATE_SIGNING_KEY=E:/tmp/blank-release/update-signing-key.pem \
 *     node scripts/make_update_feed.mjs
 *   node scripts/make_update_feed.mjs --base-url http://127.0.0.1:8099 \
 *     --sign-key E:/tmp/blank-release/update-signing-key.pem \
 *     --sbom desktop/dist/sbom.json --out desktop/dist/latest.json
 *
 * 为什么要一个脚本：壳端 `decideUpdate` 只认 version / url / sha256 三样东西，
 * 缺一样就拒装。这三样里两样可以直接从产物读，剩一样（url）必须由发版的人给定，
 * 手写 JSON 迟早写出 63 位摘要或忘记改版本号。这里把它们绑在一起算。
 *
 * v0.11.8 起 feed 必须带 Ed25519 分离签名：签名覆盖
 * `{version,url,sha256,size,sbom}` 的规范字节（由壳端同一个函数生成）。
 * 私钥只从 `--sign-key` / `BLANK_UPDATE_SIGNING_KEY` 指向的文件读，**不进仓库**；
 * 脚本用壳里钉住的公钥自检——私钥与公钥不配对时当场失败，不会产出装不上的 feed。
 *
 * 出口码：0=生成并通过自检；1=N 处自检失败（不写文件）；2=参数/产物/密钥缺失。
 */
import { createHash, sign } from "node:crypto";
import { createRequire } from "node:module";
import { existsSync, readFileSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";

const require = createRequire(import.meta.url);
const ROOT = path.resolve(import.meta.dirname, "..");
const policy = require(path.join(ROOT, "desktop", "shell-policy.cjs"));
const {
  canonicalUpdatePayload,
  decideUpdate,
  extractFeedEntries,
  pickLatestEntry,
  planInstall,
  verifyUpdateSignature,
} = policy;

const problems = [];
const ok = (label, detail = "") => console.log(`  ok    ${label}${detail ? "  " + detail : ""}`);
const bad = (label, detail = "") => {
  problems.push(label);
  console.log(`  FAIL  ${label}${detail ? "  " + detail : ""}`);
};

const args = process.argv.slice(2);
const argValue = (name) => {
  const index = args.indexOf(name);
  return index >= 0 && args[index + 1] ? args[index + 1] : "";
};

const desktop = JSON.parse(readFileSync(path.join(ROOT, "desktop", "package.json"), "utf8"));
const version = String(desktop.version ?? "");
const baseUrl = String(argValue("--base-url") || process.env.BLANK_UPDATE_BASE_URL || "").trim();
const signKeyPath = String(
  argValue("--sign-key") || process.env.BLANK_UPDATE_SIGNING_KEY || ""
).trim();
const sbomArg = String(argValue("--sbom") || "").trim();
const out = path.resolve(
  ROOT,
  argValue("--out") || path.join("desktop", "dist", "latest.json")
);

if (!/^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/.test(version)) {
  console.error(`desktop/package.json 的版本不是 semver：${JSON.stringify(version)}`);
  process.exit(2);
}
if (!/^https?:\/\//i.test(baseUrl)) {
  console.error(
    `必须给 http(s) 的更新源根地址（--base-url 或 BLANK_UPDATE_BASE_URL），实际：${JSON.stringify(baseUrl)}`
  );
  process.exit(2);
}
if (!signKeyPath) {
  console.error(
    "缺少签名私钥：给 --sign-key <path> 或 BLANK_UPDATE_SIGNING_KEY（私钥不进仓库，见 desktop/README.md）"
  );
  process.exit(2);
}
const signKeyFile = path.resolve(signKeyPath);
if (!existsSync(signKeyFile)) {
  console.error(`找不到签名私钥：${signKeyFile}`);
  process.exit(2);
}
const signingKey = readFileSync(signKeyFile, "utf8");
const sbomFile = sbomArg ? path.resolve(ROOT, sbomArg) : "";
if (sbomFile && !existsSync(sbomFile)) {
  console.error(`找不到 SBOM：${sbomFile}（先跑 scripts/make_sbom.mjs）`);
  process.exit(2);
}

const artifactName = `Blank-AI-安装版-${version}.exe`;
const artifact = path.join(ROOT, "desktop", "dist", artifactName);
if (!existsSync(artifact)) {
  console.error(`找不到安装包：${artifact}（先在 desktop 目录跑 electron-builder）`);
  process.exit(2);
}

const sha256 = createHash("sha256").update(readFileSync(artifact)).digest("hex");
const url = `${baseUrl.replace(/\/+$/, "")}/${encodeURIComponent(artifactName)}`;
const size = statSync(artifact).size;
const entry = {
  version,
  url,
  sha256,
  size,
  notes: `空白格 ${version}（安装版 ${artifactName}）`,
};
if (sbomFile) {
  entry.sbom = `${baseUrl.replace(/\/+$/, "")}/${encodeURIComponent(path.basename(sbomFile))}`;
}
entry.signature = sign(null, Buffer.from(canonicalUpdatePayload(entry), "utf8"), signingKey).toString(
  "base64"
);
const feed = { entries: [entry] };

// ── 自检：拿壳端真函数读这份 JSON ──────────────────────────────────────────
const parsed = extractFeedEntries(JSON.parse(JSON.stringify(feed)));
parsed.entries ? ok("feed 能被 extractFeedEntries 读懂") : bad("feed 结构不被壳接受", parsed.reason);

const latest = pickLatestEntry(parsed.entries);
latest && latest.version === version
  ? ok("pickLatestEntry 选中本版", version)
  : bad("pickLatestEntry 没选中本版", JSON.stringify(latest?.version));

const upgrade = decideUpdate({ current: "0.0.0", entry: latest });
upgrade.action === "available" && upgrade.install === true
  ? ok("旧版本看得到更新且允许下载", `${upgrade.action} / ${upgrade.version}`)
  : bad("旧版本没有拿到可用更新", JSON.stringify(upgrade));

upgrade.sha256 === sha256
  ? ok("决策里的摘要与文件实测一致", sha256.slice(0, 12) + "…")
  : bad("决策里的摘要与文件实测不一致", `决策 ${upgrade.sha256} / 实测 ${sha256}`);

upgrade.size === size
  ? ok("决策里的字节数与产物一致", String(size))
  : bad("决策里的字节数与产物不一致", `决策 ${upgrade.size} / 实测 ${size}`);

// ── 签名：壳里钉的公钥必须认这把私钥，且改一个字节就红 ────────────────────
const signatureOk = verifyUpdateSignature({ entry, signature: entry.signature });
signatureOk.ok
  ? ok("Ed25519 签名与壳内固定公钥匹配", entry.signature.slice(0, 16) + "…")
  : bad("签名验不过（私钥与壳内公钥不配对？）", signatureOk.reason);

for (const field of ["version", "url", "sha256", "size", ...(entry.sbom ? ["sbom"] : [])]) {
  const tampered = { ...entry, [field]: field === "size" ? size + 1 : `${entry[field]}-tampered` };
  const check = verifyUpdateSignature({ entry: tampered, signature: entry.signature });
  !check.ok
    ? ok(`改 ${field} 后签名被拒`, check.reason)
    : bad(`改 ${field} 后签名仍然通过`);
}

const unsigned = verifyUpdateSignature({ entry, signature: "" });
!unsigned.ok ? ok("未签名条目被拒", unsigned.reason) : bad("未签名条目没有被拒");

const upToDate = decideUpdate({ current: version, entry: latest });
upToDate.action === "none"
  ? ok("同版本不提示更新", upToDate.reason)
  : bad("同版本仍提示更新", JSON.stringify(upToDate));

const noDigest = decideUpdate({ current: "0.0.0", entry: { version: "9.9.9", url } });
noDigest.action === "unavailable" && noDigest.install === false
  ? ok("缺摘要被拒", noDigest.reason)
  : bad("缺摘要没有被拒", JSON.stringify(noDigest));

const install = planInstall({
  decision: upgrade,
  downloadedPath: artifact,
  actualSha256: sha256
});
install.ok ? ok("下载后摘要复核放行") : bad("下载后摘要复核误拒", install.reason);

const wrong = planInstall({
  decision: upgrade,
  downloadedPath: artifact,
  actualSha256: "0".repeat(64)
});
!wrong.ok ? ok("半包/换包被拒", wrong.reason) : bad("摘要不符仍被放行");

console.log(
  `\n产物：${artifactName}  ${feed.entries[0].size} 字节  sha256=${sha256}`
);
console.log(`更新源：${url}`);
console.log(`签名：${entry.signature.slice(0, 24)}…（Ed25519，公钥钉在 desktop/shell-policy.cjs）`);
if (entry.sbom) console.log(`SBOM：${entry.sbom}`);

if (problems.length) {
  console.log(`\n自检失败 ${problems.length} 项，未写文件。`);
  process.exit(1);
}

writeFileSync(out, JSON.stringify(feed, null, 2) + "\n", "utf8");
console.log(`\n已写入 ${out}`);
console.log("自检全部通过");
