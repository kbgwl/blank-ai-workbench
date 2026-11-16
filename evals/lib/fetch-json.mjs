/**
 * 幂等 JSON 请求：GET 在 keep-alive 竞争下会抛 `fetch failed`。
 *
 * undici 复用连接；服务端（uvicorn 默认 5s）关掉空闲连接与客户端发出下一次请求
 * 之间存在竞态，fetch 直接抛 TypeError: fetch failed（cause ECONNRESET）。
 * 2026-11-16 严格评测实测：coding-008 t1 的后端任务 1bc0bd26 已经 failed 在案，
 * runner 读 /evidence 时撞上这个竞争，服务端访问日志里根本没有这条请求，外层
 * catch 又把它归成「供应商失败」并重跑了整例（新任务 4450c7a0）。
 *
 * 只对 GET 重试：POST 不幂等（建项目/建任务重复发会造出两个任务）。重试用完仍
 * 失败就把最后一个错误抛给调用方，由它决定怎么记账。
 */
export async function fetchJson(url, options = {}) {
  const { method = "GET", body, attempts = 4, delayMs = 250, fetchImpl = fetch } = options;
  const tries = method === "GET" ? Math.max(1, attempts) : 1;
  let lastError;
  for (let i = 0; i < tries; i += 1) {
    try {
      const resp = await fetchImpl(url, {
        method,
        headers: { "content-type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      const text = await resp.text();
      let json;
      try {
        json = JSON.parse(text);
      } catch {
        json = { raw: text };
      }
      return { status: resp.status, json };
    } catch (error) {
      lastError = error;
      if (i + 1 < tries) await new Promise((r) => setTimeout(r, delayMs * (i + 1)));
    }
  }
  throw lastError;
}
