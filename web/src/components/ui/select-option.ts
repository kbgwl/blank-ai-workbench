/**
 * SelectPopover 的纯逻辑：光标移动与浮层定位。
 *
 * 单独拆成 .ts 是为了让"上下键怎么走、菜单往哪边弹"能进 node:test——
 * 组件渲染没法在这套测试装置里跑，但这两件事恰恰是最容易写歪的
 * （边界循环、翻转方向各错一次，用户就会遇到"选不中"和"菜单把字挡住"）。
 */

export type PopoverPlacement = "bottom-start" | "top-start";

/** 光标在列表里循环移动；空列表原地不动。 */
export function moveCursor(current: number, delta: number, length: number): number {
  if (length <= 0) return 0;
  const next = (current + delta) % length;
  return next < 0 ? next + length : next;
}

/**
 * 菜单往哪边弹：默认贴在触发器下方；下方空间不够、且上方比下方宽裕时向上翻。
 * ``menuHeight`` 是估算值（每行约 56px：标题 + 说明两行，封顶 320px），不必精确——
 * 方向只是初判，真正的高度上限由 ``menuStyle`` 按可用空间收，永不出视口。
 */
export function placementFor(
  anchorTop: number,
  anchorBottom: number,
  viewportHeight: number,
  menuHeight: number,
): PopoverPlacement {
  const spaceBelow = viewportHeight - anchorBottom;
  const spaceAbove = anchorTop;
  if (spaceBelow >= menuHeight) return "bottom-start";
  if (spaceAbove > spaceBelow) return "top-start";
  return "bottom-start";
}

export type AnchorRect = { left: number; top: number; bottom: number; right: number };
export type Viewport = { width: number; height: number };

export type MenuStyle = {
  /** 向下弹：左对齐锚点。 */
  left?: number;
  /** 向上弹：右对齐锚点（向左展开，不盖住右侧的发送按钮）。 */
  right?: number;
  top?: number;
  /** 向上弹用 bottom 贴住锚点上沿，不需要预先知道菜单多高。 */
  bottom?: number;
  maxWidth: number;
  /** 可用空间上限，配合 overflow-y-auto 保证菜单不越出视口。 */
  maxHeight: number;
};

const GAP = 6;
const EDGE = 8;

/**
 * 浮层的 fixed 定位样式。
 *
 * 两个真实踩过的坑（v0.10.1 GUI 验收实测）：
 *
 * 1. 向上弹不能用 ``top = anchorTop - 菜单高度``——那需要先知道高度，高度又依赖
 *    渲染，估错了菜单就盖住触发器、并从视口底部漏出去。改用 ``bottom`` 贴住
 *    锚点上沿，菜单自己往上长，多高都不越界。
 * 2. 向上弹必须**右对齐**锚点。输入栏的工具条在输入框内部右下角，左对齐展开
 *    会横着盖住右边的发送按钮；右对齐向左展开只盖住输入框空白区，选完即收。
 *
 * ``menuHeight`` 只用来收 maxHeight：空间不够时菜单内部滚动，而不是溢出屏幕。
 */
export function menuStyle(
  placement: PopoverPlacement,
  anchor: AnchorRect,
  viewport: Viewport,
  menuHeight: number,
): MenuStyle {
  if (placement === "top-start") {
    const right = Math.max(EDGE, viewport.width - anchor.right);
    const spaceAbove = Math.max(0, anchor.top - GAP - EDGE);
    return {
      right,
      bottom: Math.max(EDGE, viewport.height - anchor.top + GAP),
      maxWidth: Math.max(160, viewport.width - right - EDGE),
      maxHeight: Math.min(menuHeight, spaceAbove),
    };
  }
  const left = Math.min(Math.max(EDGE, anchor.left), Math.max(EDGE, viewport.width - EDGE - 160));
  const spaceBelow = Math.max(0, viewport.height - anchor.bottom - GAP - EDGE);
  return {
    left,
    top: anchor.bottom + GAP,
    maxWidth: Math.max(160, viewport.width - left - EDGE),
    maxHeight: Math.min(menuHeight, spaceBelow),
  };
}
