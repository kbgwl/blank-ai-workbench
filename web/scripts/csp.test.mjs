import assert from "node:assert/strict";
import { test } from "node:test";

import { buildCsp, injectCsp, inlineScriptHashes } from "./csp.mjs";

test("外链脚本不进哈希，内联脚本按内容算 SHA-256", () => {
  const html = '<head><script>window.x = 1</script><script src="/a.js"></script></head>';
  const hashes = inlineScriptHashes(html);
  assert.equal(hashes.length, 1);
  assert.match(buildCsp(html), /'sha256-[A-Za-z0-9+/=]+'/);
});

test("script-src 不放 unsafe-inline，style-src 才放", () => {
  const csp = buildCsp("<head><script>1</script></head>");
  const script = csp.split(";").find((part) => part.trim().startsWith("script-src"));
  assert.ok(script && !script.includes("unsafe-inline"), script);
  assert.match(csp, /style-src 'self' 'unsafe-inline'/);
});

test("meta 注入在 head 内、脚本之前；重复注入只保留一条", () => {
  const html = "<html><head><title>x</title><script>1</script></head><body></body></html>";
  const once = injectCsp(html);
  const twice = injectCsp(once);
  assert.equal(once.match(/data-blank-csp/g)?.length, 1);
  assert.equal(twice.match(/data-blank-csp/g)?.length, 1);
  assert.ok(once.indexOf("data-blank-csp") < once.indexOf("<script>"));
  assert.ok(once.includes("frame-ancestors") === false, "frame-ancestors 不能写在 meta 里");
});
