"use client";

import { useEffect, useRef, useState } from "react";
import type { Project, Provider } from "../tasks/api";
import { AtPicker } from "./at-picker";
import { insertReference } from "./picker";
import { composerAction } from "./turns";
import { ReferenceChips } from "./reference-chips";
import { referencesIn } from "./references";

export type ComposerProps = {
  projects: Project[];
  providers: Provider[];
  projectId: string;
  onProjectChange: (id: string) => void;
  providerId: string;
  onProviderChange: (id: string) => void;
  draft: string;
  onDraftChange: (value: string) => void;
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
  const [picking, setPicking] = useState(false);

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

  const canSend = props.draft.trim().length > 0 && !props.disabled;
  const action = composerAction(props.running, canSend);

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
        <div className="relative rounded-2xl border border-line-strong bg-surface shadow-[0_2px_6px_rgba(16,24,40,0.04),0_12px_28px_-16px_rgba(16,24,40,0.18)] transition-colors focus-within:border-brand/50 focus-within:shadow-[0_2px_8px_rgba(16,24,40,0.06),0_14px_32px_-16px_rgba(14,116,144,0.28)]">
          <ReferenceChips draft={props.draft} onChange={props.onDraftChange} disabled={props.disabled} />

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
            rows={3}
            placeholder={
              props.disabled
                ? "先选一个工作区"
                : props.running
                  ? "它正在做，可以插一句话调整方向…（Enter 发送）"
                  : "说点什么，或者直接让它去改代码…（Enter 发送，Shift+Enter 换行）"
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

            <span className="hidden font-mono text-[10.5px] text-faint sm:inline">
              {referencesIn(props.draft).length > 0 ? `已引用 ${referencesIn(props.draft).length} 个文件` : ""}
            </span>

            {props.projects.length > 1 && (
              <select
                value={props.projectId}
                onChange={(event) => props.onProjectChange(event.target.value)}
                title="工作区"
                className="max-w-[170px] truncate rounded-lg bg-transparent px-2 py-1.5 text-[12.5px] text-muted transition-colors hover:bg-surface-2 focus:outline-none"
              >
                {props.projects.map((project) => (
                  <option key={project.id} value={project.id}>
                    {project.name}
                  </option>
                ))}
              </select>
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
