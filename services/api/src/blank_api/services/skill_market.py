"""技能市场适配器：从"别人的市场"里列技能、装技能。

为什么是适配器而不是"接一个市场"
------------------------------
世界上没有统一的技能市场格式，只有三种事实标准：

1. **Claude 插件市场**：仓库里有 `.claude-plugin/marketplace.json`，
   里面是 `plugins[]`，每项有 `name` / `description` / `source`（仓库内的相对目录）。
2. **GitHub 裸仓库**：没有清单，技能就是 `任意路径/SKILL.md` 的目录。
3. **单个 SKILL.md**：一个 raw 链接就是一份技能（没有脚本与模板）。

所以这里的做法是：**认这三种形状**，把它们的共同点（一个技能目录，
入口是 SKILL.md）映射成同一份 ``MarketItem``。新增一种来源只需要加一个
``_list_*`` 函数，不用动安装链路。

安装怎么拿到"一个目录"
----------------------
GitHub 的 raw 只能拿单文件，拿不到同目录的 scripts/templates。所以安装走
**仓库 zip + 子目录前缀**：`codeload.github.com/<repo>/zip/<ref>` 下载一次，
只解 `skills/<name>/` 这一支（``prefix`` 参数）。这样脚本、模板都跟着来，
而且只解一个技能，不会把整个仓库落到本地。

安全边界沿用 ``skill_install``：https-only、体积上限、zip 只解普通文件、
落盘前先解析、装完写来源戳。市场这一层**不做任何额外信任**——来源是别人写的，
越按不可信处理越好。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import httpx

# 只认这三种形状。
GITHUB = "github"
CLAUDE_MARKETPLACE = "claude-marketplace"
RAW_SKILL = "raw-skill"

# 允许抓取的域名。市场条目最终会变成"下载 + 解压到本机"，域名放开等于
# 让任意站点决定往磁盘写什么；这里只留三家与技能生态直接相关的。
ALLOWED_HOSTS = frozenset(
    {
        "github.com",
        "codeload.github.com",
        "raw.githubusercontent.com",
        "api.github.com",
        "objects.githubusercontent.com",
    }
)

FETCH_TIMEOUT = 20.0
MAX_INDEX_BYTES = 4 * 1024 * 1024
# 列表阶段最多为多少个条目补 description（每条要读一个 SKILL.md）。
MAX_DESCRIBED = 12

# 内置来源：只放"格式确定、公开可读"的。用户还能自己填任意 GitHub 仓库或
# 市场清单地址——那不是"收录"，是"适配"。
PRESET_SOURCES: tuple[dict, ...] = (
    {
        "id": "anthropic",
        "label": "Anthropic 官方技能库",
        "kind": GITHUB,
        "repo": "anthropics/skills",
        "ref": "main",
    },
)


@dataclass(frozen=True)
class MarketItem:
    """市场里的一条技能。``install_ref`` 是安装器要的参数，不在前端暴露成 URL 拼接。"""

    id: str
    name: str
    description: str
    source_id: str
    source_label: str
    # {"kind": "github"|"plugin-dir", "repo": ..., "ref": ..., "path": <仓库内目录>}
    install_ref: dict = field(default_factory=dict)


class MarketError(Exception):
    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def preset_sources() -> list[dict]:
    return [dict(item) for item in PRESET_SOURCES]


def _extra_hosts() -> set[str]:
    """自建镜像的域名白名单（``BLANK_MARKET_HOSTS=a.example.com,b.example.com``）。

    默认只信 GitHub：市场条目最终会变成"下载并解压到本机"，把域名放开等于让
    任意站点决定往磁盘写什么。要接自己的镜像，必须显式加进来。
    """
    import os

    raw = str(os.environ.get("BLANK_MARKET_HOSTS") or "")
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def _check_url(url: str) -> str:
    text = (url or "").strip()
    if not text.startswith(("http://", "https://")):
        raise MarketError("market_url_invalid", "只接受 http(s) 地址。")
    host = re.sub(r"^https?://", "", text).split("/", 1)[0].split(":", 1)[0].lower()
    if host not in ALLOWED_HOSTS and host not in _extra_hosts():
        raise MarketError(
            "market_host_blocked",
            f"不认识的域名「{host}」。市场只从 GitHub / 官方 raw 拉取，避免任意站点往本机写文件。",
        )
    return text


def _get(client: httpx.Client, url: str, *, accept: str = "*/*") -> bytes:
    try:
        response = client.get(_check_url(url), headers={"Accept": accept})
    except httpx.HTTPError as exc:
        raise MarketError("market_fetch_failed", f"拉取失败：{exc.__class__.__name__}") from exc
    if response.status_code >= 400:
        raise MarketError("market_fetch_failed", f"拉取失败：HTTP {response.status_code}")
    data = response.content
    if len(data) > MAX_INDEX_BYTES:
        raise MarketError("market_index_too_large", "索引文件超过体积上限。")
    return data


def _parse_frontmatter_summary(text: str) -> tuple[str, str]:
    """从 SKILL.md 里抠出 ``(display_name, description)``，读不出来就返回空串。

    这只是"给列表补一句话"，解析失败不该让整条列表挂掉，所以不抛异常。
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return "", ""
    name = description = ""
    for line in lines[1:]:
        if line.strip() in ("---", "..."):
            break
        key, _, value = line.partition(":")
        value = value.strip().strip('"').strip("'")
        if key.strip() == "name" and value:
            name = value
        elif key.strip() == "description" and value:
            description = value
    return name, description


