/**
 * 静态导出的严格 CSP：给 out/**\/*.html 注入 <meta>。
 *
 * 为什么不用 next.config 的 headers()：桌面壳是 `output: "export"`，没有服务器，
 * headers() 不生效；而 Next 的静态 HTML 里既有主题引导内联脚本，也有
 * `self.__next_f.push(...)`。这里在构建后按页面实际内容算 SHA-256，写进
 * `script-src 'self' 'sha256-...'`——不放 `'unsafe-inline'`，新增/改动内联脚本
 * 而没重新构建会被浏览器直接拒绝，而不是悄悄放行。
 *
 * 只处理 `BLANK_DESKTOP_BUILD=1` 的 out；服务端构建不跑这一步。
 */

import { createHash } from "node:crypto";
import { readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const SCRIPT = /<script\b([^>]*)>([\s\S]*?)<\/script>/gi;
const SRC_ATTR = /\bsrc\s*=/i;
const CSP_META = /<meta\b[^>]*data-blank-csp[^>]*>\s*/gi;

export function inlineScriptHashes(html) {
  const hashes = new Set();
  for (const match of String(html ?? "").matchAll(SCRIPT)) {
    const attrs = match[1] || "";
    if (SRC_ATTR.test(attrs)) continue;
    hashes.add(createHash("sha256").update(match[2] || "", "utf8").digest("base64"));
  }
  return [...hashes];
}

export function buildCsp(html) {
  const hashes = inlineScriptHashes(html);
  const script = ["'self'", ...hashes.map((hash) => `'sha256-${hash}'`)].join(" ");
  return [
    "default-src 'self'",
    `script-src ${script}`,
    // React 的 style 属性与少量内联样式：样式不放行不影响脚本边界。
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob: http://127.0.0.1:* http://localhost:*",
    "font-src 'self' data:",
    // 桌面壳的后端固定在本机回环；自定义远端 API 不在这个静态导出里支持。
    "connect-src 'self' http://127.0.0.1:* http://localhost:* ws://127.0.0.1:* ws://localhost:*",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
  ].join("; ");
}

export function injectCsp(html) {
  const clean = String(html ?? "").replace(CSP_META, "");
  const csp = buildCsp(clean);
  const meta = `<meta data-blank-csp http-equiv="Content-Security-Policy" content="${csp.replaceAll("&", "&amp;").replaceAll('"', "&quot;")}">`;
  return clean.replace(/<head(\s[^>]*)?>/i, (head) => `${head}${meta}`);
}

function htmlFiles(directory) {
  const files = [];
  for (const name of readdirSync(directory)) {
    const full = path.join(directory, name);
    const stat = statSync(full);
    if (stat.isDirectory()) files.push(...htmlFiles(full));
    else if (name.toLowerCase().endsWith(".html")) files.push(full);
  }
  return files;
}

function main() {
  const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
  // 两种产物都要处理：
  // - out/：桌面 static export；
  // - .next/server/app|pages：next start 直接读的预渲染 HTML。
  // 服务端动态渲染的页面不在这些文件里，那些页面需要另走 nonce；本项目页面均为静态预渲染。
  const roots = [
    ["out", path.join(webRoot, "out")],
    [".next/server/app", path.join(webRoot, ".next", "server", "app")],
    [".next/server/pages", path.join(webRoot, ".next", "server", "pages")],
  ];
  let total = 0;
  let changed = 0;
  for (const [label, directory] of roots) {
    let files;
    try {
      files = htmlFiles(directory);
    } catch {
      continue;
    }
    let rootChanged = 0;
    for (const file of files) {
      const before = readFileSync(file, "utf8");
      const after = injectCsp(before);
      if (after !== before) {
        writeFileSync(file, after, "utf8");
        rootChanged += 1;
      }
    }
    total += files.length;
    changed += rootChanged;
    if (files.length) console.log(`[csp] ${label}：${rootChanged}/${files.length} 个页面`);
  }
  if (!total) {
    console.log("[csp] 没有可处理的 HTML 产物，跳过");
    return 0;
  }
  console.log(`[csp] 共为 ${changed}/${total} 个页面注入严格 CSP（脚本哈希）`);
  return 0;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exit(main());
}
