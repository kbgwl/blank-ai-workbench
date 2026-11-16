/**
 * 长请求 HTTP 客户端：node:http(s)，不设客户端超时。
 *
 * 全局 fetch 走 undici，headersTimeout / bodyTimeout 默认都是 300s。`POST /tasks/{id}/run`
 * 跑到终态才返回，一超 5 分钟就被掐断成 `fetch failed`（实测 cause=HeadersTimeoutError），
 * 调用方会把「任务还在跑」误判成「供应商失败」并重跑（2026-11-16 实测 coding-021 t1）。
 */
import { request as httpRequest } from "node:http";
import { request as httpsRequest } from "node:https";

export function requestLong(url, method = "GET", body) {
  return new Promise((resolve, reject) => {
    const target = url instanceof URL ? url : new URL(url);
    const req = (target.protocol === "https:" ? httpsRequest : httpRequest)(
      target,
      { method, headers: { "content-type": "application/json" } },
      (res) => {
        let text = "";
        res.setEncoding("utf8");
        res.on("data", (chunk) => (text += chunk));
        res.on("end", () => {
          let json;
          try {
            json = JSON.parse(text);
          } catch {
            json = { raw: text };
          }
          resolve({ status: res.statusCode ?? 0, json });
        });
      }
    );
    req.on("error", reject);
    if (body !== undefined) req.write(JSON.stringify(body));
    req.end();
  });
}
