// 注册（或复用）本地脚本化模型供应商，供端到端演示与评测在缺少真实 API Key 时使用
// 用法：node scripts/register_mock_provider.mjs [mockBaseUrl]
const API = process.env.BLANK_API_BASE ?? "http://localhost:8000/api/v1";
const base = process.argv[2] ?? "http://127.0.0.1:8099/v1";

async function req(method, path, body) {
  const res = await fetch(`${API}${path}`, {
    method,
    headers: { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  if (!res.ok) throw new Error(`${method} ${path} -> ${res.status} ${text.slice(0, 300)}`);
  return text ? JSON.parse(text) : null;
}

const name = "本地脚本化模型（演示用）";
const existing = await req("GET", "/providers");
let provider = existing.find((item) => item.name === name);
if (!provider) {
  provider = await req("POST", "/providers", {
    name,
    kind: "openai_compatible",
    base_url: base,
    model: "mock-coder",
    api_key: "mock-key-not-a-secret",
    supports_tools: true,
  });
} else if (provider.base_url !== base) {
  provider = await req("PATCH", `/providers/${provider.id}`, { base_url: base });
}

const test = await req("POST", `/providers/${provider.id}/test`, {});
console.log(JSON.stringify({ id: provider.id, ok: test.ok, model: test.model, message: test.message ?? "ok" }));
