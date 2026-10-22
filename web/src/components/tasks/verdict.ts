/**
 * 「这次到底验过没有」的唯一口径。
 *
 * 后端在没跑成测试时把 `passed` 记成 null（跳过），前端三个展示位（时间线、
 * 证据面板、工作台摘要）都必须读出同一个意思。各自写一遍三元表达式，迟早出现
 * 一处说「未验证」、另一处显示原始状态串让用户以为通过——那是比一律说错更难查的
 * 不一致。所以词汇表放在这个无依赖的叶子模块里，直接可测。
 */

export type VerdictTone = "accent" | "danger" | "warn";
export type Verdict = { title: string; label: string; tone: VerdictTone };

/**
 * `passed` 只有 true / false / null 三种：null 表示这次没做成自动化校验
 * （跳过、被拒、等审批、环境起不来），不是"跑过了但没结论"。
 */
export function verdictOf(passed: boolean | null, status: string): Verdict {
  if (passed === true) return { title: "验证完成", label: "通过", tone: "accent" };
  if (passed === false) return { title: "验证未通过", label: "未通过", tone: "danger" };
  return { title: "未做自动化校验", label: "未验证", tone: "warn" };
}
