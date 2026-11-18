"use client";

import { TURN_TONE_LABEL, turnTone } from "./nav";

/**
 * 会话行的状态点——全站唯一实现。
 *
 * 以前首页侧栏和全站左栏各写了一份：首页"完成"是绿点，左栏"完成"却是灰点
 * （`bg-line-strong`），同一个状态在两处颜色不同，用户会以为数据不一样。
 * 三色口径只在这里定义：进行中=品牌色（脉冲）、失败/取消=红、完成=绿、没开始=不画。
 *
 * 用 title/aria-label 把颜色翻成字：只给颜色的话，色觉差异的用户读不出"跑失败了"。
 */
export function StatusDot({ status }: { status: string }) {
  const tone = turnTone(status);
  if (tone === "idle") return null;
  const toneClass =
    tone === "running" ? "bg-brand animate-pulse" : tone === "failed" ? "bg-danger" : "bg-accent";
  return (
    <span
      title={TURN_TONE_LABEL[tone]}
      aria-label={TURN_TONE_LABEL[tone]}
      className={`size-1.5 shrink-0 rounded-full ${toneClass}`}
    />
  );
}
