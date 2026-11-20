/**
 * 错误分层：每一档失败都要答出"发生了什么"和"我该做什么"。
 *
 * 这里的断言不是文案校对，是三条判据：
 * 1. 标题里不出现错误码——码是给排查用的，不该是第一眼读的东西；
 * 2. 说要重试的，必须真的能靠重试解决；说要改设置的，不许给重试按钮；
 * 3. 认不出来的输入不许编原因。
 */
import { strict as assert } from "node:assert";
import { test } from "node:test";

import { explainFailure, failureMeta, failureOriginal, failureParts } from "./error-copy.ts";

test("登记过的码：标题是人话，码只在 code 字段里", () => {
  for (const code of [
    "model_timeout",
    "model_unreachable",
    "model_http_error",
    "model_stream_empty",
    "model_tools_unsupported",
    "all_models_failed",
    "approval_expired",
    "context_overflow",
  ]) {
    const copy = explainFailure("", code);
    assert.equal(copy.code, code, code);
    assert.ok(copy.title.length > 4, `${code} 没有标题`);
    assert.ok(!copy.title.includes(code), `${code} 的标题里裸奔了错误码：${copy.title}`);
    assert.ok(copy.action.length > 0, `${code} 没告诉用户怎么办`);
  }
});

test("终态码与 verifier 硬违规码：每个都有专属文案，且不是靠中文猜出来的", () => {
  // 后端 agent/terminal.py 的 TERMINAL_CODES 与 verifier.py 的 BLOCKING_CODES。
  // 这些码出现时，`reason` 往往是一整句中文——若这里没登记，就会掉进下面的
  // 中文正则兜底，同一件事换个说法就认不出。所以逐条钉住"有码就有专属文案"。
  const terminal = [
    "budget_exhausted",
    "verification_blocked",
    "validation_failed",
    "validation_timeout",
    "validation_error",
    "validation_blocked",
    "validation_skipped",
    "hook_blocked",
    "model_error",
    "failed",
  ];
  const hard = ["zero_change", "false_claim", "delivery_incomplete", "test_config_broken", "zero_delivery"];
  for (const code of [...terminal, ...hard]) {
    const copy = explainFailure("", code);
    assert.ok(copy.title.length > 4, `${code} 没有标题`);
    assert.ok(copy.action.length > 0, `${code} 没告诉用户怎么办`);
    assert.ok(!copy.title.includes(code), `${code} 的标题里裸奔了错误码：${copy.title}`);
    // 标题不能是"这一轮没跑完，但后端没留下原因"这种什么都不说的兜底。
    assert.ok(!copy.title.includes("没留下原因"), `${code} 掉进了兜底文案`);
  }
  // 硬违规都该是「重试能解决」这一档：改代码/补齐交付物，不是去改设置。
  for (const code of hard) assert.equal(explainFailure("", code).next, "retry", code);
  // 钩子拦下不是重试能解决的：得改钩子配置。
  assert.equal(explainFailure("", "hook_blocked").next, "settings");
});

test("终态码优先于中文文本：同一句话换成别的说法，结论不变", () => {
  // 后端给 detail 时，界面按码出文案；`reason` 只作为原文进折叠区。
  const a = explainFailure("目标要求改动，但工作区没有任何文件变化", "zero_change");
  const b = explainFailure("磁盘零改动（零交付）", "zero_change");
  assert.equal(a.title, b.title);
  assert.equal(a.code, "zero_change");
});

test("取消与暂停有码可查：不该掉进「后端没留下原因」那条兜底", () => {
  // 这两档不是失败，但终态码会带上来。空 reason + 有码时若没登记，
  // 界面会说"这一轮没跑完，但后端没留下原因"——原因明明就在码里。
  const cancelled = explainFailure("", "cancelled");
  assert.ok(cancelled.title.includes("取消"), cancelled.title);
  assert.equal(cancelled.next, "none");
  const paused = explainFailure("", "paused");
  assert.ok(paused.title.includes("暂停"), paused.title);
  assert.equal(paused.next, "none");
});

