"use strict";

/**
 * 自动更新的判定测试（decideUpdate / planInstall / 版本比较 / feed 解析）。
 *
 * 两条契约：
 * 1. 每种「不能更新」都要给出人能读懂的原因，且 install 为 false；
 * 2. 元数据说可以更新（install=true）**不等于**可以装：真正执行安装器前必须
 *    用 planInstall 拿实际下载文件的摘要再过一遍。元数据里的 sha256 是"应该是
 *    多少"，只有算一遍手上这个文件才知道"实际是多少"。
 */

const test = require("node:test");
const assert = require("node:assert/strict");

const {
  decideUpdate,
  extractFeedEntries,
  parseUpdateFeedUrl,
  parseVersion,
  pickLatestEntry,
  compareVersions,
  planInstall,
} = require("../shell-policy.cjs");

const SHA_OK = "a".repeat(64);

test("版本号解析：带 v 前缀与预发布后缀都认，残缺与乱写不认", () => {
  assert.deepEqual(parseVersion("0.8.5"), { major: 0, minor: 8, patch: 5, pre: null });
  assert.deepEqual(parseVersion("v1.2.3-rc.1"), { major: 1, minor: 2, patch: 3, pre: "rc.1" });
  assert.equal(parseVersion("1.2"), null);
  assert.equal(parseVersion("latest"), null);
  assert.equal(parseVersion(""), null);
  assert.equal(parseVersion(undefined), null);
});

test("版本比较按数值段走：0.10.0 必须大于 0.9.9（字符串比较会红）", () => {
  assert.equal(compareVersions("0.10.0", "0.9.9"), 1);
  assert.equal(compareVersions("0.9.0", "0.8.5"), 1);
  assert.equal(compareVersions("0.8.5", "0.8.5"), 0);
  assert.equal(compareVersions("0.8.4", "0.8.5"), -1);
});

test("预发布低于同号正式版，数字标识符按数值比（rc.2 < rc.10）", () => {
  assert.equal(compareVersions("1.0.0-rc.1", "1.0.0"), -1);
  assert.equal(compareVersions("1.0.0", "1.0.0-rc.1"), 1);
  assert.equal(compareVersions("1.0.0-rc.2", "1.0.0-rc.10"), -1);
  assert.equal(compareVersions("abc", "0.0.1"), null, "比不了就返回 null，不许当 0 混成相等");
});

test("feed 形态：数组 / {entries:[...]} / 单条目对象都收，其余给结构化原因", () => {
  const entry = { version: "0.9.0", url: "https://x/y", sha256: SHA_OK };
  assert.deepEqual(extractFeedEntries([entry]), { entries: [entry], reason: "" });
  assert.deepEqual(extractFeedEntries({ entries: [entry] }), { entries: [entry], reason: "" });
  assert.deepEqual(extractFeedEntries(entry), { entries: [entry], reason: "" });
  const invalid = "feed JSON 既不是条目数组，也不是 entries 数组或带 version 的单个条目";
  assert.deepEqual(extractFeedEntries("0.9.0"), { entries: null, reason: invalid });
  assert.deepEqual(extractFeedEntries({ note: "今天不发版" }), { entries: null, reason: invalid });
  assert.deepEqual(extractFeedEntries(null), { entries: null, reason: invalid });
  // entries 不是数组：同一条结构错误，不许静默吞掉。
  assert.deepEqual(extractFeedEntries({ entries: {} }), { entries: null, reason: invalid });
  assert.deepEqual(extractFeedEntries({ entries: "1.0.0" }), { entries: null, reason: invalid });
});

test("挑条目：取版本最高且可解析的一条，脏数据跳过", () => {
  const picked = pickLatestEntry([
    null,
    { version: "0.9.0" },
    "不是对象",
    { version: "nightly" },
    { version: "0.10.0" },
    { version: "0.9.9" },
  ]);
  assert.equal(picked.version, "0.10.0");
  assert.equal(pickLatestEntry([{ version: "bad" }]), null);
  assert.equal(pickLatestEntry([]), null);
  assert.equal(pickLatestEntry(undefined), null);
});

