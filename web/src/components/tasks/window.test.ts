import assert from "node:assert/strict";
import { test } from "node:test";

import type { AgentEvent } from "./api.ts";
import {
  compactionChainLabel,
  compactionReplacementLabel,
  compactionWaterLabel,
  compactionWindowLabel,
  historyRefLabel,
} from "./status.ts";
import type { HistoryRef } from "./status.ts";
import { buildBlocks } from "./timeline.ts";

/**
 * #42 的前端一侧：压缩卡片要能回答「现在是第几段历史」「这次替换换掉了哪些内容」。
 * #44 补上「这一条是哪一轮写的」「这条内容从第几窗起留下」。
 *
 * 每一条判定都能失败：
 * 1. 窗口链原样搬出来，且**没带这些字段的老事件不许读成「第 0 窗」**——
 *    把 numOf 的 null 兜底成 0 就红，界面上会把"没说"演成"出生窗"。
 * 2. 引用只有位置、字数、指纹与出身/留存身份：后端在引用里塞任何正文字段都不许跟着进投影
 *    （抄正文那一手不借）。把 refsOf 换成整条 item 原样搬运就红。
 * 3. 丢弃/新进/替换后的历史三组数各自独立，丢弃条数取自数组而不是从 folded 倒推——
 *    用 folded 当 dropped 就红（两者在"水位推进但没折新内容"那种事件里根本不等）。
 * 4. 主机路径与正文标记不得出现在投影结果里（掩码是后端的事，前端不许自己拼路径）。
 * 5. 引用数组里混进非对象条目要过滤掉：不许渲染出「#0 · 未知」这种凭空长出来的行。
 * 6. 水位比率可以大于 1（真链路实测：窗口 1600、压缩前 9264 tokens）。这时讲"超出几倍"，
 *    不讲「水位 579%」那种看着像 bug 的数——把倍数分支删掉就红。
 * 7. 出身轮与跨窗留存（#44）：没记的读成"没说"（run 空串、其余 null），整段不印；
 *    印成「出自 第 0 轮」「第 1 次提问」「从第 0 窗留下」就是编造——把 null 兜底成 0 就红。
 */

const MARKER = "压缩也不许外传的正文标记 OMEGA-4477";
const HOST_PATH = "E:\\projects\\kbgai\\kbgai";
const ATTEMPT_RUN = "task:7d0e3f2c-1111-4222-8888-abcdef000011:attempt:2";

/** 直接调文案函数时用的"最小一条"：四个新字段全部是"没说"的形状。 */
const bareRef = (over: Partial<HistoryRef> = {}): HistoryRef => ({
  index: 0,
  role: "",
  kind: "",
  chars: 0,
  sha: "",
  run: "",
  userOrder: null,
  retainedSince: null,
  retainedWindows: null,
  ...over,
});

const refPayload = (over: Record<string, unknown> = {}) => ({
  index: 3,
  role: "user",
  kind: "user",
  chars: 128,
  sha: "eecb63b20ef5160b397449dc315a6775660fd0e3",
  ...over,
});

const compacted = (payload: Record<string, unknown>, seq = 21): AgentEvent => ({
  seq,
  node: "execute_action",
  type: "context.compacted",
  payload,
  createdAt: "2026-10-22T00:00:21+00:00",
});

function boundaryOf(payload: Record<string, unknown>) {
  const block = buildBlocks([compacted(payload)]).find((item) => item.kind === "boundary");
  assert.ok(block, "压缩事件必须成为一张卡");
  if (block.kind !== "boundary") throw new Error("unexpected block");
  return block;
}

const fullPayload = (): Record<string, unknown> => ({
  folded: 12,
  covered: 40,
  active_context_tokens_before: 9000,
  active_context_tokens_after: 4200,
  tokens_saved: 4800,
  water_ratio: 0.83,
  context_window: 128000,
  triggered_by: "tokens:fold",
  window_number: 2,
  window_id: "6f1c2d3e-4b5a-4c6d-8e7f-1a2b3c4d5e6f",
  first_window_id: "b7a1c0d2-0000-4000-8000-998877665544",
  previous_window_id: "b7a1c0d2-0000-4000-8000-998877665544",
  compaction_response_id: "resp_080281808ab14ccf016ad0968ec7d48199a80fc5be24a0d819",
  replacement: {
    before_items: 30,
    after_items: 14,
    retained: 11,
    added: [refPayload({ index: 0, role: "system", kind: "summary", chars: 512, sha: "a".repeat(40) })],
    dropped: [
      refPayload({ index: 1, kind: "tool_result", chars: 4096, sha: "b".repeat(40) }),
      refPayload({ index: 2, role: "assistant", kind: "tool_call", chars: 90, sha: "c".repeat(40) }),
    ],
    items: [
      refPayload({
        index: 0,
        role: "system",
        kind: "summary",
        chars: 512,
        sha: "a".repeat(40),
        run: ATTEMPT_RUN,
        retained_since: 1,
        retained_windows: 2,
      }),
      refPayload({
        index: 5,
        role: "user",
        kind: "user",
        chars: 128,
        run: "545f3993-ec23-4b22-8f45-ce3c301d193a",
        user_order: 0,
      }),
    ],
  },
});