test("能靠重试解决的才给重试，改设置的不给假按钮", () => {
  // 上游排队 / 网络抖：重试有意义。
  assert.equal(explainFailure("model_timeout").next, "retry");
  assert.equal(explainFailure("model_stream_error").next, "retry");
  assert.equal(explainFailure("approval_expired").next, "retry");
  // 密钥、额度、模型不支持工具：重试一百次也一样。
  assert.equal(explainFailure("model_http_error").next, "settings");
  assert.equal(explainFailure("model_tools_unsupported").next, "settings");
  assert.equal(explainFailure("all_models_failed").next, "settings");
  assert.equal(explainFailure("model_unreachable").next, "settings");
  // 用户自己取消的，不给任何动作。
  assert.equal(explainFailure("任务已取消").next, "none");
});

test("没有码时按文本认成因", () => {
  assert.equal(explainFailure("所有支持工具的候选模型均调用失败：模型返回 429").next, "settings");
  assert.equal(explainFailure("模型返回 401：invalid api key").next, "settings");
  assert.equal(explainFailure("模型请求超时：qwen").next, "retry");
  assert.equal(explainFailure("connect ECONNREFUSED 127.0.0.1:8000").next, "settings");
  assert.equal(explainFailure("钩子拒绝执行 write_file：改系统盘要先备份").next, "settings");
  // 「不支持工具调用」不能因为句子里有"失败"就归到可重试那一档。
  assert.equal(explainFailure("模型 qwen 不支持工具调用").next, "settings");
});

test("码比文本优先：文本里写着超时但码是未授权，按码说", () => {
  // 供应商原文常常一通乱写，只有 code 是后端自己判的。
  const copy = explainFailure("请求超时", "model_http_error");
  assert.equal(copy.code, "model_http_error");
  assert.equal(copy.next, "settings");
});

test("英文栈不进标题：它是日志，不是一句话", () => {
  const copy = explainFailure(
    'Traceback (most recent call last):\n  File "graph.py", line 91, in run\nValueError: bad state',
  );
  assert.ok(!copy.title.includes("Traceback"), copy.title);
  assert.match(copy.title, /报了个错/);
  // 但原文不能丢——它是唯一线索，进折叠区。
  assert.match(failureOriginal("Traceback (most recent call last): boom"), /Traceback/);
});

test("长中文句只取第一句当标题，剩下的进详情", () => {
  const long =
    "提交被钩子拦下：这个项目禁止直接改生产配置；如果要改，先在 staging 上验证；验证通过后再走审批。";
  const copy = explainFailure(long);
  assert.ok(copy.title.length <= 76, copy.title);
  assert.ok(!copy.title.includes("staging 上验证"), copy.title);
});

test("认不出来的码不编原因，把码留进详情", () => {
  const copy = explainFailure("some_new_code");
  assert.equal(copy.code, "some_new_code");
  assert.match(copy.title, /没跑完/);
  assert.ok(!copy.title.includes("some_new_code"));
});

test("空原因也要有一句能读的话", () => {
  const copy = explainFailure("");
  assert.ok(copy.title.length > 0);
  assert.equal(copy.code, "");
});

test("折叠区第一行：有码写码，有时间写时间，都没有就不占位", () => {
  assert.deepEqual(failureMeta("model_timeout", "21:07"), ["code model_timeout", "21:07"]);
  assert.deepEqual(failureMeta("", "21:07"), ["21:07"]);
  assert.deepEqual(failureMeta("", ""), []);
});

test("太短的原文不值得展开", () => {
  // "失败"两个字展开还是"失败"，纯占地方。
  assert.equal(failureOriginal("失败"), "");
  assert.equal(failureOriginal("  "), "");
  assert.equal(failureOriginal("模型请求超时：gpt-4o"), "模型请求超时：gpt-4o");
});

test("裸码归到第一行，正文栏不重复印一遍已经翻译过的词", () => {
  // 老数据：reason 就是 approval_expired，没有单独的 code 字段。
  const parts = failureParts("approval_expired", "", "18:09");
  assert.deepEqual(parts.meta, ["code approval_expired", "18:09"]);
  assert.equal(parts.original, "");

  // 有显式 code 时以它为准，raw 里的句子进正文。
  const real = failureParts("模型请求超时：qwen", "model_timeout", "18:09");
  assert.deepEqual(real.meta, ["code model_timeout", "18:09"]);
  assert.equal(real.original, "模型请求超时：qwen");
});
