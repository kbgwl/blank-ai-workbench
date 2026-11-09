"use strict";

/**
 * 代码签名口径：发布者身份、证书挑选、以及「本机到底能不能签」的结论。
 *
 * 本机没有真证书，所以这里全部用合成证书清单钉判定；真实证书库的查询在
 * check-signing.cjs 里，只由人跑（`npm run sign:check`），不进自动化测试——
 * 否则一台没证书的开发机会让整条测试变红，而那不是代码缺陷。
 */

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const {
  PUBLISHER_NAME,
  TIMESTAMP_SERVER,
  certificateMatches,
  commonName,
  describeSigningConfig,
  pickSigningCertificate,
  signingBuildOverride,
  signingStatus,
} = require("../sign-policy.cjs");

const ROOT = path.join(__dirname, "..");
const pkg = JSON.parse(fs.readFileSync(path.join(ROOT, "package.json"), "utf8"));
const COMPANY = "无锡空白格人工智能科技有限公司";

const cert = (overrides = {}) => ({
  subject: `CN=${COMPANY}, O=Blank AI, C=CN`,
  thumbprint: "AAA111",
  notAfter: "2027-12-31",
  hasPrivateKey: true,
  eku: "Code Signing",
  ...overrides,
});

test("发布者身份写死在配置里，且与代码口径一致", () => {
  assert.equal(PUBLISHER_NAME, COMPANY);
  const config = describeSigningConfig(pkg.build);
  assert.deepEqual(config.problems, []);
  assert.equal(pkg.build.win.signtoolOptions.publisherName, COMPANY);
  assert.equal(pkg.build.win.signtoolOptions.certificateSubjectName, COMPANY);
  assert.deepEqual(pkg.build.win.signtoolOptions.signingHashAlgorithms, ["sha256"]);
  assert.equal(pkg.build.win.signtoolOptions.rfc3161TimeStampServer, TIMESTAMP_SERVER);
});

test("签名自检入口必须在 scripts 里，且不并进 dist", () => {
  // dist 自带一份同样的判定（dist-build.cjs），但 sign:check 是给人看的那一眼：
  // 发布前必须显式跑并看退出码，不能指望从打包日志里发现"这次没签上"。
  assert.equal(pkg.scripts["sign:check"], "node check-signing.cjs");
  assert.doesNotMatch(pkg.scripts.dist, /sign:check/);
});

test("打包走 dist-build.cjs：有证书就签，没有证书仍能出包", () => {
  assert.equal(pkg.scripts.dist, "node dist-build.cjs");
  assert.equal(pkg.scripts["dist:portable"], "node dist-build.cjs portable");
  assert.equal(pkg.scripts["dist:nsis"], "node dist-build.cjs nsis");
  assert.ok(fs.existsSync(path.join(ROOT, "dist-build.cjs")));
});

test("三态覆盖项：按指纹签 / 让位给 CSC_LINK / 只关签名", () => {
  const inStore = signingBuildOverride(signingStatus({ certs: [cert()] }));
  // 关键：必须把主体名清成 null，否则 electron-builder 仍按名字去证书库找，
  // 找不到就让整个 dist 失败（本机就是没有公司证书）。
  assert.equal(inStore.win.signtoolOptions.certificateSubjectName, null);
  assert.equal(inStore.win.signtoolOptions.certificateSha1, "AAA111");
  assert.equal(inStore.win.signExecutable, undefined);

  const external = signingBuildOverride(
    signingStatus({ certs: [], cscLink: "C:\\keys\\blank.pfx" }),
  );
  assert.equal(external.win.signtoolOptions.certificateSubjectName, null);
  assert.equal(external.win.signtoolOptions.certificateSha1, undefined);
  assert.equal(external.win.signExecutable, undefined);

  const missing = signingBuildOverride(signingStatus({ certs: [] }));
  assert.equal(missing.win.signExecutable, false);
  assert.equal(missing.win.signtoolOptions, undefined, "没有证书时不能留任何签名配置");
});

