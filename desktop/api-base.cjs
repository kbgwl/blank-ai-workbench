/**
 * 桌面壳的后端地址只有一个算法：显式 `BLANK_API_BASE` 优先，否则由
 * `BLANK_BACKEND_PORT` 拼出本机地址，端口缺省 8000。
 *
 * 抽成纯函数是为了能被单测钉住：前端构建时烘焙的 `NEXT_PUBLIC_API_BASE`
 * 只是没有壳注入时的兜底，运行时换端口必须两边读同一份结果。
 */
function resolveApiBase(env = process.env) {
  const explicit = String(env.BLANK_API_BASE ?? "").trim();
  if (explicit) return explicit.replace(/\/+$/, "");
  const port = String(env.BLANK_BACKEND_PORT ?? "").trim() || "8000";
  return `http://127.0.0.1:${port}`;
}

module.exports = { resolveApiBase };
