"""市场相关工具：让模型自己"去找"和"去装"。

分工刻意分开：

- ``find_skills`` 只读，随时可调：用户要做的事内置技能不覆盖时，去 GitHub 找
  技能或工具，把**评分（stars）、说明、仓库地址**一起带回来。没有评分的结果
  等于让模型凭标题猜质量，所以 stars 是必填输出的一部分。
- ``install_market_skill`` 写盘 + 外网下载，risk_level=high，会走审批卡：
  模型不能"顺手装一个东西"就落盘。

证据：装完写一条 ``skill.market.installed`` 事件（仓库、目录、stars、查询词），
用户事后在事件流里能查到"这个能力是哪来的"。
"""

from __future__ import annotations

from pathlib import Path

from blank_api.services.skill_install import SkillInstallError, install_skill
from blank_api.services.skill_market import MarketError, list_curated, repo_zip_url, search_market
from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome, fail, ok


def _render_hits(hits) -> str:
    if not hits:
        return "没有找到匹配的技能或工具。可以换更具体的关键词，或先把需求拆小一点。"
    lines = ["找到这些（按 GitHub stars 排序，评分是别人给的结果，不是你自己的判断）：", ""]
    for index, hit in enumerate(hits, start=1):
        kind = "技能" if hit.kind == "skill" else "仓库/工具"
        lines.append(f"{index}. [{kind}] {hit.name} — ★{hit.stars}")
        lines.append(f"   仓库：{hit.repo}{('  目录：' + hit.path) if hit.path else ''}")
        if hit.description:
            lines.append(f"   说明：{hit.description}")
        lines.append(f"   地址：{hit.url}")
        lines.append(f"   最近更新：{hit.updated_at or '未知'}")
    lines.append("")
    lines.append(
        "要用其中某一条技能，调用 install_market_skill 传 repo 与 path；"
        "只是工具/库就用 clone 或包管理器，别当技能装。"
    )
    return "\n".join(lines)


async def _find_skills(context: ToolContext, arguments: dict) -> ToolOutcome:
    query = str(arguments.get("query") or "").strip()
    if not query:
        return fail("error", "缺少 query：说明要做的事，或要解决的具体问题。")
    limit = int(arguments.get("limit") or 6)
    try:
        hits = search_market(query, limit=max(1, min(limit, 12)))
    except MarketError as exc:
        return fail("error", f"{exc.message}（本机可能连不上 GitHub）")
    return ok(_render_hits(hits), query=query, hits=[hit.as_dict() for hit in hits])


async def _install_market_skill(context: ToolContext, arguments: dict) -> ToolOutcome:
    repo = str(arguments.get("repo") or "").strip()
    path = str(arguments.get("path") or "").strip()
    ref = str(arguments.get("ref") or "main").strip() or "main"
    if not repo or "/" not in repo:
        return fail("error", "repo 要写成 owner/repo（先调 find_skills 拿到准确值，不要猜）。")
    if not path:
        return fail("error", "path 是仓库里的技能目录（find_skills 结果里有），不能为空。")

    scope = str(arguments.get("scope") or "project").strip()
    root = Path(context.root)
    try:
        result = install_skill(
            repo_zip_url({"repo": repo, "ref": ref}),
            scope="project" if scope == "project" else "user",
            project_root=root if scope == "project" else None,
            name=str(arguments.get("name") or ""),
            overwrite=bool(arguments.get("overwrite", False)),
            prefix=path,
        )
    except SkillInstallError as exc:
        return fail("error", exc.message)
    except MarketError as exc:
        return fail("error", exc.message)

    recorder = getattr(context, "recorder", None)
    if recorder is not None:
        await recorder.add(
            "skill.market.installed",
            "skill",
            {
                "repo": repo,
                "path": path,
                "stars": int(arguments.get("stars") or 0),
                "url": f"https://github.com/{repo}",
                "installed_as": result["name"],
                "scope": scope,
            },
        )
    return ok(
        f"已安装技能「{result['name']}」到 {result['path']}。"
        f"来源：{repo} 的 {path}。下一轮它会出现在技能清单里，按名字调用即可。",
        name=result["name"],
        path=result["path"],
        repo=repo,
    )


async def _curated_skills(context: ToolContext, arguments: dict) -> ToolOutcome:
    """我们自己的市场清单：离线可读，带 stars 快照与核对日期。"""
    items = list_curated()
    if not items:
        return ok("我们的市场里暂时没有精选条目（随包技能本身不需要安装）。")
    lines = ["我们的市场（人工筛选，附 stars 快照）：", ""]
    for item in items:
        lines.append(f"- {item.name}：{item.description}")
        lines.append(f"  {item.install_ref.get('repo')}  {item.install_ref.get('path')}")
    return ok("\n".join(lines), count=len(items))


def skill_market_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="find_skills",
            description=(
                "去 GitHub 找技能或工具。当用户要做的事没有现成技能覆盖、"
                "或需要某个领域的具体做法时使用。返回候选的 stars（评分）、说明、仓库地址；"
                "只读，不会下载或安装。找不到就如实说没找到，不要自己编一个技能名。"
            ),
            input_schema={
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string", "description": "要做的事或要解决的问题，用关键词"},
                    "limit": {"type": "integer", "description": "返回条数，默认 6，上限 12"},
                },
            },
            risk_level="low",
            handler=_find_skills,
            timeout_seconds=60.0,
        ),
        ToolDefinition(
            name="install_market_skill",
            description=(
                "把市场里的一条技能装到本机（会下载仓库并写盘，需要用户批准）。"
                "repo 与 path 必须来自 find_skills 的结果，不要猜；"
                "只是工具/库不要用这个工具，那应该用 clone 或包管理器。"
            ),
            input_schema={
                "type": "object",
                "required": ["repo", "path"],
                "properties": {
                    "repo": {"type": "string", "description": "owner/repo"},
                    "path": {"type": "string", "description": "仓库里的技能目录"},
                    "ref": {"type": "string", "description": "分支或 tag，默认 main"},
                    "name": {"type": "string", "description": "装成什么名字，默认取目录名"},
                    "scope": {"type": "string", "enum": ["project", "user"], "description": "装到项目还是我的，默认 project"},
                    "overwrite": {"type": "boolean", "description": "同名时是否覆盖"},
                    "stars": {"type": "integer", "description": "搜索结果里的 stars，用于记账"},
                },
            },
            risk_level="high",
            handler=_install_market_skill,
            timeout_seconds=120.0,
        ),
        ToolDefinition(
            name="list_curated_skills",
            description="列出我们自己维护的精选技能清单（离线可读，不需要网络）。",
            input_schema={"type": "object", "properties": {}},
            risk_level="low",
            handler=_curated_skills,
            timeout_seconds=10.0,
        ),
    ]