test("覆盖项真的能盖掉 package.json 里的主体名（用 electron-builder 自己的合并函数验）", () => {
  // dist-build.cjs 把覆盖项交给 electron-builder，合并语义由它决定。这里不假设
  // "null 会跳过"，而是拿它的 deepAssign 真跑一遍：主题名必须变成 null。
  const { deepAssign } = require("builder-util-runtime");
  const merged = deepAssign(
    {
      win: {
        signtoolOptions: {
          publisherName: COMPANY,
          certificateSubjectName: COMPANY,
          signingHashAlgorithms: ["sha256"],
        },
      },
    },
    signingBuildOverride(signingStatus({ certs: [cert()] })),
  );
  assert.equal(merged.win.signtoolOptions.certificateSubjectName, null);
  assert.equal(merged.win.signtoolOptions.certificateSha1, "AAA111");
  assert.equal(merged.win.signtoolOptions.publisherName, COMPANY, "发布者身份不能被覆盖项动掉");

  const unsigned = deepAssign(
    { win: { signtoolOptions: { certificateSubjectName: COMPANY } } },
    signingBuildOverride(signingStatus({ certs: [] })),
  );
  assert.equal(unsigned.win.signExecutable, false);
  assert.equal(unsigned.win.signtoolOptions.certificateSubjectName, COMPANY);
});

test("Subject 只认 CN 那一段，带引号/带其它字段都能取到", () => {
  assert.equal(commonName(`CN=${COMPANY}, O=Blank AI, C=CN`), COMPANY);
  assert.equal(commonName(`O=Blank AI, C=CN, CN=${COMPANY}`), COMPANY);
  assert.equal(commonName(`CN="Blank, Inc.", O=Blank AI`), "Blank, Inc.");
  assert.equal(commonName("O=Blank AI, C=CN"), "");
});

test("证书匹配：同名带私钥才算数", () => {
  assert.equal(certificateMatches(cert()), true);
  // 主体名不同 → 不匹配（不能拿一张别的公司的证书签我们的名）
  assert.equal(certificateMatches(cert({ subject: "CN=别的公司, O=X, C=CN" })), false);
  // 只有公钥 → 签不了
  assert.equal(certificateMatches(cert({ hasPrivateKey: false })), false);
  // 非代码签名用途（服务器证书等）→ 不匹配
  assert.equal(
    certificateMatches(cert({ eku: "Server Authentication" })),
    false,
  );
});

test("多张合规证书取有效期最晚的那张", () => {
  const older = cert({ thumbprint: "OLD", notAfter: "2027-01-01" });
  const newer = cert({ thumbprint: "NEW", notAfter: "2029-01-01" });
  const picked = pickSigningCertificate([older, newer]);
  assert.equal(picked.thumbprint, "NEW");
  const tie = pickSigningCertificate([
    cert({ thumbprint: "B" }),
    cert({ thumbprint: "A" }),
  ]);
  assert.equal(tie.thumbprint, "A", "同日期时按指纹稳定排序，结果不能随输入顺序漂移");
});

test("库里没有公司证书时，结论是 missing 且说清查了多少张", () => {
  const status = signingStatus({
    certs: [
      { subject: "CN=localhost", thumbprint: "L", hasPrivateKey: true, eku: "Server Authentication" },
    ],
  });
  assert.equal(status.ok, false);
  assert.equal(status.code, "missing");
  assert.match(status.reason, new RegExp(COMPANY));
  assert.match(status.reason, /共 1 张/);
});

test("库里找到公司证书时给出指纹；先用 CSC_LINK 时不替外部证书的主体名背书", () => {
  const inStore = signingStatus({ certs: [cert()] });
  assert.equal(inStore.ok, true);
  assert.equal(inStore.code, "store");
  assert.equal(inStore.certificate.thumbprint, "AAA111");

  const external = signingStatus({ certs: [], cscLink: "C:\\keys\\blank.pfx" });
  assert.equal(external.ok, true);
  assert.equal(external.code, "external");
  assert.match(external.reason, /未在本机核验/);
});

test("配置被单边改动时 describeSigningConfig 会报出来", () => {
  const broken = {
    win: {
      signtoolOptions: {
        publisherName: "Blank AI Inc.",
        certificateSubjectName: COMPANY,
        signingHashAlgorithms: ["sha1"],
        rfc3161TimeStampServer: "",
      },
    },
  };
  const problems = describeSigningConfig(broken).problems;
  assert.equal(problems.length, 3);
  assert.ok(problems.some((line) => line.includes("publisherName")));
  assert.ok(problems.some((line) => line.includes("sha256")));
  assert.ok(problems.some((line) => line.includes("rfc3161TimeStampServer")));
});
