/**
 * #82：审批卡的批准档位只跟后端下发的列表走。
 *
 * 缺陷本体在界面这一侧也有半份：卡片组件自己硬编码了三颗钮（含「本会话允许该工具」），
 * 而后端从 #82 起不再对命令类工具下发工具档——按下那颗钮账本上什么都不记，
 * 用户以为放行了一类操作，实际只批了眼前这一条。
 * 这里钉的是「界面无条件相信后端的档位表，缺键时也只补前端确定存在过的档位」。
 *
 * 「文案同步」还包括后半句：每一档免问多大一片，是后端算出来的（warning），
 * 前端只负责把它端上桌。档位名字相同的两颗钮，能放行多大范围完全不同——
 * 「本会话总是允许 python -c print(1)」只免问这一条命令，
 * 「本会话总是允许 apply_patch」免问的是整个档位+这片路径。少这一行，用户只能猜。
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { approvalGrantOptions, normalizeEvidence, type ApprovalGrant, type ApprovalScope } from "./api.ts";
import { buildBlocks } from "./timeline.ts";

const scopesOf = (options: { scope: ApprovalScope; label: string }[]) => options.map((item) => item.scope);
const labelsOf = (options: { scope: ApprovalScope; label: string }[]) => options.map((item) => item.label);
const warningsOf = (options: ApprovalGrant[]) => options.map((item) => item.warning);

test("后端下发两档就只给两档：命令类工具的卡上没有工具档", () => {
  const options = approvalGrantOptions({
    scopes: [
      { scope: "once", label: "仅本次允许" },
      { scope: "prefix", label: "本会话总是允许 python -c print(1)" },
    ],
  });
  assert.deepEqual(scopesOf(options), ["once", "prefix"]);
  // 文案逐字照抄后端，界面不再自己编——卡片写的和账本记的必须是同一串
  assert.deepEqual(labelsOf(options), ["仅本次允许", "本会话总是允许 python -c print(1)"]);
});

test("路径框得住的工具照旧下发三档，界面原样端出来", () => {
  const options = approvalGrantOptions({
    scopes: [
      { scope: "once", label: "仅本次允许" },
      { scope: "prefix", label: "本会话总是允许 src/a.py" },
      { scope: "tool", label: "本会话总是允许 apply_patch" },
    ],
  });
  assert.deepEqual(scopesOf(options), ["once", "prefix", "tool"]);
});

test("老载荷没有档位表时，只补前端确定存在过的档位，绝不补工具档", () => {
  assert.deepEqual(scopesOf(approvalGrantOptions({ prefix: "git push" })), ["once", "prefix"]);
  assert.deepEqual(labelsOf(approvalGrantOptions({ prefix: "git push" })), [
    "允许这一次",
    "本会话总是允许 git push",
  ]);
  assert.deepEqual(scopesOf(approvalGrantOptions({})), ["once"]);
  assert.deepEqual(scopesOf(approvalGrantOptions({ prefix: "" })), ["once"]);
});

test("重复档位只留一颗钮，缺 label 时按档位回退文案", () => {
  const options = approvalGrantOptions({
    prefix: "python -c print(1)",
    scopes: [
      { scope: "once", label: "" },
      { scope: "once", label: "仅本次允许" },
      { scope: "prefix", label: "" },
    ],
  });
  assert.deepEqual(scopesOf(options), ["once", "prefix"]);
  assert.deepEqual(labelsOf(options), ["允许这一次", "本会话总是允许 python -c print(1)"]);
});

test("后端只给了前缀档时界面仍要能提交「仅本次」", () => {
  // 少一颗"必然要点的那颗钮"，这张卡就没有退路；补上它不是加档，是最保守那一档本来就在后端语义里
  const options = approvalGrantOptions({
    scopes: [{ scope: "prefix", label: "本会话总是允许 git push" }],
  });
  assert.deepEqual(scopesOf(options), ["once", "prefix"]);
});

test("每一档的免问范围说明（warning）按档位原样带下来，顺序跟后端一致", () => {
  const options = approvalGrantOptions({
    scopes: [
      { scope: "once", label: "仅本次允许", warning: "" },
      {
        scope: "prefix",
        label: "本会话总是允许 python -c print(1)",
        warning: "只免问这一条命令：换正文、换参数、换模块或换脚本路径都会重新挂卡",
      },
      {
        scope: "tool",
        label: "本会话总是允许 apply_patch",
        warning: "该工具在本档位、这片路径内不再询问；换档位、换路径或不可逆命令仍逐条确认",
      },
    ],
  });
  assert.deepEqual(warningsOf(options), [
    "",
    "只免问这一条命令：换正文、换参数、换模块或换脚本路径都会重新挂卡",
    "该工具在本档位、这片路径内不再询问；换档位、换路径或不可逆命令仍逐条确认",
  ]);
});

test("前端不替后端编造免问范围：后端没写 warning 就是空串", () => {
  // 这一格钉的是"缺键时的默认值"。填一句界面自己想的说明，等于把账本语义换成文案，
  // 而卡片唯一该说的是后端那张账——它没给，就不能假装给了。
  assert.deepEqual(warningsOf(approvalGrantOptions({ prefix: "git push" })), ["", ""]);
  assert.deepEqual(
    warningsOf(approvalGrantOptions({ scopes: [{ scope: "prefix", label: "本会话总是允许 git push" }] })),
    ["", ""],
  );
  assert.deepEqual(
    warningsOf(approvalGrantOptions({ scopes: [{ scope: "tool", label: "本会话总是允许 apply_patch" }] })),
    ["", ""],
  );
});

test("warning 从接口键 approval_options 一路读到卡上（读了档位却丢了说明=半份同步）", () => {
  const approval = normalizeEvidence({
    pending_approvals: [
      {
        tool: "run_shell",
        arguments: { command: 'python -c "print(1)"' },
        approval_options: [
          { scope: "once", label: "仅本次允许", warning: "" },
          {
            scope: "prefix",
            label: "本会话总是允许 python -c print(1)",
            warning: "只免问这一条命令：换正文、换参数、换模块或换脚本路径都会重新挂卡",
          },
        ],
      },
    ],
  }).pendingApprovals[0];
  assert.deepEqual(warningsOf(approval.scopes), [
    "",
    "只免问这一条命令：换正文、换参数、换模块或换脚本路径都会重新挂卡",
  ]);
  assert.deepEqual(warningsOf(approvalGrantOptions(approval)), [
    "",
    "只免问这一条命令：换正文、换参数、换模块或换脚本路径都会重新挂卡",
  ]);

  const legacy = normalizeEvidence({
    pending_approvals: [{ tool: "run_shell", arguments: {}, suggested_prefix: "git push" }],
  }).pendingApprovals[0];
  assert.deepEqual(legacy.scopes, [], "老载荷没有档位表，解析结果必须是空表而不是编出来的一档");
});

test("档位表从接口键 approval_options 一路读到卡上（解析了却没人读=缺陷本体）", () => {
  const approval = normalizeEvidence({
    pending_approvals: [
      {
        tool: "run_shell",
        arguments: { command: 'python -c "print(1)"' },
        approval_options: [
          { scope: "once", label: "仅本次允许" },
          { scope: "prefix", label: "本会话总是允许 python -c print(1)" },
        ],
      },
    ],
  }).pendingApprovals[0];
  assert.deepEqual(scopesOf(approval.scopes), ["once", "prefix"]);
  assert.deepEqual(scopesOf(approvalGrantOptions(approval)), ["once", "prefix"]);

  const legacy = normalizeEvidence({
    pending_approvals: [{ tool: "run_shell", arguments: {}, suggested_prefix: "git push" }],
  }).pendingApprovals[0];
  assert.deepEqual(legacy.scopes, [], "老载荷没有档位表，解析结果必须是空表而不是编出来的一档");
  assert.equal(legacy.prefix, "git push");
  assert.deepEqual(scopesOf(approvalGrantOptions(legacy)), ["once", "prefix"]);
});

test("事件载荷里的 warning 经时间线建卡后仍在 block 上（卡片读的是这条通路）", () => {
  // 卡片组件用的是 timeline.ts 建出来的 Block，不是 api.ts 那份 pendingApprovals：
  // 这条通路少读一个键，接口层再全也不会有任何一行说明上屏。
  const block = buildBlocks([
    {
      seq: 65,
      node: "run_tools",
      type: "approval.required",
      createdAt: "2026-10-25T00:00:00+00:00",
      payload: {
        tool: "run_shell",
        tool_call_id: "call-1",
        arguments: { command: 'python -c "print(1)"' },
        approval_options: [
          { scope: "once", label: "仅本次允许", warning: "" },
          {
            scope: "prefix",
            label: "本会话总是允许 python -c print(1)",
            warning: "只免问这一条命令：换正文、换参数、换模块或换脚本路径都会重新挂卡",
          },
        ],
      },
    },
  ]).find((item) => item.kind === "approval");
  assert.ok(block && block.kind === "approval", "审批事件要在时间线里建卡");
  assert.deepEqual(scopesOf(block.scopes), ["once", "prefix"]);
  assert.deepEqual(warningsOf(block.scopes), [
    "",
    "只免问这一条命令：换正文、换参数、换模块或换脚本路径都会重新挂卡",
  ]);
  assert.deepEqual(warningsOf(approvalGrantOptions(block)), [
    "",
    "只免问这一条命令：换正文、换参数、换模块或换脚本路径都会重新挂卡",
  ]);
});