test("更新源 URL：未配置/非 http(s) 都要有可读原因，合法的原样通过", () => {
  assert.equal(parseUpdateFeedUrl("").ok, false);
  assert.match(parseUpdateFeedUrl("").reason, /BLANK_UPDATE_FEED_URL/);
  assert.equal(parseUpdateFeedUrl("ftp://intranet/feed.json").ok, false);
  assert.deepEqual(parseUpdateFeedUrl("https://example.com/feed.json"), {
    ok: true,
    url: "https://example.com/feed.json",
  });
});

test("网络失败：给可读原因、不安装", () => {
  const decision = decideUpdate({ current: "0.8.5", entry: null, fetchError: "getaddrinfo ENOTFOUND example.com" });
  assert.equal(decision.action, "unavailable");
  assert.match(decision.reason, /更新源请求失败/);
  assert.match(decision.reason, /ENOTFOUND/);
  assert.equal(decision.install, false);
});

test("元数据缺字段逐条归因：version 缺失、格式怪、当前版本读不懂各给各的原因", () => {
  const cases = [
    [{ current: "0.8.5", entry: { url: "https://x", sha256: SHA_OK } }, /version/],
    [{ current: "0.8.5", entry: { version: "0.9", url: "https://x", sha256: SHA_OK } }, /无法识别/],
    [{ current: "nightly-build", entry: { version: "0.9.0", url: "https://x", sha256: SHA_OK } }, /当前版本/],
    [{ current: "0.8.5", entry: null }, /缺失/],
  ];
  for (const [input, reasonPattern] of cases) {
    const decision = decideUpdate(input);
    assert.equal(decision.action, "unavailable", JSON.stringify(input));
    assert.match(decision.reason, reasonPattern);
    assert.equal(decision.install, false);
  }
});

test("不比当前新就不动：相等与更旧都归 'none'", () => {
  for (const version of ["0.8.5", "0.8.4"]) {
    const decision = decideUpdate({
      current: "0.8.5",
      entry: { version, url: "https://x/setup.exe", sha256: SHA_OK },
    });
    assert.equal(decision.action, "none");
    assert.match(decision.reason, /无需更新/);
    assert.equal(decision.install, false);
  }
});

test("新版本但下载/签名信息不全：缺地址、非 http(s)、缺摘要、摘要格式错，各自拒掉", () => {
  const cases = [
    [{ version: "0.9.0", sha256: SHA_OK }, /缺下载地址/],
    [{ version: "0.9.0", url: "file:///D:/share/setup.exe", sha256: SHA_OK }, /不是 http\(s\)/],
    [{ version: "0.9.0", url: "https://x/setup.exe" }, /签名摘要/],
    [{ version: "0.9.0", url: "https://x/setup.exe", sha256: "deadbeef" }, /64 位十六进制/],
  ];
  for (const [entry, reasonPattern] of cases) {
    const decision = decideUpdate({ current: "0.8.5", entry });
    assert.equal(decision.action, "unavailable", JSON.stringify(entry));
    assert.match(decision.reason, reasonPattern);
    assert.equal(decision.install, false);
  }
});

test("一切合规：available 且允许下载，但摘要要归一成小写", () => {
  const decision = decideUpdate({
    current: "0.8.5",
    entry: { version: "0.9.0", url: "https://example.com/setup-0.9.0.exe", sha256: SHA_OK.toUpperCase() },
  });
  assert.equal(decision.action, "available");
  assert.equal(decision.version, "0.9.0");
  assert.equal(decision.sha256, SHA_OK, "摘要归一成小写十六进制，比对时大小写不该咬人");
  assert.equal(decision.install, true, "校验全过之后要允许下载，否则自动更新永远是骨架");
});