def _github_tree(client: httpx.Client, repo: str, ref: str) -> list[tuple[str, str]]:
    """返回 ``[(skill_dir_path, skill_md_path)]``，直接来自 GitHub 的 tree 接口。"""
    url = f"https://api.github.com/repos/{repo}/git/trees/{ref}?recursive=1"
    payload = _get(client, url, accept="application/vnd.github+json")
    import json

    try:
        data = json.loads(payload.decode("utf-8", "replace"))
    except ValueError as exc:
        raise MarketError("market_index_bad", "GitHub 返回的不是 JSON。") from exc
    found: list[tuple[str, str]] = []
    for item in data.get("tree") or ():
        path = str(item.get("path") or "")
        if path.endswith("/SKILL.md") or path == "SKILL.md":
            directory = path[: -len("/SKILL.md")] if "/" in path else ""
            if directory:
                found.append((directory, path))
    return found


def _describe(client: httpx.Client, repo: str, ref: str, path: str) -> tuple[str, str]:
    url = f"https://raw.githubusercontent.com/{repo}/{ref}/{path}"
    try:
        text = _get(client, url).decode("utf-8", "replace")
    except MarketError:
        return "", ""
    return _parse_frontmatter_summary(text)


def list_github(
    repo: str,
    *,
    ref: str = "main",
    client: httpx.Client | None = None,
    source_id: str = "",
    source_label: str = "",
) -> list[MarketItem]:
    """列一个 GitHub 仓库里的所有技能（任意深度的 ``*/SKILL.md``）。"""
    own = client is None
    client = client or httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=True)
    try:
        dirs = _github_tree(client, repo, ref)
        items: list[MarketItem] = []
        for index, (directory, md_path) in enumerate(dirs):
            name = directory.rsplit("/", 1)[-1]
            display = description = ""
            if index < MAX_DESCRIBED:
                display, description = _describe(client, repo, ref, md_path)
            items.append(
                MarketItem(
                    id=directory,
                    name=display or name,
                    description=description,
                    source_id=source_id or repo,
                    source_label=source_label or repo,
                    install_ref={
                        "kind": GITHUB,
                        "repo": repo,
                        "ref": ref,
                        "path": directory,
                    },
                )
            )
        return items
    finally:
        if own:
            client.close()


