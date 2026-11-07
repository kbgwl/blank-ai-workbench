import assert from "node:assert/strict";
import { test } from "node:test";

import { menuStyle, moveCursor, placementFor } from "./select-option.ts";

test("光标在列表里循环移动，空列表原地不动", () => {
  assert.equal(moveCursor(0, 1, 3), 1);
  assert.equal(moveCursor(2, 1, 3), 0);
  assert.equal(moveCursor(0, -1, 3), 2);
  assert.equal(moveCursor(0, 1, 0), 0);
});

test("下方放得就一直朝下弹", () => {
  // 触发器在视口上部，下面一大片空间。
  assert.equal(placementFor(100, 130, 900, 320), "bottom-start");
});

test("下方不够、上方更宽裕时向上翻", () => {
  // 触发器贴近视口底部：上面 600px，下面只剩 60px。
  assert.equal(placementFor(280, 820, 900, 320), "top-start");
});

test("两边都不够时朝下弹（贴边也比遮住触发器好）", () => {
  // 上 380 / 下 490，菜单要 500：下面差一点、上面更不够 → 朝下。
  assert.equal(placementFor(380, 410, 900, 500), "bottom-start");
});

test("向下弹贴着锚点下缘留缝，并按下方可用空间收 maxHeight", () => {
  const style = menuStyle(
    "bottom-start",
    { left: 20, top: 100, bottom: 130, right: 140 },
    { width: 1000, height: 900 },
    320,
  );
  assert.equal(style.left, 20);
  assert.equal(style.top, 136);
  assert.equal(style.bottom, undefined);
  assert.equal(style.right, undefined);
  // 下方可用 900-130-6-8=756，菜单 320 → 取 320，不无谓缩小。
  assert.equal(style.maxHeight, 320);
  assert.equal(style.maxWidth, 1000 - 20 - 8);
});

test("向上弹用 bottom 贴住锚点上沿，菜单多高都不会漏出视口底部", () => {
  // 输入栏贴着视口底部（§10 场景下拉的真实位置）：锚点 top=820，视口 900。
  const style = menuStyle(
    "top-start",
    { left: 645, top: 820, bottom: 850, right: 727 },
    { width: 1440, height: 900 },
    320,
  );
  // 菜单底边在锚点上沿往上 6px：bottom = 900-820+6 = 86。
  assert.equal(style.bottom, 86);
  assert.equal(style.top, undefined);
  // 右对齐锚点：right = 1440-727 = 713，于是菜单向左展开，
  // 右边缘停在 727 —— 不会盖住右侧的发送按钮。
  assert.equal(style.right, 713);
  assert.equal(style.left, undefined);
  // 上方可用 820-6-8=806，菜单 320 → 320。
  assert.equal(style.maxHeight, 320);
});

test("上方空间不足时把 maxHeight 收到可用空间，菜单内部滚动而不是溢出", () => {
  const style = menuStyle(
    "top-start",
    { left: 20, top: 200, bottom: 230, right: 120 },
    { width: 1000, height: 900 },
    320,
  );
  assert.equal(style.bottom, 706);
  // 上方可用 200-6-8=186 → 收到 186。
  assert.equal(style.maxHeight, 186);
});

test("向下弹靠底部时同样收紧，不越出视口下沿", () => {
  // 锚点底 850、视口 900：下方可用 900-850-6-8=36 → 收到 36。
  // 这种极端位置 placementFor 本来就会翻到上方，这里只保证"即便朝下也不溢出"。
  const style = menuStyle(
    "bottom-start",
    { left: 20, top: 820, bottom: 850, right: 140 },
    { width: 1000, height: 900 },
    320,
  );
  assert.equal(style.top, 856);
  assert.equal(style.maxHeight, 36);
});
