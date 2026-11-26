"use client";

/**
 * 新对话首屏：只留「我们要做什么」。
 *
 * 场景切换与授权都在输入框工具条上，首屏不再重复一遍；用户选了目录之后，
 * 标题直接写「我们要在〈目录〉做什么」，让当前上下文一眼可见。
 */
export type FirstScreenProps = {
  workspaceLabel: string;
  workspaceReady: boolean;
};

export function FirstScreen(props: FirstScreenProps) {
  const workspace = props.workspaceReady ? props.workspaceLabel.trim() : "";
  // 目录名不加括号：标题就是一句话，多一层符号会让它像引用/占位。
  // 两边留空格，避免拉丁目录名和中文粘成一串。
  const title = workspace ? `我们要在 ${workspace} 做什么？` : "我们要做什么？";

  return (
    <div className="mx-auto flex min-h-[42vh] w-full max-w-3xl flex-col items-center justify-center px-4 py-16 text-center">
      <span className="grid size-16 place-items-center rounded-2xl bg-logo-bg">
        <span className="size-5 rounded-[4px] bg-logo-accent" />
      </span>
      <h2 className="mt-6 max-w-2xl text-balance text-[28px] leading-tight font-medium tracking-tight sm:text-[30px]">
        {title}
      </h2>
    </div>
  );
}