def list_claude_marketplace(
    base_url: str,
    *,
    client: httpx.Client | None = None,
) -> list[MarketItem]:
    """列一个 Claude 插件市场清单（``.claude-plugin/marketplace.json``）。

    清单里的 ``source`` 可以是仓库内相对目录，也可以是一个 git 地址；这里只处理
    "同一个仓库里的目录"——跨仓库要额外解析 remote，先不猜。
    """
    own = client is None
    client = client or httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=True)
    try:
        url = f"{base_url.rstrip('/')}/.claude-plugin/marketplace.json"
        payload = _get(client, url)
    finally:
        if own:
            client.close()
    import json

    try:
        data = json.loads(payload.decode("utf-8", "replace"))
    except ValueError as exc:
        raise MarketError("market_index_bad", "市场清单不是合法 JSON。") from exc

    repo, ref = _repo_and_ref(base_url)
    items: list[MarketItem] = []
    for plugin in data.get("plugins") or ():
        name = str(plugin.get("name") or "").strip()
        source = plugin.get("source")
        if not name or not isinstance(source, str) or not source.strip():
            continue
        path = source.strip().lstrip("./").rstrip("/")
        items.append(
            MarketItem(
                id=name,
                name=name,
                description=str(plugin.get("description") or ""),
                source_id=base_url,
                source_label=name,
                install_ref={"kind": CLAUDE_MARKETPLACE, "repo": repo, "ref": ref, "path": path},
            )
        )
    return items


def _repo_and_ref(url: str) -> tuple[str, str]:
    """从 https://github.com/<owner>/<repo>(/tree/<ref>)? 里拆出 repo 与 ref。"""
    match = re.search(r"github\.com/([^/]+)/([^/?#]+)(?:/tree/([^/?#]+))?", url or "")
    if not match:
        raise MarketError("market_repo_unparsed", "从这个地址里读不出 owner/repo。")
    repo = f"{match.group(1)}/{match.group(2)}".removesuffix(".git")
    return repo, match.group(3) or "main"


async def list_market(source: str, *, query: str = "", client: httpx.Client | None = None) -> list[MarketItem]:
    """按来源列技能。``source`` 可以是预设 id、GitHub 仓库（owner/repo）或市场地址。"""
    text = (source or "").strip()
    preset = next((item for item in PRESET_SOURCES if item["id"] == text), None)
    if preset:
        repo, ref = preset["repo"], preset["ref"]
        items = list_github(repo, ref=ref, client=client, source_id=preset["id"], source_label=preset["label"])
    elif text.startswith("github:"):
        repo = text.split(":", 1)[1]
        items = list_github(repo, client=client)
    elif re.match(r"^[\w.-]+/[\w.-]+$", text):
        items = list_github(text, client=client)
    elif text.startswith(("http://", "https://")):
        items = list_claude_marketplace(text, client=client)
    else:
        raise MarketError("market_source_invalid", "来源要填 GitHub 仓库（owner/repo）或市场地址。")

    needle = query.strip().lower()
    if needle:
        items = [
            item
            for item in items
            if needle in item.name.lower() or needle in item.description.lower() or needle in item.id.lower()
        ]
    return items


def repo_zip_url(ref: dict) -> str:
    """把 install_ref 变成本地安装器认识的 zip 地址（GitHub 的 codeload）。"""
    repo = str(ref.get("repo") or "")
    branch = str(ref.get("ref") or "main")
    if not repo:
        raise MarketError("market_ref_invalid", "这条技能没有可下载的仓库地址。")
    return f"https://codeload.github.com/{repo}/zip/refs/heads/{branch}"


# ---------------------------------------------------------------------------
# 搜索：用户说要做的事，内置技能不覆盖时去 GitHub 找
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarketHit:
    """一条搜索结果。

    ``kind`` 区分两种东西：``skill`` 是能直接装进技能库的目录（有 SKILL.md），
    ``repo`` 是"这个仓库可能有用"——工具/库通常属于这一类，装不了技能，
    但可以 clone/装包，模型看到评分与说明自己决定。
    """

    kind: str
    name: str
    description: str
    repo: str
    path: str = ""
    stars: int = 0
    url: str = ""
    updated_at: str = ""
    source_label: str = "GitHub"

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "name": self.name,
            "description": self.description,
            "repo": self.repo,
            "path": self.path,
            "stars": self.stars,
            "url": self.url,
            "updated_at": self.updated_at,
            "source_label": self.source_label,
        }


