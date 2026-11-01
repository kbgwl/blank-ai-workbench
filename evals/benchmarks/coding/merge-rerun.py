"""把 429 重跑的分片结果合并进 last-run.json 并按 runner 同一公式重算汇总。

用法：python merge-rerun.py <partial.json>
只覆盖 --only 重跑过的任务 id；summary 字段与 run.mjs 的口径逐项一致。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent


def main() -> None:
    partial_path = Path(sys.argv[1])
    base = json.loads((ROOT / "last-run.json").read_text(encoding="utf-8"))
    partial = json.loads(partial_path.read_text(encoding="utf-8"))

    redone = {r["id"]: r for r in partial["results"]}
    merged = [redone.get(r["id"], r) for r in base["results"]]

    total = len(merged)
    succeeded = sum(1 for r in merged if r["status"] == "succeeded")
    passed = sum(1 for r in merged if r.get("passed"))
    test_passed = sum(1 for r in merged if (r.get("checks") or {}).get("testPassed"))
    mis_edited = sum(len(r.get("unexpectedTouched") or []) for r in merged)
    tokens = sum(
        int(r.get("tokenUsage", {}).get("total_tokens") or r.get("tokenUsage", {}).get("total") or 0)
        for r in merged
    )
    steps = sum(int(r.get("stepsUsed") or 0) for r in merged)
    summary = {
        "total": total,
        "completionRate": f"{round(succeeded / total * 100)}%" if total else "n/a",
        "passRate": f"{round(passed / total * 100)}%" if total else "n/a",
        "testPassRate": f"{round(test_passed / total * 100)}%" if total else "n/a",
        "misEditedFiles": mis_edited,
        "avgElapsedMs": round(sum(r.get("elapsedMs") or 0 for r in merged) / total) if total else 0,
        "totalTokens": tokens,
        "totalSteps": steps,
        "falseDoneCount": sum(1 for r in merged if not (r.get("checks") or {}).get("noFalseDone")),
        "unauthorizedHighRisk": sum(1 for r in merged if not (r.get("checks") or {}).get("highRiskUnauthorized")),
    }
    (ROOT / "last-run.json").write_text(
        json.dumps({"summary": summary, "results": merged}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
