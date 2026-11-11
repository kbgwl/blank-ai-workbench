"""代码审查发现上报，对齐 Claude 的 ReportFindings。

来源是 Claude Code CLI 里的工具定义（`ReportFindings`，只读、可并发、strict）：
一次调用提交整批**核验过的**发现，按严重程度从高到低排列，界面据此渲染成结构化
清单；工具回执只说条数，不把发现正文再贴一遍。四条例外只有一条：应用修复后的
重新上报要带 `outcome`，说明每条发现实际怎么处理了。

刻意不做的事：
- 不判断"该不该审查"——那由当前生效的代码审查说明（Skill / 用户指令）决定；
- 不替模型排序或过滤——顺序就是模型对严重程度的判断，静默重排等于改写结论；
- 不把发现写进工作区——这是**给用户看的报告**，不是落盘产物。
"""

from __future__ import annotations

from typing import Any

from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome, fail, ok

TOOL_NAME = "report_findings"
MAX_FINDINGS = 32
MAX_SHORT_SUMMARY = 60
MAX_CATEGORY = 40
LEVELS = ("low", "medium", "high", "xhigh", "max")
VERDICTS = ("CONFIRMED", "PLAUSIBLE")
OUTCOMES = ("fixed", "skipped", "no_change_needed")
REQUIRED_FIELDS = ("file", "summary", "failure_scenario")


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _invalid(index: int, detail: str) -> tuple[list[dict], str]:
    return [], f"第 {index} 条审查发现无效：{detail}"


def normalize_findings(raw: Any) -> tuple[list[dict], str | None]:
    """把模型给的发现压成固定形状；任何一条不合法就整批拒绝。

    整批拒绝而不是"能收几条收几条"：一半发现被静默丢掉，用户看到的报告就会
    比模型实际核验过的少，而界面没有任何地方能说明缺了什么。
    """
    if not isinstance(raw, list):
        return [], "report_findings 的 findings 必须是数组"
    if len(raw) > MAX_FINDINGS:
        return [], f"report_findings 一次最多上报 {MAX_FINDINGS} 条发现，收到 {len(raw)} 条"

    findings: list[dict] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            return _invalid(index, "必须是对象")

        values = {field: _text(item.get(field)) for field in REQUIRED_FIELDS}
        for field, value in values.items():
            if not value:
                return _invalid(index, f"缺少 {field}（或为空）")

        entry: dict[str, Any] = {
            "file": values["file"],
            "summary": values["summary"],
            "failure_scenario": values["failure_scenario"],
        }

        line = item.get("line")
        if line is not None:
            if isinstance(line, bool) or not isinstance(line, int) or line < 1:
                return _invalid(index, "line 必须是 1 起的整数，或省略")
            entry["line"] = line

        short_summary = item.get("short_summary")
        if short_summary is not None:
            if not isinstance(short_summary, str):
                return _invalid(index, "short_summary 必须是字符串")
            short_summary = short_summary.strip()
            if len(short_summary) > MAX_SHORT_SUMMARY:
                return _invalid(
                    index, f"short_summary 最多 {MAX_SHORT_SUMMARY} 字符，收到 {len(short_summary)}"
                )
            if short_summary:
                entry["short_summary"] = short_summary

        category = item.get("category")
        if category is not None:
            if not isinstance(category, str):
                return _invalid(index, "category 必须是字符串")
            category = category.strip()
            if len(category) > MAX_CATEGORY:
                return _invalid(
                    index, f"category 最多 {MAX_CATEGORY} 字符，收到 {len(category)}"
                )
            if category:
                entry["category"] = category

        verdict = item.get("verdict")
        if verdict is not None:
            if verdict not in VERDICTS:
                return _invalid(index, f"verdict 只能是 {'/'.join(VERDICTS)} 之一")
            entry["verdict"] = verdict

        outcome = item.get("outcome")
        if outcome is not None:
            if outcome not in OUTCOMES:
                return _invalid(index, f"outcome 只能是 {'/'.join(OUTCOMES)} 之一")
            entry["outcome"] = outcome

        findings.append(entry)
    return findings, None


