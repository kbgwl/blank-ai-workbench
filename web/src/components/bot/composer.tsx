"use client";

import { useEffect, useRef, useState } from "react";
import type { Project, Provider, ScenarioInfo, Workspace } from "../tasks/api";
import { SelectPopover, type SelectOption } from "../ui/select-popover";
import { AtPicker } from "./at-picker";
import { SlashMenu } from "./slash-menu";
import { matchSlash, type SlashCommand } from "@/lib/slash-commands";
import { NEW_CHAT_EVENT } from "@/lib/commands";
import { DraftImageChips } from "./image-chips";
import { insertReference } from "./picker";
import { composerAction } from "./turns";
import { ReferenceChips } from "./reference-chips";
import { referencesIn } from "./references";
import { ACCEPT_ATTR, filesFrom, type DraftAttachment } from "./uploads";

export type ComposerProps = {
  projects: Project[];
  providers: Provider[];
  projectId: string;
  onProjectChange: (id: string) => void;
  /** 当前项目已绑定的目录。空数组表示还没绑——这时候一眼就能看出来"没地方干活"。 */
  workspaces: Workspace[];
  /**
   * 项目与目录是否已经读回来。
   *
   * 静态导出的首屏里 boot 还是空的，`workspaces.length === 0` 只是"还没读到"，
   * 不是"这个项目没绑"。拿它写占位文案会让首屏先喊一句"还没有工作目录"，
   * 数据回来又改口——用户看到的是自己的目录被否认了一次。
   */
  workspaceReady?: boolean;
  /** 切到另一个已绑定的目录（不换项目、不换会话）。 */
  onActivateWorkspace: (id: string) => void;
  /** 手动输入一个本地目录：绑定到当前项目并立刻切过去。 */
  onBindWorkspacePath: (path: string) => void;
  /** 当前项目名。Codex 桌面版在输入框上方就这么标：`项目 | Local | 分支`。 */
  projectName: string;
  /** 弹目录选择框并绑定。桌面壳里才有系统对话框，浏览器里由上层决定怎么降级。 */
  onPickWorkspace: () => void;
  /** 是否具备"挑一个新目录"的能力（桌面壳）。没有就不渲染那个选项。 */
  canPickWorkspace: boolean;
  providerId: string;
  onProviderChange: (id: string) => void;
  /**
   * 没手动选模型时，实际会用到的那个型号名。
   *
   * 后端按"角色分工"自己挑，前端不知道规则，所以把默认模型的名字显示在选择器上：
   * 顶部那条状态栏撤掉之后，模型名只能从这里看见，不能再显示成含糊的「默认模型」。
   */
  defaultModelLabel?: string;
  /** 场景目录（GET /scenarios）。加载失败时为空数组——选择器隐藏，行为回落开发场景。 */
  scenarios: ScenarioInfo[];
  scenarioKey: string;
  onScenarioChange: (key: string) => void;
  /** 仅日常场景显示：用户是否允许 AI 记住习惯（后端凭它开放 memory_write）。 */
  memoryOptIn: boolean;
  onMemoryOptInChange: (value: boolean) => void;
  /**
   * 发送栏的权限档（v0.10.1 §4）：default=询问 / bypass=完全访问 / plan=计划。
   * 它随这一轮的任务快照固定——任务创建后再改设置页默认档不影响这轮。
   */
  permissionMode: string;
  onPermissionChange: (value: string) => void;
  draft: string;
  onDraftChange: (value: string) => void;
  /** 草稿里的图片。上传完成的才有 id，上传中的不能发送。 */
  images: DraftAttachment[];
  onAddImages: (files: File[]) => void;
  onRemoveImage: (key: string) => void;
  onSend: () => void;
  /** 运行中按回车走插话，而不是再开一轮。 */
  onSteer: () => void;
  onStop: () => void;
  running: boolean;
  /** 发送失败等就地提示。 */
  error: string | null;
  /** 中性提示，比如"已排队"。 */
  hint: string | null;
  disabled?: boolean;
};

