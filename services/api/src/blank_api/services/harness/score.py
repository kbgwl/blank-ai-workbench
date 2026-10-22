"""The five-part task score, ported from ``evals/compare.mjs``.

Keeping one implementation matters: the offline optimizer selects candidates
with these numbers, and a candidate that looks better only because Python and
Node round differently would be a silent regression. The weights and the
extra-file penalty are copied as-is:

    correctness 45%  verify passes *and* at least one file changed
    hit         20%  fraction of the expected files that actually changed
    minimal     15%  no unexpected files: 0 extra → 1, 1 → 0.6, 2 → 0.3, else 0
    self        10%  the agent ran a verification command itself
    cost        10%  cheapest tokens / this run's tokens, capped at 1
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

WEIGHT_CORRECTNESS = 0.45
WEIGHT_HIT = 0.20
WEIGHT_MINIMAL = 0.15
WEIGHT_SELF = 0.10
WEIGHT_COST = 0.10


@dataclass(frozen=True)
class Score:
    tests: float
    hit: float
    minimal: float
    self_verified: float
    cost: float
    total: float

    def as_dict(self) -> dict:
        return {
            "tests": self.tests,
            "hit": self.hit,
            "minimal": self.minimal,
            "self": self.self_verified,
            "cost": self.cost,
            "total": self.total,
        }

    def render(self) -> str:
        return (
            f"{self.total:.1f} 分（正确性 {self.tests:.2f}、命中 {self.hit:.2f}、"
            f"最小改动 {self.minimal:.2f}、自证 {self.self_verified:.2f}、成本 {self.cost:.2f}）"
        )


def score_task(
    *,
    verify_ok: bool,
    changed: Sequence[str],
    expect_files: Iterable[str] = (),
    self_verified: bool = False,
    tokens: int = 0,
    cheapest_tokens: int = 0,
) -> Score:
    """Score one finished task. ``changed`` are workspace-relative POSIX paths."""
    changed_list = [str(item) for item in changed]
    expected = [str(item) for item in expect_files]

    # A green verify with no changes means the baseline was already green and the
    # agent did nothing. Without this clause doing nothing would score 60.
    tests = 1.0 if verify_ok and changed_list else 0.0
    hit = (
        sum(1 for rel in expected if rel in changed_list) / len(expected) if expected else 1.0
    )
    extra = sum(1 for rel in changed_list if rel not in expected)
    minimal = 1.0 if extra == 0 else 0.6 if extra == 1 else 0.3 if extra == 2 else 0.0
    self_part = 1.0 if self_verified else 0.0
    cost = (
        min(1.0, cheapest_tokens / tokens) if tokens > 0 and cheapest_tokens > 0 else 0.0
    )
    total = (
        WEIGHT_CORRECTNESS * tests
        + WEIGHT_HIT * hit
        + WEIGHT_MINIMAL * minimal
        + WEIGHT_SELF * self_part
        + WEIGHT_COST * cost
    )
    return Score(
        tests=tests,
        hit=hit,
        minimal=minimal,
        self_verified=self_part,
        cost=cost,
        total=round(total * 100, 1),
    )


def cheapest_tokens(values: Iterable[int]) -> int:
    """The reference cost for a task: the smallest positive run, or 0."""
    usable = [int(value) for value in values if int(value) > 0]
    return min(usable) if usable else 0