def _level(raw: Any) -> tuple[str | None, str | None]:
    if raw is None:
        return None, None
    if not isinstance(raw, str) or raw not in LEVELS:
        return None, f"level 只能是 {'/'.join(LEVELS)} 之一，或省略"
    return raw, None


async def _record(context: ToolContext, event: str, payload: dict) -> None:
    recorder = getattr(context, "recorder", None)
    if recorder is not None:
        await recorder.add(event, "review", payload)


async def _report_findings(context: ToolContext, arguments: dict) -> ToolOutcome:
    findings, error = normalize_findings(arguments.get("findings"))
    if error is not None:
        return fail("error", error)
    level, level_error = _level(arguments.get("level"))
    if level_error is not None:
        return fail("error", level_error)

    count = len(findings)
    data: dict[str, Any] = {"count": count, "findings": findings}
    if level is not None:
        data["level"] = level

    # 事件是界面渲染这份清单的入口：工具回执只说条数，正文在 data/artifact 里。
    await _record(
        context,
        "review.findings",
        {"count": count, "level": level or "", "findings": findings},
    )
    output = "没有需要上报的审查发现。" if count == 0 else f"已上报 {count} 条审查发现。"
    return ok(output, **data)


def review_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name=TOOL_NAME,
            description=(
                "上报代码审查发现（对齐 Claude ReportFindings）：把核验过的发现按"
                "严重程度从高到低一次提交成结构化清单，交给界面渲染。"
                "只在当前生效的代码审查说明明确要求用本工具上报时使用；"
                "说明里指定了别的输出格式，就按说明来。"
                "reported once：一次调用提交整批发现；空数组表示没有发现通过核验。"
                "**不要**再把同一批发现以正文形式打印一遍——工具调用本身就是报告，"
                "也不要为审查另建文件或产物。"
                "应用修复后重新上报时（仅当修复说明要求），给每条发现填 outcome "
                "说明实际结果。verdict 只在做过复核时填写：CONFIRMED 或 PLAUSIBLE。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "level": {
                        "type": "string",
                        "enum": list(LEVELS),
                        "description": "这次审查投入的力度档位；没跑复核时可以省略",
                    },
                    "findings": {
                        "type": "array",
                        "maxItems": MAX_FINDINGS,
                        "description": "核验过的发现，最严重的排在最前；没有就传空数组",
                        "items": {
                            "type": "object",
                            "properties": {
                                "file": {
                                    "type": "string",
                                    "description": "发现所在的仓库相对路径",
                                },
                                "line": {
                                    "type": "integer",
                                    "description": "发现锚定的行号（从 1 开始）；无法定位时省略",
                                },
                                "summary": {
                                    "type": "string",
                                    "description": "一句话说明缺陷是什么",
                                },
                                "short_summary": {
                                    "type": "string",
                                    "maxLength": MAX_SHORT_SUMMARY,
                                    "description": (
                                        "紧凑界面的标签（≤60 字符）：只写结论本身，"
                                        "不要带理由或后果从句"
                                    ),
                                },
                                "failure_scenario": {
                                    "type": "string",
                                    "description": "具体的输入/状态 → 错误输出或崩溃",
                                },
                                "category": {
                                    "type": "string",
                                    "maxLength": MAX_CATEGORY,
                                    "description": (
                                        "短 kebab-case 类型，例如 correctness、simplification、"
                                        "efficiency、test-coverage"
                                    ),
                                },
                                "verdict": {
                                    "type": "string",
                                    "enum": list(VERDICTS),
                                    "description": "跑过复核时填写；只有内联审查时可以省略",
                                },
                                "outcome": {
                                    "type": "string",
                                    "enum": list(OUTCOMES),
                                    "description": (
                                        "仅在应用修复后重新上报时填写：这条发现实际怎么处理了"
                                    ),
                                },
                            },
                            "required": list(REQUIRED_FIELDS),
                        },
                    },
                },
                "required": ["findings"],
            },
            risk_level="low",
            handler=_report_findings,
        )
    ]


__all__ = [
    "LEVELS",
    "MAX_CATEGORY",
    "MAX_FINDINGS",
    "MAX_SHORT_SUMMARY",
    "OUTCOMES",
    "TOOL_NAME",
    "VERDICTS",
    "normalize_findings",
    "review_tools",
]
