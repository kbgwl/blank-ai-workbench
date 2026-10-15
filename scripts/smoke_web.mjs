#!/usr/bin/env node
// 阶段7 前端可用性检查：逐页取回 HTML，断言关键标记、无未渲染错误，并检查后端连通。
// 用法： node scripts/smoke_web.mjs [--base http://localhost:3000]
const base = process.argv.includes("--base")
  ? process.argv[process.argv.indexOf("--base") + 1]
  : "http://localhost:3000";
const healthUrl = `${base.replace(/:3000$/, ":8000")}/api/v1/health`;

const pages = [
  { path: "/", markers: ["空白格", "示例数据"] },
  { path: "/projects", markers: ["项目", "工作区"] },
  { path: "/tasks", markers: ["任务"] },
  { path: "/settings", markers: ["模型", "供应商"] },
];

const failures = [];

async function checkPage(page) {
  const url = `${base}${page.path}`;
  let html = "";
  try {
    const response = await fetch(url);
    html = await response.text();
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
  } catch (error) {
    failures.push(`${page.path}: 页面不可访问（${error.message}）`);
    return;
  }
  for (const marker of page.markers) {
    if (!html.includes(marker)) failures.push(`${page.path}: 缺少关键标记「${marker}」`);
  }
  for (const bad of ["Application error", "Unhandled Runtime Error", "does not have a constructor"]) {
    if (html.includes(bad)) failures.push(`${page.path}: 页面渲染出错（${bad}）`);
  }
}

try {
  const health = await (await fetch(healthUrl)).json();
  if (health.status !== "ok") failures.push(`/health 返回 ${JSON.stringify(health)}`);
} catch (error) {
  failures.push(`后端健康检查失败：${error.message}`);
}

for (const page of pages) await checkPage(page);

if (failures.length) {
  console.error("前端可用性检查未通过：");
  for (const line of failures) console.error(`  - ${line}`);
  process.exit(1);
}
console.log(`前端可用性检查通过：${pages.length} 个页面 + 后端连通`);