test("压缩卡把窗口链与替换记录原样搬出来", () => {
  const block = boundaryOf(fullPayload());
  assert.equal(block.windowChain.windowNumber, 2);
  assert.equal(block.windowChain.windowId, "6f1c2d3e-4b5a-4c6d-8e7f-1a2b3c4d5e6f");
  assert.equal(block.windowChain.previousWindowId, "b7a1c0d2-0000-4000-8000-998877665544");
  assert.equal(block.windowChain.firstWindowId, "b7a1c0d2-0000-4000-8000-998877665544");
  assert.equal(block.windowChain.responseId, "resp_080281808ab14ccf016ad0968ec7d48199a80fc5be24a0d819");
  assert.equal(block.replacement.beforeItems, 30);
  assert.equal(block.replacement.afterItems, 14);
  assert.equal(block.replacement.retained, 11);
  assert.equal(block.replacement.dropped.length, 2);
  assert.equal(block.replacement.added.length, 1);
  assert.equal(block.replacement.items.length, 2);
  // 出身与留存身份原样搬出来（#44）：轮与窗第一次共享同一把键。
  assert.deepEqual(
    block.replacement.items.map((item) => [item.run, item.userOrder, item.retainedSince, item.retainedWindows]),
    [
      [ATTEMPT_RUN, null, 1, 2],
      ["545f3993-ec23-4b22-8f45-ce3c301d193a", 0, null, null],
    ]
  );
  // 旧的四个读数不能被新字段顶掉：它们是这张卡最早立起来的那四个事实。
  assert.equal(block.folded, 12);
  assert.equal(block.window, 128000);
});

test("引用只带位置、字数与指纹，后端多塞的正文一个字都不进投影", () => {
  const payload = fullPayload();
  const replacement = payload.replacement as Record<string, unknown>;
  replacement.dropped = [
    refPayload({ content: MARKER, text: MARKER, tool_calls: [{ id: "call_1" }] }),
    "不是对象的一条",
    null,
  ];
  const block = boundaryOf(payload);
  assert.equal(block.replacement.dropped.length, 1, "非对象条目必须过滤掉，不许渲染成凭空的一行");
  const item = block.replacement.dropped[0];
  assert.deepEqual(
    Object.keys(item).slice().sort(),
    ["chars", "index", "kind", "retainedSince", "retainedWindows", "role", "run", "sha", "userOrder"],
    "引用的键就是这九个，多一个都是搬运"
  );
  const dumped = JSON.stringify(block);
  assert.ok(!dumped.includes(MARKER), `投影里不许出现正文标记：${dumped.slice(0, 200)}`);
});

test("投影结果里不许出现主机路径", () => {
  const payload = fullPayload();
  (payload.replacement as Record<string, unknown>).items = [
    refPayload({ role: "user", kind: "user", chars: HOST_PATH.length, sha: "d".repeat(40) }),
  ];
  const dumped = JSON.stringify(boundaryOf(payload));
  assert.ok(!dumped.includes(HOST_PATH), `主机路径混进了投影：${dumped.slice(0, 200)}`);
  assert.ok(!dumped.includes("C:\\Users"), "投影里不许出现用户目录形态的路径");
});

test("老压缩事件不许把缺失的窗口号读成第 0 窗", () => {
  const block = boundaryOf({ folded: 3, covered: 18, tokens_saved: 900, triggered_by: "tokens" });
  assert.equal(block.windowChain.windowNumber, null);
  assert.equal(block.windowChain.windowId, "");
  assert.equal(block.windowChain.previousWindowId, "");
  assert.equal(block.windowChain.firstWindowId, "");
  assert.equal(block.windowChain.responseId, "");
  assert.equal(block.replacement.beforeItems, null);
  assert.equal(block.replacement.afterItems, null);
  assert.equal(block.replacement.retained, null);
  assert.deepEqual(block.replacement.dropped, []);
  assert.deepEqual(block.replacement.items, []);
  assert.equal(compactionWindowLabel(block.windowChain), "");
  assert.equal(compactionChainLabel(block.windowChain), "");
  assert.equal(compactionReplacementLabel(block.replacement), "");
  // 但卡片本身照旧立起来：老任务的边界事实仍然有效。
  assert.equal(block.folded, 3);
});

test("第 0 窗是真的出生窗，与「没说」是两回事", () => {
  const block = boundaryOf({ ...fullPayload(), window_number: 0, replacement: undefined });
  assert.equal(block.windowChain.windowNumber, 0);
  assert.equal(compactionWindowLabel(block.windowChain), "第 0 窗");
  assert.equal(block.replacement.retained, null, "replacement 整个缺失时 retained 必须是 null，不是 0");
  assert.equal(compactionReplacementLabel(block.replacement), "");
});

