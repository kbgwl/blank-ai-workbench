"use client";

import {
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import { menuStyle, moveCursor, placementFor, type PopoverPlacement } from "./select-option";

/**
 * 全局统一下拉（v0.10.1 §5）。
 *
 * 为什么不用原生 `<select>`：浏览器默认菜单不吃主题（深色工作台弹出白色系统菜单）、
 * 不显示描述与选中态、也不能键盘定制。所有业务下拉必须走这一个组件，
 * 不允许每个页面再复制一份实现。
 *
 * - 选项带图标 / 标题 / 描述 / 选中态 / 禁用与警告；
 * - 键盘：↑↓ 移动、Enter 确认、Esc 关闭、Home/End 跳边界，焦点始终可控；
 * - 点击外部关闭；空间不足自动向上翻；portal 渲染，不被侧栏/面板裁剪；
 * - aria：haspopup/listbox/option + aria-activedescendant，屏幕阅读器可用。
 */
export type SelectOption = {
  value: string;
  label: string;
  /** 第二行说明（场景的"读写代码、运行测试"、权限的"仍需确认：删除、发布"）。 */
  description?: string;
  /** 行首图标。不传就渲染选中勾/占位圆点，保持行高一致。 */
  icon?: ReactNode;
  disabled?: boolean;
  /** 该选项的注意事项（如完全访问的边界），渲染在描述下面的警示行。 */
  warning?: string;
};

export type SelectPopoverProps = {
  value: string;
  options: SelectOption[];
  onChange: (value: string) => void;
  /** 触发器与菜单的尺寸档。compact 是输入栏工具条，regular 是设置页表单。 */
  size?: "compact" | "regular";
  /** 触发器无值时显示的占位文案。 */
  placeholder?: string;
  ariaLabel: string;
  disabled?: boolean;
  /** 触发器附加类名（宽度、颜色随场景微调用）。 */
  triggerClassName?: string;
  /** 菜单最小宽度；默认不小于触发器宽度。 */
  menuMinWidth?: number;
  /** 选中值在触发器上的自定义渲染（比如"名字 · 模型"）。 */
  renderValue?: (option: SelectOption | undefined) => ReactNode;
};

const MAX_MENU_HEIGHT = 320;

export function SelectPopover(props: SelectPopoverProps) {
  const {
    value,
    options,
    onChange,
    size = "regular",
    placeholder = "请选择",
    ariaLabel,
    disabled = false,
    triggerClassName = "",
    menuMinWidth,
    renderValue,
  } = props;

  const [open, setOpen] = useState(false);
  // 光标跟着"值+选项集"存：值或选项变了，键一变就自动回到当前选中项——
  // 不用 effect 里 setState 同步（那会被 react-hooks/set-state-in-effect 拦）。
  const cursorKey = `${value}\u0000${options.length}`;
  const [cursor, setCursor] = useState({ key: cursorKey, index: 0 });
  const index = cursor.key === cursorKey ? cursor.index : Math.max(0, options.findIndex((item) => item.value === value));
  const setIndex = (next: number | ((current: number) => number)) =>
    setCursor((current) => {
      const base = current.key === cursorKey ? current.index : Math.max(0, options.findIndex((item) => item.value === value));
      return { key: cursorKey, index: typeof next === "function" ? next(base) : next };
    });
  const [rect, setRect] = useState<{
    left: number;
    top: number;
    bottom: number;
    right: number;
    width: number;
    viewportWidth: number;
    viewportHeight: number;
    height: number;
  } | null>(null);
  const [placement, setPlacement] = useState<PopoverPlacement>("bottom-start");
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const listId = useId();

  const selected = options.find((item) => item.value === value);
  const pad = size === "compact" ? "px-2 py-1.5 text-[12.5px]" : "px-2.5 py-2 text-[13px]";

  // 打开时定位：锚点矩形 → 竖直方向翻面。用 layoutEffect 避免闪一帧错位。
  useLayoutEffect(() => {
    if (!open) return;
    const node = trigger.current;
    if (!node) return;
    const box = node.getBoundingClientRect();
    // 每行约 56px（标题 + 说明两行），上限 320；只用于判方向，实际高度由
    // menuStyle 按可用空间收，估偏了也不会溢出视口。
    const estimate = Math.min(MAX_MENU_HEIGHT, options.length * 56 + 12);
    const nextPlacement = placementFor(box.top, box.bottom, window.innerHeight, estimate);
    setPlacement(nextPlacement);
    setRect({
      left: box.left,
      top: box.top,
      bottom: box.bottom,
      right: box.right,
      width: box.width,
      viewportWidth: window.innerWidth,
      viewportHeight: window.innerHeight,
      height: estimate,
    });
    // 依赖只有 open：打开那一刻量一次就够，滚动/改窗时下次打开会重算。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // 打开时光标对到当前选中项（派生逻辑见 cursorKey，无需 effect）。

  // 点击外部关闭；Esc 一律先关菜单。
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent) => {
      const target = event.target as Node;
      if (trigger.current?.contains(target) || menu.current?.contains(target)) return;
      setOpen(false);
    };
    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, [open]);

  // 键盘移动后保持可见行滚进视口。
  useEffect(() => {
    if (!open) return;
    menu.current?.querySelector<HTMLElement>(`[data-row="${index}"]`)?.scrollIntoView({ block: "nearest" });
  }, [index, open]);

  const commit = (option: SelectOption | undefined) => {
    if (!option || option.disabled) return;
    onChange(option.value);
    setOpen(false);
    trigger.current?.focus();
  };

  const onTriggerKeyDown = (event: React.KeyboardEvent) => {
    if (disabled) return;
    if (!open) {
      if (event.key === "ArrowDown" || event.key === "ArrowUp" || event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        setOpen(true);
      }
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      setOpen(false);
      return;
    }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setIndex((current) => moveCursor(current, 1, options.length));
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      setIndex((current) => moveCursor(current, -1, options.length));
      return;
    }
    if (event.key === "Home") {
      event.preventDefault();
      setIndex(0);
      return;
    }
    if (event.key === "End") {
      event.preventDefault();
      setIndex(Math.max(0, options.length - 1));
      return;
    }
    if (event.key === "Enter") {
      event.preventDefault();
      commit(options[index]);
    }
  };

  const position = rect
    ? menuStyle(
        placement,
        { left: rect.left, top: rect.top, bottom: rect.bottom, right: rect.right },
        { width: rect.viewportWidth, height: rect.viewportHeight },
        rect.height,
      )
    : null;

  return (
    <>
      <button
        ref={trigger}
        type="button"
        disabled={disabled}
        aria-label={ariaLabel}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        onClick={() => !disabled && setOpen((current) => !current)}
        onKeyDown={onTriggerKeyDown}
        title={selected ? `${ariaLabel}：${selected.label}` : ariaLabel}
        className={`flex min-w-0 max-w-full shrink items-center gap-1 truncate rounded-lg bg-transparent transition-colors hover:bg-surface-2 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/30 disabled:opacity-40 ${pad} ${
          selected ? "text-muted" : "text-faint"
        } ${triggerClassName}`}
      >
        <span className="min-w-0 truncate">
          {renderValue ? renderValue(selected) : (selected?.label ?? placeholder)}
        </span>
        <svg
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth={2}
          strokeLinecap="round"
          strokeLinejoin="round"
          className={`size-3 shrink-0 text-faint transition-transform ${open ? "rotate-180" : ""}`}
          aria-hidden
        >
          <path d="M6 9l6 6 6-6" />
        </svg>
      </button>

      {open &&
        rect !== null &&
        createPortal(
          <div
            ref={menu}
            id={listId}
            role="listbox"
            aria-label={ariaLabel}
            tabIndex={-1}
            style={{
              position: "fixed",
              left: position?.left,
              right: position?.right,
              top: position?.top,
              bottom: position?.bottom,
              // min-width 会压过 max-width（CSS 规定），所以上限更小时要把它一起收住，
              // 否则窄屏/贴边时菜单仍会横向溢出。
              minWidth: Math.min(
                Math.max(menuMinWidth ?? 0, rect.width),
                position?.maxWidth ?? Number.POSITIVE_INFINITY,
              ),
              maxWidth: position?.maxWidth,
              maxHeight: Math.min(MAX_MENU_HEIGHT, position?.maxHeight ?? MAX_MENU_HEIGHT),
            }}
            className="z-50 overflow-y-auto rounded-xl border border-line bg-surface py-1 shadow-[0_12px_32px_-12px_rgba(16,24,40,0.28)]"
          >
            {options.length === 0 && (
              <p className="px-3 py-3 text-center text-[11.5px] text-faint">没有可选项</p>
            )}
            {options.map((option, row) => {
              const isSelected = option.value === value;
              const isCursor = row === index;
              return (
                <button
                  key={option.value}
                  type="button"
                  role="option"
                  aria-selected={isSelected}
                  data-row={row}
                  disabled={option.disabled}
                  title={option.label}
                  onMouseEnter={() => setIndex(row)}
                  onClick={() => commit(option)}
                  className={`flex w-full items-start gap-2 px-2.5 py-1.5 text-left transition-colors disabled:opacity-40 ${
                    isCursor ? "bg-surface-2" : ""
                  }`}
                >
                  <span className="mt-[3px] grid size-4 shrink-0 place-items-center text-brand">
                    {option.icon ??
                      (isSelected ? (
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.4} strokeLinecap="round" strokeLinejoin="round" className="size-3.5" aria-hidden>
                          <path d="M4 12l5 5L20 6" />
                        </svg>
                      ) : (
                        <span className="size-1.5 rounded-full bg-line-strong" aria-hidden />
                      ))}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className={`block truncate text-[12.5px] ${isSelected ? "text-text" : "text-text/90"}`}>
                      {option.label}
                    </span>
                    {option.description && (
                      <span className="block truncate text-[11px] text-muted">{option.description}</span>
                    )}
                    {option.warning && (
                      <span className="block truncate text-[11px] text-warn">{option.warning}</span>
                    )}
                  </span>
                </button>
              );
            })}
          </div>,
          document.body,
        )}
    </>
  );
}
