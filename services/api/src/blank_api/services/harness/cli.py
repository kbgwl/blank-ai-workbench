"""CLI for validating a harness, optimizing one, and browsing the library.

    python -m blank_api.services.harness.cli validate
    python -m blank_api.services.harness.cli library --domain 解析器
    python -m blank_api.services.harness.cli run --only T01,T02 --k 3

The ``run`` subcommand talks to a live Blank API. It rebuilds every fixture from
``evals/task-samples.json`` and checks the work itself; nothing is trusted just
because the agent said so.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from blank_api.services.harness import spec
from blank_api.services.harness.adapter import BlankEvalAdapter, HttpApi
from blank_api.services.harness.library import Library
from blank_api.services.harness.optimize import optimize
from blank_api.services.harness.proposer import (
    ChatProposer,
    ReflectionProposer,
    RuleProposer,
)


def _default_api() -> str:
    base = str(os.environ.get("BLANK_API_BASE") or "").rstrip("/")
    if not base:
        return "http://127.0.0.1:8000/api/v1"
    return base if base.endswith("/api/v1") else f"{base}/api/v1"


def _load_samples(path: Path, *, only: str, category: str) -> tuple[list[dict], str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    fixtures = payload.get("fixtures") or {}
    samples = list(payload.get("samples") or [])
    wanted = {item.strip() for item in only.split(",") if item.strip()}
    if wanted:
        samples = [item for item in samples if str(item.get("id")) in wanted]
    if category:
        samples = [item for item in samples if str(item.get("category") or "") == category]
    if not samples:
        raise ValueError("没有匹配的样例，检查 --only / --category")

    resolved: list[dict] = []
    for item in samples:
        sample = dict(item)
        fixture = sample.get("fixture")
        files = fixtures.get(fixture) if isinstance(fixture, str) else fixture
        if not isinstance(files, dict):
            raise ValueError(f"样例 {sample.get('id')} 的 fixture {fixture!r} 不存在")
        sample["files"] = files
        resolved.append(sample)
    return resolved, str(path)


def _domain_of(samples: list[dict], override: str) -> str:
    if override:
        return override
    categories = sorted({str(item.get("category") or "") for item in samples if item.get("category")})
    return "、".join(categories) or "未标注"


def _write_report(path: str, payload: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


async def _cmd_validate(args: argparse.Namespace) -> int:
    directory = (
        Path(args.path)
        if args.path
        else Path(args.workspace) / spec.HARNESS_DIR
    )
    try:
        tree = spec.read_tree(directory, strict=args.strict)
    except (spec.HarnessError, OSError) as exc:
        print(f"读取失败：{exc}", file=sys.stderr)
        return 2
    if not tree:
        print(f"{directory}：没有 harness 组件（这是合法状态）")
        return 0
    if args.json:
        payload = {
            "path": str(directory),
            "files": [
                {
                    "path": rel,
                    "chars": len(body),
                    "component": spec.component_for(rel).key if spec.component_for(rel) else "?",
                }
                for rel, body in sorted(tree.items())
            ],
        }
        try:
            spec.validate(tree)
            payload["ok"] = True
        except spec.HarnessError as exc:
            payload["ok"] = False
            payload["problems"] = str(exc).splitlines()
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload["ok"] else 2

    for rel, body in sorted(tree.items()):
        component = spec.component_for(rel)
        label = f"{component.key} {component.label}" if component else "?"
        print(f"- {rel}: {len(body):,} 字符（{label}）")
    try:
        spec.validate(tree)
    except spec.HarnessError as exc:
        print(f"校验失败：\n{exc}", file=sys.stderr)
        return 2
    print("校验通过")
    return 0


async def _cmd_library(args: argparse.Namespace) -> int:
    library = Library(args.root, args.domain)
    if not library.exists():
        print(f"经验库为空：{library.dir}")
        return 0
    if args.json:
        payload = {
            "domain": library.domain,
            "dir": str(library.dir),
            "entries": library.entries(),
            "lessons": library.lessons(),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(library.context(limit=args.limit))
    return 0


async def _cmd_run(args: argparse.Namespace) -> int:
    samples_path = Path(args.samples)
    try:
        samples, _ = _load_samples(samples_path, only=args.only, category=args.category)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"读取样例失败：{exc}", file=sys.stderr)
        return 2

    regression: list[dict] = []
    if args.regression_only or args.regression_category or args.regression_samples:
        regression_path = Path(args.regression_samples or args.samples)
        try:
            regression, _ = _load_samples(
                regression_path,
                only=args.regression_only,
                category=args.regression_category,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"读取回归样例失败：{exc}", file=sys.stderr)
            return 2
        primary_ids = {str(item.get("id")) for item in samples}
        regression = [
            item for item in regression if str(item.get("id")) not in primary_ids
        ]
        if not regression:
            print("回归样例与主样例重复或为空", file=sys.stderr)
            return 2

    domain = _domain_of(samples, args.domain)
    base_tree: dict[str, str] = {}
    if args.base:
        try:
            base_tree = spec.read_tree(Path(args.base), strict=True)
        except (spec.HarnessError, OSError) as exc:
            print(f"读取基准 harness 失败：{exc}", file=sys.stderr)
            return 2

    library = Library(args.library, domain)
    api = HttpApi(args.api, timeout=max(120.0, args.timeout + 60.0))
    adapter = BlankEvalAdapter(
        api,
        root=args.workdir,
        provider_id=args.provider,
        python_executable=args.python,
        node_executable=args.node,
        poll_interval=args.poll,
        timeout=args.timeout,
        cleanup=args.cleanup,
        keep_workspace=args.keep_workspace,
        baseline_verify=args.baseline_verify,
    )

    rules = RuleProposer()
    proposer: Any = rules

    async def chat(prompt: str) -> str:
        reply = await api.request(
            "POST",
            f"/providers/{args.provider}/chat",
            {"message": prompt, "stream": False},
        )
        return str((reply or {}).get("text") or "")

    if args.model_proposer:
        if not args.provider:
            print("--model-proposer 需要同时给 --provider", file=sys.stderr)
            await api.close()
            return 2
        proposer = ChatProposer(chat, fallback=rules)

    reflector = None
    if args.reflect:
        if not args.provider:
            print("--reflect 需要同时给 --provider", file=sys.stderr)
            await api.close()
            return 2
        reflector = ReflectionProposer(chat)

    try:
        report = await optimize(
            samples,
            runner=adapter,
            proposer=proposer,
            reflector=reflector,
            library=library,
            domain=domain,
            base_tree=base_tree,
            regression_samples=regression,
            count=args.k,
            min_gain=args.min_gain,
            max_regression_drop=args.max_regression_drop,
            max_token_growth=args.max_token_growth,
            reflect_rounds=args.reflect_rounds,
            max_tokens=args.max_tokens,
            max_seconds=args.max_seconds,
            adopt=not args.no_adopt,
            run_id=args.run_id or "",
        )
    except Exception as exc:
        print(f"优化失败：{exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        await api.close()

    payload = report.as_dict()
    if args.report:
        _write_report(args.report, payload)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(report.render())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="blank-harness",
        description="校验、优化和浏览项目 harness（c1-c7）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="校验一棵 harness 树")
    validate.add_argument("path", nargs="?", default="", help="harness 目录；默认是 <workspace>/.blank/harness")
    validate.add_argument("--workspace", default=".", help="工作区根目录")
    validate.add_argument("--strict", action="store_true", help="目录里出现未知文件也算错误")
    validate.add_argument("--json", action="store_true")
    validate.set_defaults(handler=_cmd_validate)

    run = sub.add_parser("run", help="跑一组样例，propose/probe/compose 后按需写入经验库")
    run.add_argument("--samples", default="evals/task-samples.json")
    run.add_argument("--only", default="", help="逗号分隔的样例 id，例如 T01,T02")
    run.add_argument("--category", default="", help="只跑某一类任务")
    run.add_argument("--api", default=_default_api())
    run.add_argument("--provider", default="", help="任务使用的模型供应商 id")
    run.add_argument("--workdir", default="_verify/harness_work")
    run.add_argument("--library", default="_verify/harness_library")
    run.add_argument("--base", default="", help="当前已在用的 harness 目录")
    run.add_argument("--domain", default="", help="经验库领域名；默认取样例 category")
    run.add_argument("--regression-samples", default="", help="回归样例文件；默认与 --samples 相同")
    run.add_argument("--regression-only", default="", help="逗号分隔的回归样例 id")
    run.add_argument("--regression-category", default="", help="只把某一类旧任务作为回归集")
    run.add_argument("--k", type=int, default=3, help="候选数量")
    run.add_argument("--min-gain", type=float, default=0.5, help="采纳所需的最小均分增益")
    run.add_argument(
        "--max-regression-drop",
        type=float,
        default=0.0,
        help="回归集允许的最大掉分；默认 0，掉分即拒绝",
    )
    run.add_argument(
        "--max-token-growth",
        type=float,
        default=1.0,
        help="候选相对基线的 token 增长上限；默认 1.0 即最多 2×",
    )
    run.add_argument("--max-tokens", type=int, default=2_000_000)
    run.add_argument("--max-seconds", type=float, default=3_600.0)
    run.add_argument("--timeout", type=float, default=420.0, help="单个样例的超时秒数")
    run.add_argument("--poll", type=float, default=1.5)
    run.add_argument("--python", default=sys.executable, help="验证命令里的 python 替换成它")
    run.add_argument("--node", default="", help="验证命令里的 node 替换成它")
    run.add_argument("--cleanup", action="store_true", help="跑完删除远端项目记录")
    run.add_argument("--keep-workspace", action="store_true", help="保留工作区用于排查")
    run.add_argument("--baseline-verify", action="store_true", help="运行前先记录一次验证结果")
    run.add_argument("--model-proposer", action="store_true", help="让模型提出候选，失败时回退到规则")
    run.add_argument(
        "--reflect",
        action="store_true",
        help="首轮测量后，用失败轨迹让模型再提一轮候选（需要 --provider）",
    )
    run.add_argument("--reflect-rounds", type=int, default=1, help="反思轮数")
    run.add_argument("--no-adopt", action="store_true", help="只测量，不写经验库")
    run.add_argument("--report", default="", help="把完整报告写到这个 JSON 文件")
    run.add_argument("--run-id", default="")
    run.add_argument("--json", action="store_true")
    run.set_defaults(handler=_cmd_run)

    library = sub.add_parser("library", help="查看某个领域的经验库")
    library.add_argument("--root", default="_verify/harness_library")
    library.add_argument("--domain", required=True)
    library.add_argument("--limit", type=int, default=12_000)
    library.add_argument("--json", action="store_true")
    library.set_defaults(handler=_cmd_library)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return asyncio.run(args.handler(args))
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
