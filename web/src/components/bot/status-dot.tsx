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
export function StatusDot({ status, quietDone = false }: { status: string; quietDone?: boolean }) {
  const tone = turnTone(status);
  if (tone === "idle") return null;
  // 长列表里「已完成」是常态：18 行会话 18 个绿点，排成一列等于没有信息，
  // 还把标题往右推。导航列表传 quietDone，只在跑着 / 失败时占那个位置；
  // 单条详情之类需要"这一轮到底成功没有"的地方保持默认，照画。
  if (quietDone && tone === "done") return null;
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
