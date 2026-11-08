"use strict";

/**
 * 桌面通知的「投递策略」——壳自己才答得出来的三个问题。
 *
 * 判定「该不该打扰」**不在这里**：那是后端的 ``agent/notify.py``，结论连同理由
 * 落在 ``notify.decided`` 事件里，取数口 ``/api/v1/notifications`` 只吐
 * ``disturb=true`` 的那几条。壳拿到的是已经判过的结果，这里不再复判一遍——
 * 两套规则各判一次，迟早会漂成「设置里说静默、屏幕却在响」。
 *
 * 这个文件只管壳知道答案的三件事：
 *
 *   1. 这条我是不是已经弹过了（跨轮询去重，游标是秒精度，同批会被读到两次）
 *   2. 用户此刻是不是正看着这个窗口（看着就别打断，界面上本来就有提示）
 *   3. 一分钟内已经弹过几条（防刷屏；审批例外，压着就等于让任务停死）
 *
 * 禁止 ``require("electron")``：main.cjs 一旦 require 就跑不进普通 node 测试，
 * 判定规则就没法钉住（和 shell-policy.cjs、gpu-guard.cjs 同一个理由）。
 */

// 去重集合的上限。通知是"最近发生的事"，只留最近这么多条就够；再多也认不出来。
const MAX_SEEN = 200;

// 防刷屏：一分钟内同一批最多弹几条。超过就压着，等下一分钟。
const BURST_WINDOW_MS = 60_000;
const BURST_LIMIT = 3;

// 正文长度上限。Windows 的 toast 大概两行就截断了，自己截比让系统截好看。
const BODY_LIMIT = 120;

/**
 * 一条通知该长什么样。
 *
 * 标题按「是谁起的 + 什么结果」分档，因为同一句"任务完成"扣在定时提醒上就不
 * 像提醒了：用户设了"九点提醒我检查部署"，到点该看到的是"到点了"，不是
 * "有一轮跑完了"。这依赖后端在 ``notify.decided`` 里记的 ``origin``。
 */
function noticeText(item) {
  const raw = String((item && (item.goal || item.title)) || "").trim();
  const body = raw.length > BODY_LIMIT ? `${raw.slice(0, BODY_LIMIT - 1)}…` : raw;
  const kind = String((item && item.kind) || "");
  const origin = String((item && item.origin) || "");

  if (kind === "approval") {
    return { title: "等你批准", body: body || "有个操作要你点一下" };
  }
  if (kind === "failure") {
    return { title: "没跑成", body: body || "有一轮失败了，去看看吧" };
  }
  if (origin === "schedule") {
    return { title: "到点了", body: body || "你设的定时到了" };
  }
  if (origin === "watch") {
    return { title: "有变化了", body: body || "盯着的那件事有动静" };
  }
  return { title: "做完了", body: body || "有一轮跑完了" };
}

/** 把一条 id 记进"弹过"的集合，留最近 MAX_SEEN 条。返回新数组，不改入参。 */
function rememberSeen(seenIds, id) {
  const key = String(id || "");
  if (!key) return Array.isArray(seenIds) ? [...seenIds] : [];
  const kept = (Array.isArray(seenIds) ? seenIds : []).filter((item) => item !== key);
  kept.push(key);
  return kept.length > MAX_SEEN ? kept.slice(kept.length - MAX_SEEN) : kept;
}

/** 是否已经弹过。集合里没有这条就是新的。 */
function alreadyShown(seenIds, id) {
  const key = String(id || "");
  if (!key) return false;
  return (Array.isArray(seenIds) ? seenIds : []).includes(key);
}

/**
 * 这一刻弹不弹，以及为什么。``reason`` 是给人看的，日志与证据页都要用。
 *
 * ``settle`` 回答的是**记账问题**，和 ``show`` 不是一回事：调用方要不要把这条
 * 记进"已处理"，取决于它压下去的原因。
 *
 *   - 已经弹过 / 用户正看着 → settle=true。前者无需再谈；后者用户已经在界面上
 *     看见状态变了，切走之后再补弹一次属于重复告知。
 *   - 防刷屏压着 → settle=**false**。这一条只是"现在不说"，不是"不说了"，
 *     下一轮还得有机会弹出来。记成已处理等于永久丢掉它。
 *
 * ``shownAt`` 是最近弹过的时间戳（毫秒）数组，由调用方维护——策略层不持有状态，
 * 这样同一份输入永远得到同一个答案，测试才钉得住。
 */
function decideNotification({
  item,
  seenIds,
  windowFocused = false,
  windowVisible = false,
  shownAt = [],
  nowMs = 0,
} = {}) {
  if (!item || !String(item.id || "")) {
    return { show: false, settle: true, reason: "没有可弹的内容" };
  }
  if (alreadyShown(seenIds, item.id)) {
    return { show: false, settle: true, reason: "这条已经弹过" };
  }
  if (windowFocused && windowVisible) {
    // 用户就看着这个窗口：界面上任务卡自己会变，再弹一次是重复告知。
    return { show: false, settle: true, reason: "用户正看着这个窗口，不打断" };
  }

  const kind = String(item.kind || "");
  const approval = kind === "approval";
  if (!approval) {
    const recent = (Array.isArray(shownAt) ? shownAt : []).filter(
      (at) => Number.isFinite(at) && nowMs - at < BURST_WINDOW_MS
    );
    if (recent.length >= BURST_LIMIT) {
      // 审批不压：任务停在等批准，压到下一分钟等于让它白等。
      return {
        show: false,
        settle: false,
        reason: `一分钟内已经弹过 ${recent.length} 条，先压着`,
      };
    }
  }

  return { show: true, settle: true, reason: approval ? "审批等待，必弹" : "有新的结果要看" };
}

module.exports = {
  BODY_LIMIT,
  BURST_LIMIT,
  BURST_WINDOW_MS,
  MAX_SEEN,
  alreadyShown,
  decideNotification,
  noticeText,
  rememberSeen,
};