test("不变量：拿不到可用元数据的分支，install 一次都不许为真", () => {
  const inputs = [
    { current: "0.8.5", entry: { version: "0.8.5" } },
    { current: "0.8.5", entry: {}, fetchError: "超时" },
    { current: "0.8.5", entry: null },
    { current: "坏版本", entry: { version: "0.9.0" } },
    { current: undefined, entry: undefined, fetchError: undefined },
  ];
  for (const input of inputs) {
    const decision = decideUpdate(input);
    assert.ok(["available", "none", "unavailable"].includes(decision.action), JSON.stringify(decision));
    assert.equal(decision.install, false, `不该允许安装却允许了：${JSON.stringify(input)}`);
    assert.equal(typeof decision.reason, "string");
    assert.ok(decision.reason.length > 0, "每一条判定都得说人话");
  }
});

test("planInstall：摘要对不上就拒绝执行安装器", () => {
  const decision = decideUpdate({
    current: "0.8.5",
    entry: { version: "0.9.0", url: "https://x/setup.exe", sha256: SHA_OK },
  });
  const good = planInstall({
    decision,
    downloadedPath: "C:/tmp/blank-update/setup.exe",
    actualSha256: SHA_OK.toUpperCase(),
  });
  assert.equal(good.ok, true);
  assert.deepEqual(good.args, ["/S"], "NSIS 静默安装开关");

  const cases = [
    [{ downloadedPath: "", actualSha256: SHA_OK }, /没有下载成功/],
    [{ downloadedPath: "C:/tmp/setup.zip", actualSha256: SHA_OK }, /不是 \.exe\/\.msi/],
    [{ downloadedPath: "C:/tmp/setup.exe", actualSha256: "" }, /没有算出.*摘要/],
    [{ downloadedPath: "C:/tmp/setup.exe", actualSha256: "b".repeat(64) }, /摘要不匹配/],
  ];
  for (const [extra, pattern] of cases) {
    const plan = planInstall({ decision, ...extra });
    assert.equal(plan.ok, false, JSON.stringify(extra));
    assert.match(plan.reason, pattern);
    assert.equal(plan.path, "", "拒绝时不能给出可执行路径");
  }
});

test("planInstall：没有 available 的判定时，任何文件都不许装", () => {
  for (const decision of [
    null,
    { action: "none", install: false },
    { action: "unavailable", install: false },
    { action: "available", install: false },
  ]) {
    const plan = planInstall({
      decision,
      downloadedPath: "C:/tmp/setup.exe",
      actualSha256: SHA_OK,
    });
    assert.equal(plan.ok, false, JSON.stringify(decision));
  }
});

// ── Ed25519 更新签名（v0.11.8）───────────────────────────────────────────────
//
// 摘要回答"文件是不是这份元数据说的那份"，签名回答"这份元数据是不是发版人写的"。
// 两条链缺一条，更新源仍然可以自说自话。

const { generateKeyPairSync, sign, createPublicKey } = require("node:crypto");
const {
  UPDATE_PUBKEY_PEM,
  canonicalUpdatePayload,
  verifyUpdateSignature,
} = require("../shell-policy.cjs");

const TEST_KEYS = generateKeyPairSync("ed25519");
const TEST_PUBKEY = TEST_KEYS.publicKey.export({ type: "spki", format: "pem" });

function signedEntry(overrides = {}) {
  const entry = {
    version: "0.9.0",
    url: "https://example.com/setup-0.9.0.exe",
    sha256: SHA_OK,
    size: 123456,
    ...overrides,
  };
  entry.signature = sign(
    null,
    Buffer.from(canonicalUpdatePayload(entry), "utf8"),
    TEST_KEYS.privateKey
  ).toString("base64");
  return entry;
}

