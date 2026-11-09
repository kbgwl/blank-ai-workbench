"use strict";

/**
 * 代码签名的口径：发布者是谁、什么样的证书算数、这台机器现在能不能签。
 *
 * 判定全部是纯函数，测试用合成证书清单把结论钉住（本机没有真证书，不能拿
 * "跑一次没报错"当证据）；只有 loadCertificateStore 碰系统，读一次证书记录。
 *
 * 「发布者名」是产品身份的一部分，和 appId 同级：它不是一个可随手覆盖的配置项。
 * package.json 里的 publisherName / certificateSubjectName 必须与这里一致，
 * tests/sign-policy.test.cjs 会拦下任何单边改动。
 */

const { execFileSync } = require("node:child_process");

const PUBLISHER_NAME = "无锡空白格人工智能科技有限公司";
// RFC3161 时间戳：没有它，证书过期后已发布的安装包签名会一起失效。
// DigiCert 的公共端点不需要账号，是 electron-builder 的默认值。
const TIMESTAMP_SERVER = "http://timestamp.digicert.com";
const SIGNING_HASH_ALGORITHMS = ["sha256"];

/** Subject 里的 CN 值。证书库返回的是 `CN=名字, O=..., C=CN` 这种形状。 */
function commonName(subject) {
  const text = String(subject ?? "");
  // 值里带逗号时 openssl 会写成 CN="a, b"，先认这种，再退回无引号形式。
  const quoted = /(?:^|[,+])\s*CN\s*=\s*"([^"]*)"/i.exec(text);
  if (quoted) return quoted[1];
  const plain = /(?:^|[,+])\s*CN\s*=\s*([^,]*)/i.exec(text);
  return plain ? plain[1].trim() : "";
}

/** 比较主体名：去掉空白、大小写不敏感。证书里的中文是同一串字符，不做拼音/英文名匹配。 */
function normalize(value) {
  return String(value ?? "").replace(/\s+/g, "").toUpperCase();
}

/**
 * 这张证书能不能签我们的名。
 *
 * 三件事缺一不可：Subject 的 CN 就是我们、带私钥（只有公钥的证书验证得了别人的签名，
 * 签不了自己的）、并且真的被系统认作代码签名证书（查询侧用 -CodeSigningCert 过滤，
 * 这里再看一眼 EKU 字段，防止调用方传进来一份没过滤的清单）。
 */
function certificateMatches(cert, publisher = PUBLISHER_NAME) {
  if (!cert || typeof cert !== "object") return false;
  if (cert.hasPrivateKey === false) return false;
  if (cert.eku && !String(cert.eku).toLowerCase().includes("code signing")) return false;
  return normalize(commonName(cert.subject)) === normalize(publisher);
}

function certificateDate(cert) {
  const parsed = Date.parse(String(cert?.notAfter ?? ""));
  return Number.isNaN(parsed) ? 0 : parsed;
}

/** 有多张合规证书时用有效期最晚的那张；同日期时取指纹稳定排序的第一个。 */
function pickSigningCertificate(certs, publisher = PUBLISHER_NAME) {
  const matches = (Array.isArray(certs) ? certs : []).filter((cert) =>
    certificateMatches(cert, publisher),
  );
  if (!matches.length) return null;
  return matches.sort((a, b) => {
    const diff = certificateDate(b) - certificateDate(a);
    if (diff !== 0) return diff;
    return String(a.thumbprint ?? "").localeCompare(String(b.thumbprint ?? ""));
  })[0];
}

/**
 * 结论三态：store（证书库里有）/ external（交给 CSC_LINK 的 pfx）/ missing。
 *
 * external 不明说"一定能签"：pfx 的 Subject 要输密码才看得到，这里只能确认
 * electron-builder 会拿到一份外部证书，不能替它背书主体名。
 */
function signingStatus({ certs, cscLink } = {}, publisher = PUBLISHER_NAME) {
  const list = Array.isArray(certs) ? certs : [];
  if (cscLink) {
    return {
      ok: true,
      code: "external",
      certificate: null,
      reason:
        "检测到 CSC_LINK/WIN_CSC_LINK：electron-builder 会用这份外部证书签名，" +
        "其主体名未在本机核验",
    };
  }
  const certificate = pickSigningCertificate(list, publisher);
  if (certificate) {
    return {
      ok: true,
      code: "store",
      certificate,
      reason: `证书库中找到「${publisher}」的代码签名证书（指纹 ${certificate.thumbprint ?? "未知"}）`,
    };
  }
  return {
    ok: false,
    code: "missing",
    certificate: null,
    reason:
      `证书库里没有「${publisher}」的代码签名证书（带私钥）：` +
      `CurrentUser\\My + LocalMachine\\My 按代码签名用途过滤后共 ${list.length} 张，均不匹配`,
  };
}

