// fetchJson 的重试判据：GET 撞上连接被杀必须重试，POST 必须只发一次。
//
// 这条缺陷在 2026-11-16 严格评测里真实发生过：后端任务已经 failed 在案，
// runner 读 /evidence 的连接被服务端关掉，fetch 抛 fetch failed，外层把它
// 记成 provider_failed 并重跑了整例——服务端访问日志里根本没有那条请求。
import test from "node:test";
import assert from "node:assert/strict";
import http from "node:http";

import { fetchJson } from "../lib/fetch-json.mjs";

function startServer(handler) {
  return new Promise((resolve) => {
    const hits = [];
    const server = http.createServer((req, res) => {
      hits.push(req.method);
      handler(req, res, hits.length);
    });
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      resolve({ server, hits, url: `http://127.0.0.1:${port}/probe` });
    });
  });
}

function stop(server) {
  server.closeAllConnections?.();
  server.close();
}

test("GET 的连接在响应前被杀：重试后拿到结果", async () => {
  const { server, hits, url } = await startServer((req, res, n) => {
    if (n === 1) {
      req.socket.destroy();
      return;
    }
    res.writeHead(200, { "content-type": "application/json" });
    res.end('{"ok":true}');
  });
  try {
    const r = await fetchJson(url, { delayMs: 10 });
    assert.equal(r.status, 200);
    assert.deepEqual(r.json, { ok: true });
    assert.deepEqual(hits, ["GET", "GET"]);
  } finally {
    stop(server);
  }
});

test("负向自检：attempts=1 时同样的断连必须抛错（重试才是救它的原因）", async () => {
  const { server, hits, url } = await startServer((req, res, n) => {
    if (n === 1) {
      req.socket.destroy();
      return;
    }
    res.writeHead(200, { "content-type": "application/json" });
    res.end('{"ok":true}');
  });
  try {
    await assert.rejects(fetchJson(url, { attempts: 1, delayMs: 10 }));
    assert.deepEqual(hits, ["GET"]);
  } finally {
    stop(server);
  }
});

test("POST 不重试：连接被杀就抛错，服务端只收到一次", async () => {
  const { server, hits, url } = await startServer((req, res) => {
    req.socket.destroy();
  });
  try {
    await assert.rejects(fetchJson(url, { method: "POST", body: { a: 1 }, delayMs: 10 }));
    assert.deepEqual(hits, ["POST"]);
  } finally {
    stop(server);
  }
});

test("非 JSON 响应不吞掉：原样放进 raw", async () => {
  const { server, url } = await startServer((req, res) => {
    res.writeHead(502, { "content-type": "text/plain" });
    res.end("bad gateway");
  });
  try {
    const r = await fetchJson(url, { delayMs: 10 });
    assert.equal(r.status, 502);
    assert.equal(r.json.raw, "bad gateway");
  } finally {
    stop(server);
  }
});