test("规范负载：字段顺序固定、无空白，size 必须为正整数，多出来的字段不进签名", () => {
  const payload = canonicalUpdatePayload({
    version: " 0.9.0 ",
    url: " https://x/setup.exe ",
    sha256: SHA_OK.toUpperCase(),
    size: 42,
    notes: "不该进签名",
  });
  assert.equal(
    payload,
    JSON.stringify({
      version: "0.9.0",
      url: "https://x/setup.exe",
      sha256: SHA_OK,
      size: 42,
      sbom: "",
    })
  );
  assert.equal(canonicalUpdatePayload({ ...JSON.parse(payload), size: 0 }).includes('"size":0'), true);
  assert.equal(canonicalUpdatePayload({ ...JSON.parse(payload), size: -1 }).includes('"size":0'), true);
  assert.equal(canonicalUpdatePayload({ ...JSON.parse(payload), size: 1.5 }).includes('"size":0'), true);
});

test("自签密钥：正确签名通过，改任一个被签字段都拒绝", () => {
  const entry = signedEntry();
  assert.equal(
    verifyUpdateSignature({ entry, signature: entry.signature, publicKeyPem: TEST_PUBKEY }).ok,
    true
  );
  for (const field of ["version", "url", "sha256", "size", "sbom"]) {
    const tampered = { ...entry, [field]: field === "size" ? 123457 : `${entry[field]}-x` };
    const check = verifyUpdateSignature({
      entry: tampered,
      signature: entry.signature,
      publicKeyPem: TEST_PUBKEY,
    });
    assert.equal(check.ok, false, `改 ${field} 之后签名仍然通过`);
    assert.match(check.reason, /不匹配/);
  }
});

test("未签名、坏 base64、长度不对、没 size、公钥缺失都拒绝且各说各的原因", () => {
  const entry = signedEntry();
  const cases = [
    [{ entry, signature: "" }, /缺 signature/],
    [{ entry, signature: "not base64!!" }, /不是合法的 base64/],
    [{ entry, signature: Buffer.alloc(32).toString("base64") }, /64 字节/],
    [{ entry: { ...entry, size: undefined }, signature: entry.signature }, /缺 size/],
    [{ entry, signature: entry.signature, publicKeyPem: "-----BEGIN PUBLIC KEY-----\nX\n-----END PUBLIC KEY-----" }, /无法验证|不匹配/],
  ];
  for (const [input, pattern] of cases) {
    const check = verifyUpdateSignature({ ...input, publicKeyPem: input.publicKeyPem ?? TEST_PUBKEY });
    assert.equal(check.ok, false, JSON.stringify(input));
    assert.match(check.reason, pattern);
  }
});

test("别人的密钥签名不算数：默认公钥是壳里钉住的那把，不是测试自己的", () => {
  const entry = signedEntry();
  const pinned = verifyUpdateSignature({ entry, signature: entry.signature });
  assert.equal(pinned.ok, false, "自签的签名绝不能凭默认公钥通过");
  assert.match(pinned.reason, /不匹配/);
});

test("壳里钉住的公钥真的是一把可解析的 Ed25519 公钥", () => {
  const key = createPublicKey(UPDATE_PUBKEY_PEM);
  assert.equal(key.asymmetricKeyType, "ed25519");
});

test("decideUpdate 把 size/sbom/signature 原样带出，SBOM 不是 http(s) 就拒绝", () => {
  const entry = signedEntry({ sbom: "https://example.com/sbom.json" });
  const decision = decideUpdate({ current: "0.8.5", entry });
  assert.equal(decision.install, true);
  assert.equal(decision.size, 123456);
  assert.equal(decision.sbom, "https://example.com/sbom.json");
  assert.equal(decision.signature, entry.signature);

  const badSbom = decideUpdate({
    current: "0.8.5",
    entry: signedEntry({ sbom: "file:///D:/sbom.json" }),
  });
  assert.equal(badSbom.action, "unavailable");
  assert.match(badSbom.reason, /SBOM/);
  assert.equal(badSbom.install, false);
});