def _repo_search(client: httpx.Client, query: str, limit: int) -> list[dict]:
    url = (
        "https://api.github.com/search/repositories"
        f"?q={quote(query)}&sort=stars&order=desc&per_page={max(1, min(limit, 20))}"
    )
    payload = _get(client, url, accept="application/vnd.github+json")
    import json

    try:
        data = json.loads(payload.decode("utf-8", "replace"))
    except ValueError as exc:
        raise MarketError("market_index_bad", "GitHub 搜索返回的不是 JSON。") from exc
    return list(data.get("items") or ())


def search_market(
    query: str,
    *,
    limit: int = 6,
    client: httpx.Client | None = None,
    per_repo_skills: int = 3,
) -> list[MarketHit]:
    """按一句话找技能与工具。

    先用仓库搜索拿"评分（stars）+ 说明 + 最近更新"，再对前几个仓库扫 SKILL.md：
    扫到就当技能，扫不到就当工具/库。**排序用 stars**，这是唯一能从 GitHub
    拿到的公开质量信号；不编"匹配度"。
    """
    text = (query or "").strip()
    if not text:
        raise MarketError("market_query_empty", "搜索词不能为空。")
    own = client is None
    client = client or httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=True)
    try:
        repos = _repo_search(client, f"{text} in:name,description,readme", limit)
        hits: list[MarketHit] = []
        for repo in repos:
            full = str(repo.get("full_name") or "")
            if not full:
                continue
            stars = int(repo.get("stargazers_count") or 0)
            updated = str(repo.get("pushed_at") or repo.get("updated_at") or "")
            url = str(repo.get("html_url") or f"https://github.com/{full}")
            description = str(repo.get("description") or "")
            branch = str(repo.get("default_branch") or "main")
            try:
                dirs = _github_tree(client, full, branch)
            except MarketError:
                # 单个仓库读不到（空仓库 / 超限）不该让整次搜索失败。
                dirs = []
            if not dirs:
                hits.append(
                    MarketHit(
                        kind="repo",
                        name=full,
                        description=description,
                        repo=full,
                        stars=stars,
                        url=url,
                        updated_at=updated,
                    )
                )
                continue
            for directory, md_path in dirs[:per_repo_skills]:
                display, skill_description = _describe(client, full, branch, md_path)
                hits.append(
                    MarketHit(
                        kind="skill",
                        name=display or directory.rsplit("/", 1)[-1],
                        description=skill_description or description,
                        repo=full,
                        path=directory,
                        stars=stars,
                        url=url,
                        updated_at=updated,
                    )
                )
        hits.sort(key=lambda item: item.stars, reverse=True)
        return hits
    finally:
        if own:
            client.close()


# ---------------------------------------------------------------------------
# 我们自己的市场：随包发布的精选清单（离线可读，不依赖网络）
# ---------------------------------------------------------------------------

REGISTRY_PATH = Path(__file__).resolve().parent.parent / "skills" / "registry.json"


def list_curated(path: Path | None = None) -> list[MarketItem]:
    """读我们自己维护的清单。

    条目分两类：``builtin``（随包技能，装都不用装）与 ``external``（精选的
    外部技能，带 stars 快照与核对日期）。快照的价值是**证据**：半年后 star 掉了
    或仓库搬了，能看出当初是凭什么推荐的。
    """
    target = path or REGISTRY_PATH
    if not target.is_file():
        return []
    import json

    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items: list[MarketItem] = []
    for row in data.get("external") or ():
        repo = str(row.get("repo") or "")
        skill_path = str(row.get("path") or "")
        if not repo or not skill_path:
            continue
        items.append(
            MarketItem(
                id=f"{repo}:{skill_path}",
                name=str(row.get("name") or skill_path.rsplit("/", 1)[-1]),
                description=str(row.get("why") or ""),
                source_id="curated",
                source_label="我们的市场",
                install_ref={"kind": GITHUB, "repo": repo, "ref": str(row.get("ref") or "main"), "path": skill_path},
            )
        )
    return items