export function Composer(props: ComposerProps) {
  const textarea = useRef<HTMLTextAreaElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const [picking, setPicking] = useState(false);
  // 附件菜单：引用文件 / 上传图片都收在「+」里。以前并排摆一个 @ 和一个相框图，
  // 相框那个是十年前的写法，主流产品早就不用单独的图片按钮了。
  const [attachOpen, setAttachOpen] = useState(false);
  const attachRef = useRef<HTMLDivElement>(null);
  // 手动输入本地目录：浏览器里没有原生目录框，这是唯一能选任意目录的入口。
  // null = 收起；空串 = 刚打开还没填。
  const [pathEntry, setPathEntry] = useState<string | null>(null);
  // 斜杠面板的光标。面板不抢焦点，键盘事件仍由 textarea 收。
  const [slashIndex, setSlashIndex] = useState(0);
  const [dragging, setDragging] = useState(false);
  // 拖拽进入子元素会再触发一次 dragenter，只用布尔量会让高亮在图片条上闪烁。
  const dragDepth = useRef(0);

  // 自适应高度：起点就是三行，最多长到九行，再多就内部滚动。
  // 一行的输入框放在对话区底部，四周全是留白，看着像"没做完"；
  // 三行既能把常见的一句话摊开，也给了粘贴多行时的余地。
  //
  // 空内容时不写内联高度，交给 `rows={3}` 自己撑开：量 scrollHeight 会漏掉
  // 字体加载前的行高，写死一个偏小的高度会把占位文案截掉半行。
  useEffect(() => {
    const node = textarea.current;
    if (!node) return;
    if (!props.draft) {
      node.style.height = "";
      return;
    }
    node.style.height = "auto";
    node.style.height = `${Math.min(node.scrollHeight, 9 * 26 + 28)}px`;
  }, [props.draft]);

  // 附件菜单点外面收起。捕获取 mousedown 而不是 click：拖动选择文本时不该触发菜单。
  useEffect(() => {
    if (!attachOpen) return;
    const close = (event: MouseEvent) => {
      if (attachRef.current && !attachRef.current.contains(event.target as Node)) setAttachOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [attachOpen]);

  // 斜杠命令：只有输入以 / 开头且还没打空格时才出现。
  const slash = matchSlash(props.draft);
  const slashItems = slash?.items ?? [];
  // 面板开着时不能用越界的旧下标（换了关键词命令会变少）。
  const slashCursor = slashItems.length === 0 ? 0 : Math.min(slashIndex, slashItems.length - 1);

  function runCommand(command: SlashCommand) {
    if (command.action === "new-chat") {
      window.dispatchEvent(new CustomEvent(NEW_CHAT_EVENT));
      props.onDraftChange("");
    } else if (command.action === "clear") {
      props.onDraftChange("");
    } else if (command.action === "plan-mode") {
      props.onPermissionChange("plan");
      props.onDraftChange("");
    } else if (command.href) {
      window.location.href = command.href;
      props.onDraftChange("");
    }
    setSlashIndex(0);
    textarea.current?.focus();
  }

  /**
   * 真正能用的模型：启用了**并且**填了 Key。
   *
   * 之前把 7 家供应商全列出来，而它们默认 enabled 却没 Key——用户看到一长串、
   * 选完发消息才发现不能用。选择器只该出现"现在就能发出去的"。
   */
  const usableProviders = props.providers.filter(
    (provider) => provider.enabled && (provider.api_key_hint ?? "") !== "",
  );
  const unconfigured = props.providers.filter(
    (provider) => provider.enabled && (provider.api_key_hint ?? "") === "",
  );

  const readyImages = props.images.filter((image) => image.id && !image.uploading && !image.error);
  const uploading = props.images.some((image) => image.uploading);
  // 只有图没有字也能发："照这张图改"里的"这张图"本身就是全部意思。
  // 上传中不能发：这时候发出去会是一条只带文字的轮次，图被悄悄丢掉。
  const canSend =
    (props.draft.trim().length > 0 || readyImages.length > 0) && !props.disabled && !uploading;
  const action = composerAction(props.running, canSend);

  // 后端在没有 is_active 时会回落到第一个，这里保持一致：显示的那个必须就是真正会用的那个。
  const activeWorkspace = props.workspaces.find((item) => item.is_active) ?? props.workspaces[0] ?? null;
  /**
   * 「在哪儿干活」= 选一个本地目录。
   *
   * 逆向 Codex 之后的结论：它**没有"项目"这一层**，只有 `-C/--cd <DIR>` 指定工作根，
   * 目录是会话的属性（`codex resume` 换的是会话，不是换项目）。我们之前把"选目录"
   * 做成了"切项目"，结果是换目录连带把会话列表清空了——用户看到的就是"工作区内容没了"。
   *
   * 所以这里只列**当前项目**的目录；换目录不换项目、不换会话。
   */
  const workspaceOptions: SelectOption[] = [
    // 桌面版能开系统目录框；浏览器里没有原生框，所以第二条永远存在。
    ...(props.canPickWorkspace
      ? [{ value: "__pick__", label: "选择另一个目录…", description: "打开系统目录选择框" }]
      : []),
    { value: "__path__", label: "输入本地目录路径…", description: "例如 E:/projects/demo" },
    ...props.workspaces.map((workspace) => ({
      value: `ws:${workspace.id}`,
      label: workspace.label,
      description: looksLikePath(workspace.path)
        ? workspace.path
        : "在这个目录里读写文件、跑命令",
    })),
  ];

  /** 永远可点：至少还有「输入本地目录路径…」这条入口，不然浏览器里等于不能换目录。 */
  const workspaceSelectable = true;

  // 场景联动（任务书 §8.2）：换场景时输入框提示词跟着换；记忆开关只在日常场景出现。
  const activeScenario = props.scenarios.find((item) => item.key === props.scenarioKey);
  const placeholderText =
    activeScenario?.inputHint || "说点什么，或者直接贴张图/丢个文件进来…";

  // v0.10.1 §4.2：三种权限的中文文案与范围说明。选项顺序即建议顺序。
  const permissionOptions: SelectOption[] = [
    {
      value: "default",
      label: "询问",
      description: "自动允许读取、分析和低风险检查",
      warning: "修改文件、装依赖、高风险命令前先问你",
    },
    {
      value: "bypass",
      label: "完全访问",
      description: "工作区内自动读写文件、跑测试、装依赖、自动修复",
      warning: "仍需确认：大量删除、工作区外路径、发布、不可逆操作",
    },
    {
      value: "plan",
      label: "计划",
      description: "只读取、分析、生成计划和建议",
      warning: "不修改文件、不执行命令",
    },
  ];

  return (
    <div className="bg-canvas px-4 pt-3 pb-3.5">
      <div className="mx-auto w-full max-w-3xl">
        {props.error && (
          <p className="mb-2 rounded-lg border border-danger/25 bg-danger/[0.04] px-2.5 py-1.5 text-[12px] text-danger">
            {props.error}
          </p>
        )}
        {props.hint && (
          <p className="mb-2 rounded-lg border border-line bg-surface-2 px-2.5 py-1.5 text-[12px] text-muted">
            {props.hint}
          </p>
        )}

        {/* 没有分隔线，靠边框 + 一点投影把卡片从白底上托起来：页面和白卡同色，
            只留一条线的话线一去掉盒子就"化"进背景里了。 */}
        <div
          className={`relative rounded-xl border bg-surface shadow-sm transition-colors focus-within:border-brand/45 focus-within:shadow-md ${
            dragging ? "border-brand/60 ring-2 ring-brand/15" : "border-line-strong"
          }`}
          onDragEnter={(event) => {
            if (!event.dataTransfer?.types?.includes("Files")) return;
            event.preventDefault();
            dragDepth.current += 1;
            setDragging(true);
          }}
          onDragOver={(event) => {
            // 不 preventDefault 的话浏览器会直接打开这张图，把当前对话顶掉。
            if (event.dataTransfer?.types?.includes("Files")) event.preventDefault();
          }}
          onDragLeave={() => {
            dragDepth.current = Math.max(0, dragDepth.current - 1);
            if (dragDepth.current === 0) setDragging(false);
          }}
          onDrop={(event) => {
            event.preventDefault();
            dragDepth.current = 0;
            setDragging(false);
            const files = filesFrom(event.dataTransfer?.files);
            if (files.length > 0) props.onAddImages(files);
          }}
        >
          <ReferenceChips draft={props.draft} onChange={props.onDraftChange} disabled={props.disabled} />

          <DraftImageChips images={props.images} onRemove={props.onRemoveImage} disabled={props.disabled} />

          {dragging && (
            <span className="pointer-events-none absolute inset-0 grid place-items-center rounded-xl bg-$1 font-mono text-[12px] text-brand">
              松手就加上这张图
            </span>
          )}

          {/* 上下文条：这一轮在哪个项目、哪个目录里干。
              Codex 桌面版同样是这条（项目 | Local | 分支），分支我们还没取，先不摆假数据。 */}
          <div className="flex items-center gap-1.5 px-4 pt-2.5 text-[11px] text-faint">
            <span className="max-w-[40%] truncate rounded bg-surface-2 px-1.5 py-0.5 text-muted">
              {props.projectName || "未命名项目"}
            </span>
            <span className="text-faint">·</span>
            <span className="min-w-0 truncate">{activeWorkspace?.label ?? "未绑定目录"}</span>
            {activeWorkspace?.branch && (
              <>
                <span className="text-faint">·</span>
                <span className="shrink-0 font-mono">{activeWorkspace.branch}</span>
              </>
            )}
          </div>

          <textarea
            ref={textarea}
            value={props.draft}
            onChange={(event) => {
              const next = event.target.value;
              // 打一个 @ 就直接开引用面板（前面必须是空白或句首）。
              // Claude Code / Cursor / Qoder 都是这个交互：引用文件不该先去找图标。
              // 邮箱里的 a@b 不会被误触发——@ 前一个字符不是空白就不管。
              if (next.length === props.draft.length + 1 && next.endsWith("@")) {
                const before = next.slice(0, -1);
                if (!before || /\s$/.test(before)) {
                  props.onDraftChange(before);
                  setPicking(true);
                  return;
                }
              }
              props.onDraftChange(next);
            }}
            onKeyDown={(event) => {
              // 斜杠面板优先接管 ↑↓ / Enter / Esc：这时回车是"执行命令"，不是"发送"。
              if (slash && !event.nativeEvent.isComposing) {
                if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                  event.preventDefault();
                  if (slashItems.length > 0) {
                    const step = event.key === "ArrowDown" ? 1 : -1;
                    setSlashIndex((slashCursor + step + slashItems.length) % slashItems.length);
                  }
                  return;
                }
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  const picked = slashItems[slashCursor];
                  if (picked) runCommand(picked);
                  return;
                }
                if (event.key === "Escape") {
                  event.preventDefault();
                  props.onDraftChange("");
                  return;
                }
              }
              if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault();
                if (!canSend) return;
                // 跑着的时候按回车是"插一句话"，不是"再开一轮"：同一轮里改方向，
                // 比等它跑完再纠正要省一次重复执行。
                if (props.running) props.onSteer();
                else props.onSend();
              }
            }}
            onPaste={(event) => {
              // 截图工具的常规用法就是 Ctrl+V。只处理剪贴板里真有图片文件的情况，
              // 复制一段带图的网页内容时也会命中这里，此时不该拦掉文本粘贴。
              const files = filesFrom(event.clipboardData?.files);
              if (files.length === 0) return;
              event.preventDefault();
              props.onAddImages(files);
            }}
            rows={3}
            placeholder={
              props.disabled
                ? "先选一个项目"
                : props.workspaceReady === false
                  ? "正在读取项目…"
                  : props.workspaces.length === 0
                    ? "还没有工作目录：点下面的「未绑定目录」选一个，它才知道在哪儿干活"
                    : props.running
                      ? "它正在做，可以插一句话调整方向…（Enter 发送）"
                      : `${placeholderText}（Enter 发送，Shift+Enter 换行）`
            }
            className="block max-h-[262px] min-h-[80px] w-full resize-none bg-transparent px-4 pt-3 pb-2 text-[14px] leading-[1.7] text-text placeholder:text-faint focus:outline-none"
          />

          {/* 工具条：左边是"在哪儿干活、按什么规矩"，右边是"用哪个模型、发不发"。
              以前全部挤在一行且允许收缩，窗口一窄文字类控件就被压成竖排（"不/记/住"）；
              现在整条允许换行，右边整组（开关/模型/发送）作为一块整体换到下一行靠右。 */}
          {pathEntry !== null && (
            <div className="mb-1 flex flex-wrap items-center gap-1.5 border-t border-line px-2.5 pt-2">
              <input
                autoFocus
                value={pathEntry}
                onChange={(event) => setPathEntry(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && pathEntry.trim()) {
                    props.onBindWorkspacePath(pathEntry.trim());
                    setPathEntry(null);
                  }
                  if (event.key === "Escape") setPathEntry(null);
                }}
                placeholder="本地目录的完整路径，例如 E:////projects////demo"
                aria-label="本地目录路径"
                className="min-w-[14rem] flex-1 rounded-md border border-line bg-surface px-2 py-1.5 font-mono text-[12px] text-text placeholder:text-faint focus:outline-none"
              />
              <button
                type="button"
                disabled={pathEntry.trim() === ""}
                onClick={() => {
                  props.onBindWorkspacePath(pathEntry.trim());
                  setPathEntry(null);
                }}
                className="rounded-md border border-brand/40 bg-brand/10 px-2 py-1 text-[12px] text-brand transition-colors hover:bg-brand/15 disabled:opacity-50"
              >
                绑定并切换
              </button>
              <button
                type="button"
                onClick={() => setPathEntry(null)}
                className="rounded-md border border-line px-2 py-1 text-[12px] text-muted transition-colors hover:text-text"
              >
                取消
              </button>
            </div>
          )}

          <div className="flex flex-wrap items-center gap-x-1 gap-y-1 px-2.5 pt-1 pb-2.5">
            {/* 附件只剩一个「+」。以前是 @ 和相框两个图标并排：相框是十年前
                图片上传的写法，现在的 agent 都把附件收进一个入口，输入框更干净。 */}
            <div ref={attachRef} className="relative shrink-0">
              <button
                type="button"
                onClick={() => setAttachOpen((value) => !value)}
                disabled={props.disabled}
                aria-haspopup="menu"
                aria-expanded={attachOpen}
                title="添加附件"
                className={`grid size-8 place-items-center rounded-lg transition-colors hover:bg-surface-2 hover:text-text disabled:opacity-40 ${
                  attachOpen ? "bg-surface-2 text-text" : "text-faint"
                }`}
              >
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" className="size-[16px]" aria-hidden>
                  <path d="M12 5v14M5 12h14" />
                </svg>
              </button>
              {attachOpen && (
                <div
                  role="menu"
                  className="absolute bottom-full left-0 z-30 mb-1 w-[212px] overflow-hidden rounded-lg border border-line bg-surface py-1 shadow-[var(--shadow-lg)]"
                >
                  <button
                    type="button"
                    role="menuitem"
                    onClick={() => {
                      setAttachOpen(false);
                      setPicking(true);
                    }}
                    className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-[13px] text-muted transition-colors hover:bg-surface-2 hover:text-text"
                  >
                    <span className="w-4 shrink-0 text-center font-mono text-[12px] text-faint">@</span>
                    引用工作区文件
                  </button>
                  <button
                    type="button"
                    role="menuitem"
                    onClick={() => {
                      setAttachOpen(false);
                      fileInput.current?.click();
                    }}
                    className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-[13px] text-muted transition-colors hover:bg-surface-2 hover:text-text"
                  >
                    <span className="grid w-4 shrink-0 place-items-center text-faint">
                      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round" className="size-[14px]" aria-hidden>
                        <path d="M20 11.5 12 19.5a5 5 0 0 1-7-7l8-8a3.5 3.5 0 0 1 5 5l-8 8a2 2 0 0 1-3-3l7-7" />
                      </svg>
                    </span>
                    上传图片或附件
                  </button>
                </div>
              )}
            </div>
            <input
              ref={fileInput}
              type="file"
              accept={ACCEPT_ATTR}
              multiple
              hidden
              onChange={(event) => {
                const files = filesFrom(event.target.files);
                if (files.length > 0) props.onAddImages(files);
                // 清空：不清的话选同一张图第二次不会触发 change。
                event.target.value = "";
              }}
            />

            <span className="hidden shrink-0 font-mono text-[11px] text-faint sm:inline">
              {referencesIn(props.draft).length > 0
                ? `已引用 ${referencesIn(props.draft).length} 个文件`
                : uploading
                  ? "附件上传中…"
                  : readyImages.length > 0
                    ? `${readyImages.length} 个附件`
                    : ""}
            </span>

            {/* 一条竖线把"操作按钮"和"上下文下拉"分开：之前它们长得一样、挨得也近。 */}
            <span className="mx-0.5 h-4 w-px shrink-0 bg-line" aria-hidden />

            {/* 工作目录：菜单里直接列目录（含别的项目），选它就是切项目。 */}
            {workspaceSelectable ? (
              <SelectPopover
                size="compact"
                ariaLabel="工作目录"
                value={activeWorkspace ? `ws:${activeWorkspace.id}` : ""}
                options={workspaceOptions}
                onChange={(next) => {
                  if (next === "__pick__") {
                    props.onPickWorkspace();
                    return;
                  }
                  if (next === "__path__") {
                    setPathEntry("");
                    return;
                  }
                  if (!next.startsWith("ws:")) return;
                  props.onActivateWorkspace(next.slice(3));
                }}
                renderValue={() => (
                  <span className="flex min-w-0 items-center gap-1">
                    <FolderIcon />
                    <span className="truncate">{activeWorkspace?.label ?? "未绑定目录"}</span>
                  </span>
                )}
                triggerClassName={`max-w-[176px] min-w-[88px] ${activeWorkspace ? "" : "text-danger"}`}
                menuMinWidth={280}
              />
            ) : activeWorkspace ? (
              // 不能选目录的环境（CLI 静态站）里，已绑定的目录要如实显示名字，
              // 不能因为"改不了"就冒充"没绑定"——那会把用户赶去项目页白跑一趟。
              <span
                title={activeWorkspace.path || "在哪个目录里干活（桌面版可切换）"}
                className="flex min-w-0 max-w-[176px] items-center gap-1 rounded-lg px-2 py-1.5 text-[13px] text-muted"
              >
                <FolderIcon />
                <span className="truncate">{activeWorkspace.label}</span>
              </span>
            ) : (
              <span
                title="这个项目还没绑定目录，去「项目」页绑一个"
                className="rounded-lg px-2 py-1.5 text-[13px] text-danger"
              >
                未绑定目录
              </span>
            )}

            {/* 场景选择器（§8.1 顶部唯一入口）：决定这轮在哪个场景跑。
                目录为空说明清单还没读到，隐藏而不是渲染一个空下拉。 */}
            {props.scenarios.length > 0 && (
              <SelectPopover
                size="compact"
                ariaLabel="场景"
                value={props.scenarioKey}
                options={props.scenarios.map((scenario) => ({
                  value: scenario.key,
                  label: scenario.label,
                  description: scenario.summary,
                  warning: scenario.allowWrite ? undefined : "只读：不修改文件、不执行命令",
                }))}
                onChange={props.onScenarioChange}
                triggerClassName="max-w-[96px] min-w-[68px]"
                menuMinWidth={220}
              />
            )}

            {/* 右边整组：模型 + 权限 + 偏好开关 + 发送。整组不会被拆开，窄了就整体换到下一行靠右；
                真窄到放不下时只让模型下拉变窄（它的名字本来就该截断），开关和发送保持原样，
                免得再出现文字被压成竖排。 */}
            <div className="ml-auto flex min-w-0 shrink items-center gap-1.5">
              <SelectPopover
                size="compact"
                ariaLabel="用哪个模型"
                value={props.providerId}
                options={
                  usableProviders.length === 0
                    ? [
                        {
                          value: "",
                          label: "还没有配置模型",
                          description: "去「设置 → 模型与供应商」填一个 API Key，这里就能选",
                          disabled: true,
                        },
                      ]
                    : [
                        { value: "", label: "默认模型", description: "按设置页的角色分工自动挑" },
                        ...usableProviders.map((provider) => ({
                          value: provider.id,
                          label: provider.model ? `${provider.name} · ${provider.model}` : provider.name,
                        })),
                        // 没配 Key 的不当作可选项，但列出来说明"为什么它不在上面"。
                        ...unconfigured.map((provider) => ({
                          value: provider.id,
                          label: provider.name,
                          description: "未配置 API Key——去设置里填了才可用",
                          disabled: true,
                        })),
                      ]
                }
                onChange={props.onProviderChange}
                disabled={props.providers.length === 0}
                renderValue={(option) => (
                  // 颜色写在里层：触发器自己带 text-muted，同优先级下谁在 CSS 后面
                  // 谁生效——挂在 triggerClassName 上不一定红得起来。
                  <span className={`truncate ${usableProviders.length === 0 ? "text-danger" : ""}`}>
                    {usableProviders.length === 0
                      ? "未配置模型"
                      : props.providerId
                        ? (option?.label ?? "")
                        : props.defaultModelLabel || "默认模型"}
                  </span>
                )}
                triggerClassName="max-w-[168px] min-w-[72px]"
                menuMinWidth={240}
              />

              {/* 权限档（v0.10.1 §4）：这轮任务「问不问」的总开关，随任务快照固定。 */}
              <SelectPopover
                size="compact"
                ariaLabel="权限模式"
                value={props.permissionMode}
                options={permissionOptions}
                onChange={props.onPermissionChange}
                triggerClassName={`max-w-[92px] min-w-[64px] ${
                  props.permissionMode === "bypass" ? "text-warn" : props.permissionMode === "plan" ? "text-brand" : ""
                }`}
                menuMinWidth={260}
              />
              {/* 记忆开关只在日常场景出现（§7.2）：开发场景连看都看不到。 */}
              {activeScenario?.memoryAllowed && (
                <button
                  type="button"
                  onClick={() => props.onMemoryOptInChange(!props.memoryOptIn)}
                  aria-pressed={props.memoryOptIn}
                  title={
                    props.memoryOptIn
                      ? "正在记住你的习惯，点一下改为不记住"
                      : "点一下允许 AI 记住你的偏好（敏感信息不会记）"
                  }
                  className={`shrink-0 rounded-full border px-2 py-1 text-[12px] whitespace-nowrap transition-colors ${
                    props.memoryOptIn
                      ? "border-brand/40 bg-brand/10 text-brand"
                      : "border-line text-faint hover:bg-surface-2"
                  }`}
                >
                  {props.memoryOptIn ? "记住习惯" : "不记住"}
                </button>
              )}

              {/* 只有一个按钮：闲着是发送，跑着是停止。
                  以前跑着的时候是"停止 + 插话"两个圆钮并排，停止还从对话区再出一个文字按钮，
                  三处都能停，用户得先分清哪个是哪个。插话仍然可用——按回车就是，提示写在占位文案里。 */}
              <button
                type="button"
                onClick={action === "stop" ? props.onStop : props.onSend}
                disabled={action === "disabled"}
                title={action === "stop" ? "停止这一轮（想插一句话直接按 Enter）" : "发送（Enter）"}
                aria-label={action === "stop" ? "停止这一轮" : "发送"}
                className="grid size-8 shrink-0 place-items-center rounded-full bg-brand text-on-brand transition-opacity hover:opacity-90 disabled:opacity-30"
              >
                {action === "stop" ? (
                  <span className="size-2.5 rounded-[2px] bg-current" />
                ) : (
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.1} strokeLinecap="round" strokeLinejoin="round" className="size-[18px]">
                    <path d="M12 19V5M6 11l6-6 6 6" />
                  </svg>
                )}
              </button>
            </div>
          </div>

          {slash && <SlashMenu items={slashItems} index={slashCursor} onPick={runCommand} />}

          {picking &&
            (props.projectId ? (
              <AtPicker
                projectId={props.projectId}
                onClose={() => setPicking(false)}
                onPick={(path) => {
                  props.onDraftChange(insertReference(props.draft, path));
                  setPicking(false);
                  textarea.current?.focus();
                }}
              />
            ) : (
              // 没绑工作区时以前是"点了什么都不发生"，用户只会以为按钮坏了。
              <div className="absolute bottom-full left-2 right-2 z-30 mb-1 flex items-start gap-2 rounded-xl border border-warn/30 bg-warn/[0.05] px-3 py-2 shadow-[var(--shadow-lg)]">
                <span className="mt-[1px] shrink-0 text-[12px] text-warn">@</span>
                <span className="min-w-0 flex-1 text-[12px] leading-relaxed text-muted">
                  还没有绑定工作区，引用不了文件。先在下面的「未绑定目录」里选一个目录。
                </span>
                <button
                  type="button"
                  onClick={() => setPicking(false)}
                  className="shrink-0 text-[12px] text-faint hover:text-text"
                >
                  知道了
                </button>
              </div>
            ))}
        </div>

        {/* 场景与权限的边界要写在用户看得见的地方（v164 §4.3 / v0.10.1 §4）。
            只在提示词里写"别改代码"等于没说：用户点开发送才发现做不了。 */}
        <p className="mt-2 text-center text-[12px] text-faint">
          {props.permissionMode === "plan"
            ? "计划模式：只读分析和出建议，不改文件、不执行命令。要动手请切回「询问」或「完全访问」。"
            : activeScenario && !activeScenario.allowWrite
              ? `「${activeScenario.label}」场景只读：不修改文件、不执行命令。要动手请切换到开发场景。`
              : activeScenario && activeScenario.writeExtensions.length > 0
                ? `「${activeScenario.label}」场景只能写 ${activeScenario.writeExtensions
                    .slice(0, 4)
                    .join(" / ")}${activeScenario.writeExtensions.length > 4 ? " 等文档" : ""}；改代码请切换到开发场景。`
                : props.permissionMode === "bypass"
                  ? "完全访问：工作区内自动读写和测试；删除、工作区外路径、发布仍会先确认。"
                  : "它会读你的代码、改文件、跑命令；高风险操作会先问你。"}
        </p>
      </div>
    </div>
  );
}

/** 目录图标：工作区选择器上唯一需要说明「这是一块磁盘上的位置」的线索。 */
function FolderIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.7}
      strokeLinejoin="round"
      className="size-[13px] shrink-0 opacity-70"
      aria-hidden
    >
      <path d="M3 7.5A1.5 1.5 0 0 1 4.5 6H9l1.5 2h9A1.5 1.5 0 0 1 21 9.5v8A1.5 1.5 0 0 1 19.5 19h-15A1.5 1.5 0 0 1 3 17.5v-10Z" />
    </svg>
  );
}

/**
 * 是不是一个真实路径。
 *
 * 库里有些工作区存的是占位串（`<workspace>name`），把它当"路径说明"展示，
 * 用户会以为目录坏了；这种值退回成一句人话更稳。
 */
function looksLikePath(value: string | undefined): boolean {
  if (!value) return false;
  return /^([a-zA-Z]:[\\/]|\/|\\\\)/.test(value);
}