test("窗口链与替换的人话读法", () => {
  const block = boundaryOf(fullPayload());
  assert.equal(compactionChainLabel(block.windowChain), "第 2 窗 · 接续 b7a1c0d2");
  assert.equal(compactionReplacementLabel(block.replacement), "保留 11 条 · 丢弃 2 条 · 新进 1 条");
  assert.equal(historyRefLabel(block.replacement.dropped[0]), "#1 · tool_result · 4096 字 · bbbbbbbb");
  assert.equal(historyRefLabel(bareRef()), "#0 · 未知 · 0 字 · 无指纹");
});

test("出身轮与跨窗留存念得出来，缺的键整段不印", () => {
  const block = boundaryOf(fullPayload());
  const [summary, question] = block.replacement.items;
  // attempt 键念"主线程#2"（与 #43 同一口径），砍前八位等于两次运行分不开
  assert.equal(
    historyRefLabel(summary),
    "#0 · summary · 512 字 · aaaaaaaa · 出自 主线程#2 · 自第 1 窗留下 · 已跨 2 窗"
  );
  assert.equal(
    historyRefLabel(question),
    "#5 · user · 128 字 · eecb63b2 · 出自 545f3993 · 第 1 次提问"
  );
  // 没说≠说了"没有"：run 空串、序号与窗数缺失时，一句都不许多印
  assert.equal(historyRefLabel(bareRef({ run: "", userOrder: 0 })), "#0 · 未知 · 0 字 · 无指纹 · 第 1 次提问");
  assert.equal(
    historyRefLabel(bareRef({ run: ATTEMPT_RUN, userOrder: null, retainedSince: null, retainedWindows: null })),
    "#0 · 未知 · 0 字 · 无指纹 · 出自 主线程#2"
  );
  // 只跨了一窗（第一次出现）不讲"已跨 1 窗"：那是每个新条目的默认值，讲了等于没讲
  assert.equal(
    historyRefLabel(bareRef({ kind: "tool_result", retainedSince: 3, retainedWindows: 1 })),
    "#0 · tool_result · 0 字 · 无指纹"
  );
});

test("接续关系缺失时只报窗号，不硬凑一句「接续」", () => {
  const block = boundaryOf({ ...fullPayload(), previous_window_id: "" });
  assert.equal(compactionChainLabel(block.windowChain), "第 2 窗");
});

test("丢弃条数取自引用数组，不从 folded 倒推", () => {
  // 「水位推进但这次没折新内容」的事件 folded=0 却确实可能丢过内容；
  // 反过来 folded 大也不等于丢得多。用 folded 当 dropped 会在两种情形都说错话。
  const block = boundaryOf({
    folded: 0,
    covered: 55,
    triggered_by: "chars:micro",
    window_number: 1,
    window_id: "w1",
    first_window_id: "w1",
    previous_window_id: "w1",
    replacement: {
      before_items: 55,
      after_items: 55,
      retained: 55,
      added: [],
      dropped: [refPayload({ index: 7, kind: "tool_result", chars: 2048, sha: "e".repeat(40) })],
      items: [refPayload({ index: 0, role: "system", kind: "summary", chars: 512, sha: "f".repeat(40) })],
    },
  });
  assert.equal(compactionReplacementLabel(block.replacement), "保留 55 条 · 丢弃 1 条");
  assert.equal(block.replacement.added.length, 0);
  assert.equal(compactionWindowLabel(block.windowChain), "第 1 窗");
});

test("压缩前已经塞不下时讲倍数，不讲 579% 那种像 bug 的数", () => {
  // 真链路实测：把窗口压到 1600 跑一条读三份 25KB 文件的任务，
  // 压缩前 9264 tokens，比率 5.79——数值是诚实的，读法不是。
  assert.equal(compactionWaterLabel(5.7859, 1600), "超出窗口 5.8 倍 · 窗口 1600");
  assert.equal(compactionWaterLabel(1.02, 128000), "超出窗口 1.0 倍 · 窗口 128000");
  assert.equal(compactionWaterLabel(0.83, 128000), "水位 83% · 窗口 128000");
  assert.equal(compactionWaterLabel(1, 128000), "水位 100% · 窗口 128000");
  assert.equal(compactionWaterLabel(0, 1600), "");
  assert.equal(compactionWaterLabel(5.8, 0), "", "窗口未知就不给分母，倍数也不行");
});

test("两张压缩卡各自带着自己的窗号，不合并成一张", () => {
  const blocks = buildBlocks([
    compacted({ ...fullPayload(), window_number: 1, window_id: "w-1", previous_window_id: "w-0" }, 11),
    compacted({ ...fullPayload(), window_number: 2, window_id: "w-2", previous_window_id: "w-1" }, 12),
  ]);
  const cards = blocks.filter((item) => item.kind === "boundary");
  assert.equal(cards.length, 2);
  const numbers = cards.map((item) => (item.kind === "boundary" ? item.windowChain.windowNumber : -1));
  assert.deepEqual(numbers, [1, 2]);
  const second = cards[1];
  if (second.kind !== "boundary") throw new Error("unexpected block");
  assert.equal(second.windowChain.previousWindowId, "w-1", "第二张卡要能指回第一张卡那一窗");
});
