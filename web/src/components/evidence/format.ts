/**
 * 证据库的读法：一条交付"到底证明了什么"。
 *
 * 这层的存在理由是**不许编**。验收条件三态（覆盖 / 没覆盖 / 不知道）、测试四态
 * （通过 / 失败 / 跳过 / 根本没跑），在界面上必须各说各的：把"不知道"显示成
 * "没覆盖"会让用户去补一份已经写好的验收；把"没跑测试"显示成"通过"更糟，
 * 那是拿一份不存在的证据给交付盖章。
 */

import type { Evidence, TestInfo } from "../tasks/api";

export type TestState = "passed" | "failed" | "skipped" | "none" | "unknown";

export function testState(test: TestInfo | null | undefined): TestState {
  if (!test) return "none";
  if (test.passed === true) return "passed";
  if (test.passed === false) return "failed";
  if ((test.skippedReason ?? "").trim()) return "skipped";
  const status = (test.status ?? "").trim().toLowerCase();
  if (status === "passed" || status === "success" || status === "ok") return "passed";
  if (status === "failed" || status === "error") return "failed";
  if (status === "skipped") return "skipped";
  // 命令和状态都没有：这不是"结果未知"，是压根没有测试记录。
  if (!status && !(test.command ?? "").trim()) return "none";
  return "unknown";
}

export const TEST_LABEL: Record<TestState, string> = {
  passed: "测试通过",
  failed: "测试失败",
  skipped: "测试跳过",
  none: "没有测试记录",
  unknown: "测试结果未判定",
};

export type EvidenceDigest = {
  criteriaTotal: number;
  covered: number;
  uncovered: number;
  /** 后端没判定的验收条件。与 uncovered 分开计数——一个要补写，一个要判定。 */
  unjudged: number;
  /** 去重后的改动文件数。同一文件改四次是"动了 1 个文件"，不是 4 个。 */
  files: number;
  test: TestState;
  testText: string;
  tokens: number | null;
};

export function evidenceDigest(evidence: Evidence | null | undefined): EvidenceDigest {
  const acceptance = evidence?.acceptance ?? [];
  const covered = acceptance.filter((item) => item.covered === true).length;
  const uncovered = acceptance.filter((item) => item.covered === false).length;
  const unjudged = acceptance.filter((item) => item.covered === null).length;
  const files = new Set((evidence?.changes ?? []).map((item) => item.path).filter(Boolean)).size;
  const state = testState(evidence?.test ?? null);
  const skipped = state === "skipped" ? (evidence?.test?.skippedReason ?? "").trim() : "";
  return {
    criteriaTotal: acceptance.length,
    covered,
    uncovered,
    unjudged,
    files,
    test: state,
    testText: skipped ? `${TEST_LABEL.skipped}：${skipped}` : TEST_LABEL[state],
    tokens: evidence?.usage?.totalTokens ?? null,
  };
}

/** 验收一行话：三种"没通过"的原因不同，合并成一句会丢信息。 */
export function criteriaText(digest: EvidenceDigest): string {
  if (digest.criteriaTotal === 0) return "没有验收条件";
  const parts = [`${digest.covered}/${digest.criteriaTotal} 覆盖`];
  if (digest.uncovered > 0) parts.push(`${digest.uncovered} 条没覆盖`);
  if (digest.unjudged > 0) parts.push(`${digest.unjudged} 条未判定`);
  return parts.join(" · ");
}

/**
 * 一条交付值不值得点开。
 *
 * 只按"有没有可看的东西"判：没有验收、没有改动、没跑测试、没用量的任务，
 * 展开只会得到一张空面板——不如在列表上直接认成"没有证据"。
 */
export function hasEvidence(digest: EvidenceDigest): boolean {
  return digest.criteriaTotal > 0 || digest.files > 0 || digest.test !== "none" || (digest.tokens ?? 0) > 0;
}
