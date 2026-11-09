"use strict";

/**
 * 发布前自检：这台 Windows 上有没有能签「无锡空白格人工智能科技有限公司」的证书。
 *
 * 只读，不改任何东西。退出码：0 找到；2 没找到（dist 仍能出包，但产物是未签名 exe，
 * SmartScreen 会显示"未知发布者"）；1 查询本身失败（不能把"查不到"说成"没有"）。
 *
 * dist-build.cjs 用同一份判定决定签或不签；这里单独成一个命令，是因为发布前必须有人
 * 看见这一行，不能指望从打包日志里一句容易被刷过去的 warn 里发现签名缺失。
 */
const pkg = require("./package.json");
const {
  describeSigningConfig,
  loadCertificateStore,
  signingStatus,
} = require("./sign-policy.cjs");

function main() {
  const config = describeSigningConfig(pkg.build || {});
  console.log(`发布者：${config.publisher || "（未配置）"}`);
  for (const problem of config.problems) console.log(`  FAIL  ${problem}`);
  if (config.problems.length) return 1;

  const store = loadCertificateStore();
  if (!store.ok) {
    console.log(`  FAIL  查询证书库失败：${store.reason}`);
    return 1;
  }
  const status = signingStatus({
    certs: store.certs,
    cscLink: process.env.WIN_CSC_LINK || process.env.CSC_LINK || "",
  });
  console.log(status.ok ? `  ok    ${status.reason}` : `  FAIL  ${status.reason}`);
  if (!status.ok) {
    console.log("        把公司的代码签名证书（含私钥）装进 Cert:\\CurrentUser\\My，");
    console.log("        或用 CSC_LINK + CSC_KEY_PASSWORD 指向 pfx，再重跑本检查。");
    return 2;
  }
  return 0;
}

process.exitCode = main();
