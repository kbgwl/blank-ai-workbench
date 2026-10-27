"use strict";

/**
 * 自动更新骨架的判定测试（decideUpdate / 版本比较 / feed 解析）。
 *
 * 守的核心契约只有一句话：**这个骨架在任何分支都不安装**（install 恒为 false），
 * 且每种「不能更新」都要给出人能读懂的原因。下面最后一条把不变量扫成循环——
 * 哪天有人接上真实下载却忘了收口，这条先红。
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

test("feed 形态：数组 / {entries:[...]} / 单条目对象都收，其余给 null", () => {
  const entry = { version: "0.9.0", url: "https://x/y", sha256: SHA_OK };
  assert.deepEqual(extractFeedEntries([entry]), [entry]);
  assert.deepEqual(extractFeedEntries({ entries: [entry] }), [entry]);
  assert.deepEqual(extractFeedEntries(entry), [entry]);
  assert.equal(extractFeedEntries("0.9.0"), null);
  assert.equal(extractFeedEntries({ note: "今天不发版" }), null);
  assert.equal(extractFeedEntries(null), null);
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

test("一切合规也仅仅是 available：骨架只给提示，明写不下载不安装", () => {
  const decision = decideUpdate({
    current: "0.8.5",
    entry: { version: "0.9.0", url: "https://example.com/setup-0.9.0.exe", sha256: SHA_OK.toUpperCase() },
  });
  assert.equal(decision.action, "available");
  assert.equal(decision.version, "0.9.0");
  assert.equal(decision.sha256, SHA_OK, "摘要归一成小写十六进制，比对时大小写不该咬人");
  assert.match(decision.reason, /不下载、不安装/);
  assert.equal(decision.install, false);
});

test("不变量：把每条分支都走一遍，install 没有一次为真", () => {
  const inputs = [
    { current: "0.8.5", entry: { version: "0.9.0", url: "https://x", sha256: SHA_OK } },
    { current: "0.8.5", entry: { version: "0.8.5" } },
    { current: "0.8.5", entry: {}, fetchError: "超时" },
    { current: "0.8.5", entry: null },
    { current: "坏版本", entry: { version: "0.9.0" } },
    { current: undefined, entry: undefined, fetchError: undefined },
  ];
  for (const input of inputs) {
    const decision = decideUpdate(input);
    assert.ok(["available", "none", "unavailable"].includes(decision.action), JSON.stringify(decision));
    assert.equal(decision.install, false, `出现了会安装的分支：${JSON.stringify(input)}`);
    assert.equal(typeof decision.reason, "string");
    assert.ok(decision.reason.length > 0, "每一条判定都得说人话");
  }
});
