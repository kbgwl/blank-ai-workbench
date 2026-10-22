"""Propose / Probe / Compose: choose a harness by measuring it.

The loop is deliberately boring:

1. **Propose** — turn the base tree plus the domain library into a handful of
   complete candidate trees (:mod:`proposer`).
2. **Probe** — run the same tasks against every candidate *and* against the
   untouched baseline. Same fixtures, same model, same scoring.
3. **Compose** — pick the winner under a correctness gate and a minimum gain,
   then store exactly the tree that was measured.

Three rules keep the result honest:

* The baseline is always probed. A candidate is only "better" relative to a
  measured baseline, never relative to an imagined one.
* A candidate that changes ``.blank/harness`` during a run is disqualified. If
  the agent can rewrite its own instructions mid-run, the measurement no longer
  belongs to the tree that was installed.
* What is adopted is what was probed. The compose stage validates and records
  the winner; it does not quietly add extra text after the last measurement.

Selection is conservative: all probed tasks must pass the correctness gate
(verify green *and* at least one file changed) and the mean score must beat the
baseline by ``min_gain``.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, Sequence

from blank_api.services.harness import spec
from blank_api.services.harness.library import Experience, Library
from blank_api.services.harness.proposer import (
    Candidate,
    ProposalRequest,
    Proposer,
    RuleProposer,
    sanitize,
)
from blank_api.services.harness.score import Score, cheapest_tokens, score_task


class ProbeRunner(Protocol):
    """One task run. ``record`` only needs the attributes read below."""

    async def run(self, sample: Mapping[str, Any], tree: Mapping[str, str]) -> Any: ...


@dataclass
class FailedRecord:
    """A run that never produced evidence; scored like any other failure."""

    status: str = "error"
    verify_ok: bool = False
    changed: list[str] = field(default_factory=list)
    self_verified: bool = False
    tokens: int = 0
    turns: int = 0
    harness_dirty: bool = False
    error: str = ""


@dataclass
class CandidateRun:
    candidate: Candidate
    records: list[Any] = field(default_factory=list)
    scores: list[Score] = field(default_factory=list)
    tokens: int = 0
    wall_ms: int = 0
    disqualified: str = ""
    primary_count: int = 0

    @property
    def mean(self) -> float:
        return round(sum(item.total for item in self.scores) / len(self.scores), 2) if self.scores else 0.0

    @property
    def primary_scores(self) -> list[Score]:
        return self.scores[: self.primary_count] if self.primary_count else self.scores

    @property
    def regression_scores(self) -> list[Score]:
        return self.scores[self.primary_count :] if self.primary_count else []

    @property
    def primary_mean(self) -> float:
        scores = self.primary_scores
        return round(sum(item.total for item in scores) / len(scores), 2) if scores else 0.0

    @property
    def regression_mean(self) -> float:
        scores = self.regression_scores
        return round(sum(item.total for item in scores) / len(scores), 2) if scores else 0.0

    @property
    def vector(self) -> list[float]:
        return [item.total for item in self.primary_scores]

    def as_dict(self) -> dict:
        return {
            "label": self.candidate.label,
            "origin": self.candidate.origin,
            "why": self.candidate.why,
            "files": sorted(self.candidate.tree),
            "mean": self.mean,
            "primary_mean": self.primary_mean,
            "regression_mean": self.regression_mean,
            "vector": self.vector,
            "tokens": self.tokens,
            "wall_ms": self.wall_ms,
            "runs": len(self.records),
            "disqualified": self.disqualified,
            "scores": [item.as_dict() for item in self.scores],
        }


@dataclass
class OptimizeReport:
    run_id: str
    domain: str
    entry_id: str
    baseline: CandidateRun | None
    runs: list[CandidateRun]
    winner: CandidateRun | None
    adopted: bool
    reason: str
    library_entry: str = ""
    budget_exhausted: bool = False
    notes: list[str] = field(default_factory=list)
    primary_count: int = 0
    regression_count: int = 0
    reflections: int = 0
    pareto: list[dict] = field(default_factory=list)

    @property
    def gain(self) -> float:
        if self.winner is None or self.baseline is None:
            return 0.0
        return round(self.winner.primary_mean - self.baseline.primary_mean, 2)

    @property
    def regression_drop(self) -> float:
        if self.winner is None or self.baseline is None or not self.regression_count:
            return 0.0
        return round(self.baseline.regression_mean - self.winner.regression_mean, 2)

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "domain": self.domain,
            "entry_id": self.entry_id,
            "adopted": self.adopted,
            "reason": self.reason,
            "library_entry": self.library_entry,
            "budget_exhausted": self.budget_exhausted,
            "notes": list(self.notes),
            "gain": self.gain,
            "primary_count": self.primary_count,
            "regression_count": self.regression_count,
            "regression_drop": self.regression_drop,
            "reflections": self.reflections,
            "pareto": list(self.pareto),
            "baseline": self.baseline.as_dict() if self.baseline else None,
            "candidates": [item.as_dict() for item in self.runs],
            "winner": self.winner.as_dict() if self.winner else None,
            "winner_tree": dict(self.winner.candidate.tree) if self.winner else {},
        }

    def render(self) -> str:
        lines = [
            f"# harness 优化报告 {self.run_id}",
            "",
            f"- 领域：{self.domain or '未标注'}",
            f"- 基线均分：{self.baseline.primary_mean if self.baseline else '-'}",
        ]
        for run in self.runs:
            tail = f"（{run.disqualified}）" if run.disqualified else ""
            lines.append(
                f"- 候选 `{run.candidate.label}`：{run.primary_mean} 分 / {run.tokens} tokens{tail}"
            )
        if self.regression_count:
            lines.append(
                f"- 回归集：{self.regression_count} 题，"
                f"基线 {self.baseline.regression_mean if self.baseline else '-'}，"
                f"赢家 {self.winner.regression_mean if self.winner else '-'}"
            )
        if self.pareto:
            lines.append(
                "- 帕累托前沿：" + "、".join(f"`{item['label']}`" for item in self.pareto)
            )
        if self.reflections:
            lines.append(f"- 反思轮新增候选：{self.reflections}")
        lines.append(f"- 结论：{self.reason}")
        if self.adopted:
            lines.append(f"- 已写入经验库：`{self.library_entry}`")
        for note in self.notes:
            lines.append(f"- 备注：{note}")
        return "\n".join(lines)


async def optimize(
    samples: Sequence[Mapping[str, Any]],
    *,
    runner: ProbeRunner,
    proposer: Proposer | None = None,
    reflector: Proposer | None = None,
    library: Library | None = None,
    domain: str = "",
    base_tree: Mapping[str, str] | None = None,
    regression_samples: Sequence[Mapping[str, Any]] | None = None,
    count: int = 3,
    min_gain: float = 0.5,
    max_regression_drop: float = 0.0,
    max_token_growth: float = 1.0,
    reflect_rounds: int = 1,
    max_tokens: int = 2_000_000,
    max_seconds: float = 3_600.0,
    adopt: bool = True,
    entry_id: str = "",
    run_id: str = "",
) -> OptimizeReport:
    if not samples:
        raise ValueError("至少需要一个样例任务")

    primary = [dict(item) for item in samples]
    primary_ids = {
        str(item.get("id") or f"task-{index + 1}") for index, item in enumerate(primary)
    }
    regression: list[Mapping[str, Any]] = []
    for item in regression_samples or []:
        sid = str(item.get("id") or "")
        if sid and sid in primary_ids:
            continue
        regression.append(item)
    all_samples = [*primary, *regression]
    primary_count = len(primary)
    regression_count = len(regression)

    started = time.monotonic()
    deadline = started + max(0.0, float(max_seconds))
    run_id = run_id or uuid.uuid4().hex[:8]
    domain = domain or ""
    ids = [str(item.get("id") or f"task-{index + 1}") for index, item in enumerate(primary)]
    entry_id = entry_id or (ids[0] if len(ids) == 1 else "+".join(ids))
    base = {str(rel): str(body) for rel, body in (base_tree or {}).items()}

    proposer = proposer or RuleProposer()
    proposal = await proposer.propose(
        ProposalRequest(
            base=base,
            domain=domain,
            title=str(primary[0].get("title") or ""),
            count=count,
            library=library,
        )
    )

    baseline_candidate = Candidate(
        label="baseline",
        tree=base,
        why="不安装候选 harness（当前基线树）",
        origin="baseline",
    )
    baseline = await _probe(baseline_candidate, all_samples, runner, deadline)
    baseline.primary_count = primary_count
    total_tokens = baseline.tokens
    budget_exhausted = total_tokens > max_tokens or time.monotonic() > deadline

    runs: list[CandidateRun] = []
    seen = {baseline_candidate.candidate_key()}
    notes: list[str] = []

    async def probe_batch(candidates: Sequence[Candidate]) -> int:
        nonlocal total_tokens, budget_exhausted
        before = len(runs)
        for candidate in candidates:
            if budget_exhausted:
                break
            key = candidate.candidate_key()
            if key in seen:
                continue
            seen.add(key)
            run = await _probe(candidate, all_samples, runner, deadline)
            run.primary_count = primary_count
            runs.append(run)
            total_tokens += run.tokens
            if total_tokens > max_tokens or time.monotonic() > deadline:
                budget_exhausted = True
        return len(runs) - before

    await probe_batch(proposal)
    _score_all(all_samples, [baseline, *runs])

    reflections = 0
    if reflector is not None and int(reflect_rounds) > 0:
        for _ in range(int(reflect_rounds)):
            if budget_exhausted:
                notes.append("预算耗尽，跳过后续反思轮")
                break
            evidence = _failure_evidence(primary, [baseline, *runs], primary_count)
            if not evidence:
                notes.append("没有仍未通过的主任务，跳过反思轮")
                break
            try:
                reflected = await reflector.propose(
                    ProposalRequest(
                        base=base,
                        domain=domain,
                        title=str(primary[0].get("title") or ""),
                        count=count,
                        library=library,
                        failures=evidence,
                    )
                )
            except Exception as exc:  # 反思失败不能推翻已经测过的候选
                notes.append(f"反思提案失败：{exc.__class__.__name__}: {exc}")
                break
            fresh = [item for item in reflected if item.candidate_key() not in seen]
            if not fresh:
                notes.append("反思没有给出新候选")
                break
            added = await probe_batch(fresh)
            reflections += added
            _score_all(all_samples, [baseline, *runs])
            if added == 0:
                notes.append("反思候选与已测候选重复")
                break

    # 成本分要按“同一题里最省的运行”对齐，基线与回归集也参与比较。
    _score_all(all_samples, [baseline, *runs])

    expected = len(all_samples)
    eligible = [
        run
        for run in runs
        if not run.disqualified
        and len(run.records) == expected
        and len(run.scores) == expected
        and all(score.tests == 1.0 for score in run.primary_scores)
    ]

    winner: CandidateRun | None = None
    reason = ""
    adopted = False
    library_entry = ""

    if not baseline.scores:
        reason = "基线未跑完，无法比较，不采纳任何候选"
    elif not runs:
        reason = "推荐器没有给出候选，保留基线"
    elif not eligible:
        reason = "没有候选通过正确性守门（主任务验证通过且至少改动一个文件）"
    else:
        best = max(eligible, key=lambda item: (item.primary_mean, -item.tokens))
        passing: list[CandidateRun] = []
        for run in eligible:
            problems = _gate_problems(
                run,
                baseline,
                min_gain=min_gain,
                max_regression_drop=max_regression_drop,
                max_token_growth=max_token_growth,
                regression_count=regression_count,
            )
            if problems:
                notes.append(
                    f"`{run.candidate.label}` 未通过采纳门禁：{'；'.join(problems)}"
                )
            else:
                passing.append(run)

        if not passing:
            problems = _gate_problems(
                best,
                baseline,
                min_gain=min_gain,
                max_regression_drop=max_regression_drop,
                max_token_growth=max_token_growth,
                regression_count=regression_count,
            )
            reason = (
                f"最佳候选 `{best.candidate.label}` 未通过采纳门禁："
                f"{'；'.join(problems)}，保留基线"
            )
            winner = None
        else:
            winner = max(passing, key=lambda item: (item.primary_mean, -item.tokens))
            gain = winner.primary_mean - baseline.primary_mean
            reason = (
                f"采纳 `{winner.candidate.label}`：主任务均分 {winner.primary_mean:.1f}，"
                f"基线 {baseline.primary_mean:.1f}，增益 {gain:.1f}"
            )
            if regression_count:
                reason += (
                    f"；回归集 {winner.regression_mean:.1f}（掉分 "
                    f"{baseline.regression_mean - winner.regression_mean:.1f}）"
                )
            if adopt:
                if library is None:
                    reason += "；未提供经验库，只返回结果"
                    notes.append("没有经验库，无法落盘")
                else:
                    try:
                        # 采纳的树必须与探测的树逐字节一致；compose 只做校验与记账。
                        spec.validate(winner.candidate.tree)
                        experience = Experience(
                            cue=_cue(domain, primary[0]),
                            lesson=winner.candidate.why
                            or f"候选 {winner.candidate.label} 在探测中优于基线",
                            evidence=(
                                f"{primary_count} 道主任务平均 {winner.primary_mean:.1f} 分，"
                                f"基线 {baseline.primary_mean:.1f}"
                                + (
                                    f"；{regression_count} 道回归任务平均 {winner.regression_mean:.1f}，"
                                    f"基线 {baseline.regression_mean:.1f}"
                                    if regression_count
                                    else ""
                                )
                                + "；主任务验证通过且至少改动一个文件"
                            ),
                            confidence="high" if gain >= 10 else "medium" if gain >= 3 else "low",
                            edits=spec.summarize(spec.diff_tree(base, winner.candidate.tree)),
                            scope=domain,
                            last_verified=datetime.now(timezone.utc).isoformat(
                                timespec="seconds"
                            ),
                        )
                        target = library.add(
                            entry_id,
                            winner.candidate.tree,
                            experience=experience,
                            why=winner.candidate.why,
                            label=f"run-{run_id}-{winner.candidate.label}",
                            run_id=run_id,
                        )
                        if target is None:
                            reason += "；经验不完整，未写入经验库"
                            notes.append("lesson 缺字段，经验库拒绝写入")
                        else:
                            adopted = True
                            library_entry = str(target)
                    except (spec.HarnessError, OSError) as exc:
                        reason += "；采纳树校验失败，未写入"
                        notes.append(str(exc)[:500])
            else:
                notes.append("adopt=False，只返回候选，不写经验库")

    # 失败也入库：只记录基线仍未解决的题，作为下一次提案的反例，而不是规则。
    if adopt and library is not None and baseline.scores:
        unresolved = _failure_evidence(primary, [baseline], primary_count)
        if unresolved:
            first = unresolved[0]
            detail = (
                sanitize(first.get("verify_output") or first.get("error") or "")
                or "验证未通过或没有产生改动"
            )
            try:
                library.add_failure(
                    f"{entry_id}-baseline",
                    experience=Experience(
                        cue=_cue(domain, primary[0]),
                        lesson=(
                            f"基线在 {len(unresolved)} 道主任务上未通过；"
                            f"先处理「{first.get('sample_id')}」这类失败，再谈新增规则。"
                        ),
                        evidence=f"轨迹/验证摘录：{detail[:800]}",
                        confidence="low",
                        scope=domain,
                        last_verified=datetime.now(timezone.utc).isoformat(
                            timespec="seconds"
                        ),
                    ),
                    label=f"run-{run_id}-baseline",
                    run_id=run_id,
                )
                notes.append(f"已记录 {len(unresolved)} 条基线失败教训")
            except OSError as exc:
                notes.append(f"失败教训未写入：{exc}")

    return OptimizeReport(
        run_id=run_id,
        domain=domain,
        entry_id=entry_id,
        baseline=baseline,
        runs=runs,
        winner=winner,
        adopted=adopted,
        reason=reason,
        library_entry=library_entry,
        budget_exhausted=budget_exhausted,
        notes=notes,
        primary_count=primary_count,
        regression_count=regression_count,
        reflections=reflections,
        pareto=_pareto([baseline, *runs], primary_count),
    )


def _gate_problems(
    run: CandidateRun,
    baseline: CandidateRun,
    *,
    min_gain: float,
    max_regression_drop: float,
    max_token_growth: float,
    regression_count: int,
) -> list[str]:
    """The three adoption gates, in the order a reviewer would ask about them."""
    problems: list[str] = []
    gain = round(run.primary_mean - baseline.primary_mean, 2)
    if gain < min_gain:
        problems.append(f"增益 {gain:.1f} 低于阈值 {min_gain:.1f}")
    if regression_count:
        drop = round(baseline.regression_mean - run.regression_mean, 2)
        if drop > max_regression_drop:
            problems.append(f"回归集掉分 {drop:.1f} 超过允许 {max_regression_drop:.1f}")
    growth = max(0.0, float(max_token_growth))
    if baseline.tokens > 0 and run.tokens > baseline.tokens * (1.0 + growth):
        ratio = run.tokens / baseline.tokens
        problems.append(f"token 增长 {ratio:.2f}× 超过上限 {1.0 + growth:.2f}×")
    return problems


def _failure_evidence(
    samples: Sequence[Mapping[str, Any]],
    runs: Sequence[CandidateRun],
    primary_count: int,
) -> list[dict]:
    """For each primary task, the best observed attempt if it still failed.

    This is what makes reflection different from "ask the model for more
    ideas": the prompt only sees tasks that resisted every measured candidate,
    and it sees the exact verify output and tool trajectory that resisted.
    """
    out: list[dict] = []
    for index, sample in enumerate(samples[:primary_count]):
        observations: list[tuple[CandidateRun, Any, Score]] = []
        for run in runs:
            if index >= len(run.records) or index >= len(run.scores):
                continue
            observations.append((run, run.records[index], run.scores[index]))
        if not observations:
            continue
        run, record, score = max(
            observations, key=lambda item: (item[2].tests, item[2].total)
        )
        failed = (
            not bool(getattr(record, "verify_ok", False))
            or not list(getattr(record, "changed", []) or [])
            or score.tests < 1.0
            or bool(getattr(record, "error", ""))
        )
        if not failed:
            continue
        out.append(
            {
                "sample_id": str(sample.get("id") or f"task-{index + 1}"),
                "title": str(sample.get("title") or ""),
                "label": run.candidate.label,
                "verify_ok": bool(getattr(record, "verify_ok", False)),
                "changed": list(getattr(record, "changed", []) or []),
                "expect_files": [str(item) for item in (sample.get("expect_files") or [])],
                "score": score.total,
                "tokens": int(getattr(record, "tokens", 0) or 0),
                "verify_output": str(getattr(record, "verify_output", "") or ""),
                "trajectory": str(getattr(record, "trajectory", "") or ""),
                "error": str(getattr(record, "error", "") or ""),
            }
        )
    return out[:6]


def _pareto(runs: Sequence[CandidateRun], primary_count: int) -> list[dict]:
    """Non-dominated candidates over per-task primary scores."""
    pool = [
        run
        for run in runs
        if not run.disqualified
        and len(run.primary_scores) == primary_count
        and run.primary_scores
    ]
    archive: list[dict] = []
    for run in pool:
        vector = run.vector
        dominated = False
        for other in pool:
            if other is run:
                continue
            other_vector = other.vector
            if all(a >= b for a, b in zip(other_vector, vector)) and any(
                a > b for a, b in zip(other_vector, vector)
            ):
                dominated = True
                break
        if dominated:
            continue
        archive.append(
            {
                "label": run.candidate.label,
                "origin": run.candidate.origin,
                "mean": run.primary_mean,
                "vector": vector,
                "tokens": run.tokens,
            }
        )
    return sorted(archive, key=lambda item: item["mean"], reverse=True)


async def _probe(
    candidate: Candidate,
    samples: Sequence[Mapping[str, Any]],
    runner: ProbeRunner,
    deadline: float,
) -> CandidateRun:
    started = time.monotonic()
    records: list[Any] = []
    tokens = 0
    disqualified = ""
    for sample in samples:
        if time.monotonic() > deadline:
            disqualified = "时间预算耗尽，未跑完"
            break
        try:
            record = await runner.run(sample, candidate.tree)
        except Exception as exc:  # 单个候选失败不能中止整轮
            record = FailedRecord(error=f"{exc.__class__.__name__}: {exc}")
        records.append(record)
        tokens += max(0, int(getattr(record, "tokens", 0) or 0))
        if bool(getattr(record, "harness_dirty", False)):
            disqualified = "运行期间改动了 .blank/harness，无法归因"
            break
    return CandidateRun(
        candidate=candidate,
        records=records,
        tokens=tokens,
        wall_ms=int((time.monotonic() - started) * 1000),
        disqualified=disqualified,
    )


def _score_all(samples: Sequence[Mapping[str, Any]], runs: Sequence[CandidateRun]) -> None:
    for run in runs:
        run.scores.clear()
    for index, sample in enumerate(samples):
        observed = [
            int(getattr(run.records[index], "tokens", 0) or 0)
            for run in runs
            if index < len(run.records)
        ]
        cheapest = cheapest_tokens(observed)
        expected = [str(item) for item in (sample.get("expect_files") or [])]
        for run in runs:
            if index >= len(run.records):
                continue
            record = run.records[index]
            run.scores.append(
                score_task(
                    verify_ok=bool(getattr(record, "verify_ok", False)),
                    changed=list(getattr(record, "changed", []) or []),
                    expect_files=expected,
                    self_verified=bool(getattr(record, "self_verified", False)),
                    tokens=int(getattr(record, "tokens", 0) or 0),
                    cheapest_tokens=cheapest,
                )
            )


def _cue(domain: str, sample: Mapping[str, Any]) -> str:
    title = str(sample.get("title") or "").strip()
    if domain and title:
        return f"{domain}：{title}"
    return title or domain or "同类任务"