/** package.json 的签名配置与实际身份是否一致；返回问题清单，空数组才算配好。 */
function describeSigningConfig(build = {}) {
  const options = (build.win && build.win.signtoolOptions) || {};
  const problems = [];
  if (options.publisherName !== PUBLISHER_NAME) {
    problems.push(`win.signtoolOptions.publisherName 必须是「${PUBLISHER_NAME}」`);
  }
  if (options.certificateSubjectName !== PUBLISHER_NAME) {
    problems.push(`win.signtoolOptions.certificateSubjectName 必须是「${PUBLISHER_NAME}」`);
  }
  const algorithms = options.signingHashAlgorithms;
  if (!Array.isArray(algorithms) || !algorithms.includes("sha256")) {
    problems.push("win.signtoolOptions.signingHashAlgorithms 必须包含 sha256");
  }
  if (!options.rfc3161TimeStampServer) {
    problems.push("win.signtoolOptions.rfc3161TimeStampServer 不能为空（否则证书过期后签名一起失效）");
  }
  return { publisher: options.publisherName || "", problems };
}

/**
 * 读证书库（只读）。返回 {ok, certs} 或 {ok:false, reason}，不抛异常：
 * "查不到"与"用不了"要在调用方分成两种结局，混在异常里迟早被当成"没有证书"。
 */
function loadCertificateStore() {
  if (process.platform !== "win32") {
    return { ok: false, certs: [], reason: `证书库只在 Windows 上可查（当前平台 ${process.platform}）` };
  }
  // 中文 Subject 必须按 UTF-8 写回 stdout，否则 Node 侧拿到的是 OEM 编码的乱码，
  // "名字对不上"会变成一条永远修不好的假红。
  const script = [
    "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8",
    "$rows = @()",
    "foreach ($store in @('Cert:\\CurrentUser\\My', 'Cert:\\LocalMachine\\My')) {",
    "  $rows += @(Get-ChildItem -Path $store -CodeSigningCert -ErrorAction SilentlyContinue | ForEach-Object {",
    "    [pscustomobject]@{",
    "      subject = $_.Subject",
    "      thumbprint = $_.Thumbprint",
    "      notAfter = $_.NotAfter.ToString('yyyy-MM-dd')",
    "      hasPrivateKey = [bool]$_.HasPrivateKey",
    "      eku = ($_.EnhancedKeyUsageList | ForEach-Object { $_.FriendlyName }) -join '; '",
    "    }",
    "  })",
    "}",
    "ConvertTo-Json -Compress -InputObject @($rows)",
  ].join("\n");
  try {
    const raw = execFileSync(
      "powershell",
      ["-NoProfile", "-NonInteractive", "-Command", script],
      { encoding: "utf8", timeout: 30_000 },
    ).trim();
    if (!raw) return { ok: true, certs: [] };
    const parsed = JSON.parse(raw);
    return { ok: true, certs: Array.isArray(parsed) ? parsed : [parsed] };
  } catch (error) {
    return { ok: false, certs: [], reason: String(error.message).split("\n")[0] };
  }
}

/**
 * 打包时盖在 package.json 的 build 之上的覆盖项。
 *
 * electron-builder 只要看到 certificateSubjectName 就去证书库找，找不到直接让
 * `dist` 失败（app-builder-lib codeSign/windowsSignToolManager.js）。发布机上有证书时
 * 这正是我们要的行为；没有证书的开发机/CI 仍要能出未签名包做验收，所以这里给出
 * 三态：按指纹签 / 交给 CSC_LINK / 只关签名。signExecutable=false 只跳过签名，
 * 图标与版本元数据照常写入。
 */
function signingBuildOverride(status) {
  if (status && status.ok && status.code === "store") {
    return {
      win: {
        signtoolOptions: {
          certificateSubjectName: null,
          certificateSha1: status.certificate && status.certificate.thumbprint,
        },
      },
    };
  }
  if (status && status.ok && status.code === "external") {
    return { win: { signtoolOptions: { certificateSubjectName: null } } };
  }
  return { win: { signExecutable: false } };
}

module.exports = {
  PUBLISHER_NAME,
  SIGNING_HASH_ALGORITHMS,
  TIMESTAMP_SERVER,
  certificateMatches,
  commonName,
  describeSigningConfig,
  loadCertificateStore,
  normalize,
  pickSigningCertificate,
  signingBuildOverride,
  signingStatus,
};
