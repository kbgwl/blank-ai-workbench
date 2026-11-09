"use strict";

/**
 * 打包入口：有公司证书就签，没有就明确说一声、只出未签名包。
 *
 * 不能直接跑 `electron-builder`：package.json 里配了 certificateSubjectName，
 * electron-builder 会去证书库找这张证书，找不到就让整个 dist 失败
 * （app-builder-lib codeSign/windowsSignToolManager.js 的 getCertificateFromStoreInfo）。
 * 发布机上这正是我们要的；开发机/CI 没有证书，也得能出包做验收。
 *
 * 用法与原来的脚本一致：`node dist-build.cjs` / `node dist-build.cjs portable`。
 * 证书判定与覆盖项在 sign-policy.cjs（可单测），这里只做「读状态 → 打印 → 交给 electron-builder」。
 */

const { Arch, Platform, build } = require("electron-builder");
const { loadCertificateStore, signingBuildOverride, signingStatus } = require("./sign-policy.cjs");

const TARGETS = ["portable", "nsis", "dir"];

function requestedTargets(argv) {
  const wanted = argv.filter((arg) => TARGETS.includes(arg));
  const unknown = argv.filter((arg) => !TARGETS.includes(arg));
  if (unknown.length) {
    throw new Error(`不认识的参数：${unknown.join(" ")}（可用：${TARGETS.join(" ")}）`);
  }
  return wanted;
}

async function main() {
  const targets = requestedTargets(process.argv.slice(2));
  const store = loadCertificateStore();
  const status = signingStatus({
    certs: store.ok ? store.certs : [],
    cscLink: process.env.WIN_CSC_LINK || process.env.CSC_LINK || "",
  });
  if (!store.ok && !status.ok) {
    console.log(`代码签名：证书库查询失败（${store.reason}），按没有证书处理`);
  }
  console.log(`代码签名：${status.reason}`);
  if (!status.ok) {
    console.log("代码签名：本次只出未签名包（SmartScreen 会显示“未知发布者”），发布前先跑 npm run sign:check");
  }

  const platformTarget = targets.length
    ? Platform.WINDOWS.createTarget(targets, Arch.x64)
    : Platform.WINDOWS.createTarget();
  await build({ targets: platformTarget, config: signingBuildOverride(status) });
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
