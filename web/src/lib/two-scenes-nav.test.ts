import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { COMMANDS } from "./commands.ts";

/**
 * 产品只保留两个场景：编程（对话）与日常。头脑风暴 / 逆向实验室 / 能力页
 * 的源码文件按老任务兼容留在仓库里，但不允许再出现在任何可见入口上——
 * 命令面板、侧边导航、路由标题表、首页侧栏、工作台侧栏。
 *
 * 这条测试钉的就是"入口"，不碰页面文件本身。以前清理过两轮又被人加回来，
 * 靠人记不住，靠这条断言挡。
 */
const OLD_ROUTES = ["/brainstorm", "/reverse-lab", "/features"];

function source(relativeToLib: string): string {
  return readFileSync(fileURLToPath(new URL(relativeToLib, import.meta.url)), "utf8");
}

test("全局命令清单不含已下线场景", () => {
  const hrefs = COMMANDS.flatMap((item) => (item.href ? [item.href] : []));
  for (const route of OLD_ROUTES) {
    assert.ok(!hrefs.some((href) => href.startsWith(route)), `命令清单仍含旧入口 ${route}`);
  }
});

test("侧边导航 / 首页侧栏 / 工作台侧栏不含已下线场景链接", () => {
  const entryFiles = [
    "../components/side-nav.tsx",
    "../components/chat/chat-home.tsx",
    "../components/workbench/workbench.tsx",
  ];
  for (const file of entryFiles) {
    const code = source(file);
    for (const route of OLD_ROUTES) {
      assert.ok(!code.includes(`href="${route}"`), `${file} 仍链到旧场景 ${route}`);
    }
  }
});

test("路由标题表不含已下线场景", () => {
  const code = source("../components/app-shell.tsx");
  for (const route of OLD_ROUTES) {
    assert.ok(!code.includes(`"${route}":`), `app-shell 仍为旧场景 ${route} 保留标题`);
  }
});
