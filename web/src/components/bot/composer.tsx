"use client";

import { useEffect, useRef, useState } from "react";
import type { Project, Provider, Workspace } from "../tasks/api";
import { AtPicker } from "./at-picker";
import { DraftImageChips } from "./image-chips";
import { insertReference } from "./picker";
import { composerAction } from "./turns";
import { ReferenceChips } from "./reference-chips";
import { referencesIn } from "./references";
import { ACCEPT_ATTR, imageFilesFrom, type DraftImage } from "./uploads";

export type ComposerProps = {
  projects: Project[];
  providers: Provider[];
  projectId: string;
  onProjectChange: (id: string) => void;
  /** 当前项目已绑定的目录。空数组表示还没绑——这时候一眼就能看出来"没地方干活"。 */
  workspaces: Workspace[];
  /** 切到另一个已绑定的目录。 */
  onActivateWorkspace: (id: string) => void;
  /** 弹目录选择框并绑定。桌面壳里才有系统对话框，浏览器里由上层决定怎么降级。 */
  onPickWorkspace: () => void;
  /** 是否具备"挑一个新目录"的能力（桌面壳）。没有就不渲染那个选项。 */
  canPickWorkspace: boolean;
  providerId: string;
  onProviderChange: (id: string) => void;
  draft: string;
  onDraftChange: (value: string) => void;
  /** 草稿里的图片。上传完成的才有 id，上传中的不能发送。 */
  images: DraftImage[];
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

  const readyImages = props.images.filter((image) => image.id && !image.uploading && !image.error);
  const uploading = props.images.some((image) => image.uploading);
  // 只有图没有字也能发："照这张图改"里的"这张图"本身就是全部意思。
  // 上传中不能发：这时候发出去会是一条只带文字的轮次，图被悄悄丢掉。
  const canSend =
    (props.draft.trim().length > 0 || readyImages.length > 0) && !props.disabled && !uploading;
  const action = composerAction(props.running, canSend);

  // 后端在没有 is_active 时会回落到第一个，这里保持一致：显示的那个必须就是真正会用的那个。
  const activeWorkspace = props.workspaces.find((item) => item.is_active) ?? props.workspaces[0] ?? null;
  /** 没有可挑的目录、也没有已绑目录时，渲染一句话而不是一个点不动的下拉。 */
  const workspaceSelectable = props.canPickWorkspace || props.workspaces.length > 1;

  return (
    <div className="bg-bg/95 px-4 pt-3 pb-3.5 backdrop-blur">
      <div className="mx-auto w-full max-w-3xl">
        {props.error && (
          <p className="mb-2 rounded-lg border border-danger/25 bg-danger/[0.04] px-2.5 py-1.5 text-[11.5px] text-danger">
            {props.error}
          </p>
        )}
        {props.hint && (
          <p className="mb-2 rounded-lg border border-line bg-surface-2/60 px-2.5 py-1.5 text-[11.5px] text-muted">
            {props.hint}
          </p>
        )}

        {/* 没有分隔线，靠边框 + 一点投影把卡片从白底上托起来：页面和白卡同色，
            只留一条线的话线一去掉盒子就"化"进背景里了。 */}
        <div
          className={`relative rounded-2xl border bg-surface shadow-[0_2px_6px_rgba(16,24,40,0.04),0_12px_28px_-16px_rgba(16,24,40,0.18)] transition-colors focus-within:border-brand/50 focus-within:shadow-[0_2px_8px_rgba(16,24,40,0.06),0_14px_32px_-16px_rgba(14,116,144,0.28)] ${
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
            const files = imageFilesFrom(event.dataTransfer?.files);
            if (files.length > 0) props.onAddImages(files);
          }}
        >
          <ReferenceChips draft={props.draft} onChange={props.onDraftChange} disabled={props.disabled} />

          <DraftImageChips images={props.images} onRemove={props.onRemoveImage} disabled={props.disabled} />

          {dragging && (
            <span className="pointer-events-none absolute inset-0 grid place-items-center rounded-2xl bg-bg/80 font-mono text-[11px] text-brand">
              松手就加上这张图
            </span>
          )}

          <textarea
            ref={textarea}
            value={props.draft}
            onChange={(event) => props.onDraftChange(event.target.value)}
            onKeyDown={(event) => {
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
              const files = imageFilesFrom(event.clipboardData?.files);
              if (files.length === 0) return;
              event.preventDefault();
              props.onAddImages(files);
            }}
            rows={3}
            placeholder={
              props.disabled
                ? "先选一个项目"
                : props.running
                  ? "它正在做，可以插一句话调整方向…（Enter 发送）"
                  : "说点什么，或者直接贴张图/丢个文件进来…（Enter 发送，Shift+Enter 换行）"
            }
            className="block max-h-[262px] min-h-[88px] w-full resize-none bg-transparent px-4 pt-3.5 pb-2 text-[15px] leading-[1.75] text-text placeholder:text-faint focus:outline-none"
          />

          <div className="flex items-center gap-1.5 px-2.5 pt-1 pb-2.5">
            <button
              type="button"
              onClick={() => setPicking((value) => !value)}
              title="引用工作区里的文件"
              className="grid size-8 place-items-center rounded-lg text-faint transition-colors hover:bg-surface-2 hover:text-text"
            >
              <span className="font-mono text-[14px]">@</span>
            </button>

            <button
              type="button"
              onClick={() => fileInput.current?.click()}
              disabled={props.disabled}
              title="添加图片（也可以直接拖进来或 Ctrl+V 粘贴）"
              className="grid size-8 place-items-center rounded-lg text-faint transition-colors hover:bg-surface-2 hover:text-text disabled:opacity-40"
            >
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round" className="size-[15px]" aria-hidden>
                <rect x="3" y="4" width="18" height="16" rx="2.5" />
                <circle cx="8.5" cy="9.5" r="1.5" />
                <path d="M4 17l4.5-4.5a2 2 0 0 1 2.8 0L20 21" />
              </svg>
            </button>
            <input
              ref={fileInput}
              type="file"
              accept={ACCEPT_ATTR}
              multiple
              hidden
              onChange={(event) => {
                const files = imageFilesFrom(event.target.files);
                if (files.length > 0) props.onAddImages(files);
                // 清空：不清的话选同一张图第二次不会触发 change。
                event.target.value = "";
              }}
            />

            <span className="hidden font-mono text-[10.5px] text-faint sm:inline">
              {referencesIn(props.draft).length > 0
                ? `已引用 ${referencesIn(props.draft).length} 个文件`
                : uploading
                  ? "图片上传中…"
                  : readyImages.length > 0
                    ? `${readyImages.length} 张图`
                    : ""}
            </span>

            {props.projects.length > 1 && (
              <select
                value={props.projectId}
                onChange={(event) => props.onProjectChange(event.target.value)}
                title="项目"
                className="max-w-[170px] truncate rounded-lg bg-transparent px-2 py-1.5 text-[12.5px] text-muted transition-colors hover:bg-surface-2 focus:outline-none"
              >
                {props.projects.map((project) => (
                  <option key={project.id} value={project.id}>
                    {project.name}
                  </option>
                ))}
              </select>
            )}

            {/* 工作区：真正决定"在哪个目录里改东西"的就是它，之前只在项目页能改，
                对话页连看都看不到，于是整个功能像是不存在。 */}
            {workspaceSelectable ? (
              <select
                value={activeWorkspace?.id ?? ""}
                onChange={(event) => {
                  const next = event.target.value;
                  if (next === "__pick__") {
                    props.onPickWorkspace();
                    return;
                  }
                  if (next) props.onActivateWorkspace(next);
                }}
                title={activeWorkspace ? "在哪个目录里干活" : "这个项目还没绑定目录"}
                className={`max-w-[180px] truncate rounded-lg bg-transparent px-2 py-1.5 text-[12.5px] transition-colors hover:bg-surface-2 focus:outline-none ${
                  activeWorkspace ? "text-muted" : "text-danger"
                }`}
              >
                {!activeWorkspace && <option value="">未绑定目录</option>}
                {props.workspaces.map((workspace) => (
                  <option key={workspace.id} value={workspace.id}>
                    {workspace.label}
                  </option>
                ))}
                {props.canPickWorkspace && (
                  <option value="__pick__">
                    {props.workspaces.length > 0 ? "选另一个目录…" : "选一个目录…"}
                  </option>
                )}
              </select>
            ) : (
              <span
                title="这个项目还没绑定目录，去「项目」页绑一个"
                className="rounded-lg px-2 py-1.5 text-[12.5px] text-danger"
              >
                未绑定目录
              </span>
            )}

            <select
              value={props.providerId}
              onChange={(event) => props.onProviderChange(event.target.value)}
              title="用哪个模型"
              className="max-w-[220px] truncate rounded-lg bg-transparent px-2 py-1.5 text-[12.5px] text-muted transition-colors hover:bg-surface-2 focus:outline-none"
            >
              <option value="">默认模型</option>
              {props.providers.map((provider) => (
                <option key={provider.id} value={provider.id}>
                  {provider.name}
                  {provider.model ? ` · ${provider.model}` : ""}
                </option>
              ))}
            </select>

            {/* 只有一个按钮：闲着是发送，跑着是停止。
                以前跑着的时候是"停止 + 插话"两个圆钮并排，停止还从对话区再出一个文字按钮，
                三处都能停，用户得先分清哪个是哪个。插话仍然可用——按回车就是，提示写在占位文案里。 */}
            <div className="ml-auto flex items-center gap-2">
              <button
                type="button"
                onClick={action === "stop" ? props.onStop : props.onSend}
                disabled={action === "disabled"}
                title={action === "stop" ? "停止这一轮（想插一句话直接按 Enter）" : "发送（Enter）"}
                aria-label={action === "stop" ? "停止这一轮" : "发送"}
                className="grid size-9 place-items-center rounded-full bg-text text-bg transition-opacity hover:opacity-90 disabled:opacity-25"
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

          {picking && props.projectId && (
            <AtPicker
              projectId={props.projectId}
              onClose={() => setPicking(false)}
              onPick={(path) => {
                props.onDraftChange(insertReference(props.draft, path));
                setPicking(false);
                textarea.current?.focus();
              }}
            />
          )}
        </div>

        <p className="mt-2 text-center text-[11px] text-faint">
          它会读你的代码、改文件、跑命令；高风险操作会先问你。
        </p>
      </div>
    </div>
  );
}
