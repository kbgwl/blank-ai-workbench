"""本地市场镜像：先把目录与包拉到本机，之后列表与安装都不再依赖网络。

为什么要有这一层
----------------
之前的市场是"点一下就去 GitHub 拉"，于是：网络不通就什么都没有；同一个仓库
每次都要重新下载；GitHub 限流还会让列表莫名变空。

做法是**本机就是服务器**：
1. 目录（``catalog.json``）同步到 ``~/.blank/market``，列表只读本地；
2. 技能包（zip）按需下载并**缓存**到 ``packages/``，第二次安装不再联网；
3. 搜索优先 GitHub，失败时回落到本地目录，并标明"来自快照 + 同步日期"。

这样"网络问题"只在**同步那一步**存在，而且失败不会把已有能力弄丢。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx

from blank_api.services.skill_market import MarketError, _check_url

MAX_PACKAGE_BYTES = 16 * 1024 * 1024
FETCH_TIMEOUT = 30.0
CATALOG_VERSION = 1


def market_home(env: dict[str, str] | None = None) -> Path:
    env = env if env is not None else os.environ
    override = str(env.get("BLANK_MARKET_DIR") or "").strip()
    return Path(override).expanduser() if override else Path.home() / ".blank" / "market"


def catalog_path(env: dict[str, str] | None = None) -> Path:
    return market_home(env) / "catalog.json"


def packages_dir(env: dict[str, str] | None = None) -> Path:
    return market_home(env) / "packages"


@dataclass(frozen=True)
class CatalogEntry:
    """目录里的一条。``package`` 是**在我们服务器上的 zip 相对路径**（可空）。

    有 package 时安装走本地/缓存（不碰 GitHub）；没有时退回仓库 zip（要联网）。
    """

    key: str
    name: str
    repo: str
    path: str
    ref: str = "main"
    stars: int = 0
    summary: str = ""
    tags: tuple[str, ...] = ()
    package: str = ""
    checked_at: str = ""

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "repo": self.repo,
            "path": self.path,
            "ref": self.ref,
            "stars": self.stars,
            "summary": self.summary,
            "tags": list(self.tags),
            "package": self.package,
            "checked_at": self.checked_at,
        }


def read_catalog(env: dict[str, str] | None = None) -> dict:
    """读本地目录。没有或读坏时返回空目录（列表接口不该因此报错）。"""
    path = catalog_path(env)
    if not path.is_file():
        return {"version": CATALOG_VERSION, "synced_at": "", "source": "", "entries": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": CATALOG_VERSION, "synced_at": "", "source": "", "entries": []}
    rows = []
    for row in data.get("entries") or ():
        if not isinstance(row, dict):
            continue
        repo = str(row.get("repo") or "")
        skill_path = str(row.get("path") or "")
        if not repo or not skill_path:
            continue
        rows.append(
            CatalogEntry(
                key=f"{repo}:{skill_path}",
                name=str(row.get("name") or skill_path.rsplit("/", 1)[-1]),
                repo=repo,
                path=skill_path,
                ref=str(row.get("ref") or "main"),
                stars=int(row.get("stars") or 0),
                summary=str(row.get("summary") or ""),
                tags=tuple(str(tag) for tag in (row.get("tags") or ())),
                package=str(row.get("package") or ""),
                checked_at=str(row.get("checked_at") or ""),
            )
        )
    return {
        "version": int(data.get("version") or CATALOG_VERSION),
        "synced_at": str(data.get("synced_at") or ""),
        "source": str(data.get("source") or ""),
        "entries": rows,
    }


def write_catalog(payload: dict, *, source: str = "local", env: dict[str, str] | None = None) -> dict:
    """把一份目录写进本地。字段做最小校验，不合法条目直接跳过（不让一条坏数据废掉整份目录）。"""
    if not isinstance(payload, dict):
        raise MarketError("market_catalog_bad", "目录内容必须是 JSON 对象。")
    rows = payload.get("entries")
    if not isinstance(rows, list) or not rows:
        raise MarketError("market_catalog_empty", "目录里没有任何条目。")

    clean: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        repo = str(row.get("repo") or "").strip()
        skill_path = str(row.get("path") or "").strip()
        if "/" not in repo or not skill_path:
            continue
        clean.append(
            {
                "name": str(row.get("name") or skill_path.rsplit("/", 1)[-1]),
                "repo": repo,
                "path": skill_path,
                "ref": str(row.get("ref") or "main"),
                "stars": int(row.get("stars") or 0),
                "summary": str(row.get("summary") or ""),
                "tags": [str(tag) for tag in (row.get("tags") or ())],
                "package": str(row.get("package") or ""),
                "checked_at": str(row.get("checked_at") or ""),
            }
        )
    if not clean:
        raise MarketError("market_catalog_empty", "目录里的条目都缺 repo / path，没有可用数据。")

    stamp = datetime.now().isoformat(sep=" ", timespec="seconds")
    payload = {"version": CATALOG_VERSION, "synced_at": stamp, "source": source, "entries": clean}
    path = catalog_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 先写临时文件再替换：同步中断时不会留下半份目录。
    staging = path.with_suffix(".json.tmp")
    staging.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    staging.replace(path)
    return read_catalog(env)


async def sync_from_url(url: str, *, client: httpx.Client | None = None) -> tuple[dict, str]:
    """从镜像地址拉一份目录 JSON，返回 ``(payload, source)``。

    只接受 https，且域名必须在白名单里（GitHub 或 ``BLANK_MARKET_HOSTS``）——
    目录本身也会被写进本机并驱动"下载并解压"，所以它和安装包是同一个信任级别。
    """
    text = (url or "").strip()
    if not text:
        raise MarketError("market_url_invalid", "请填目录地址。")
    checked = _check_url(text)
    own = client is None
    client = client or httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=True)
    try:
        try:
            response = client.get(checked)
        except httpx.HTTPError as exc:
            raise MarketError("market_fetch_failed", f"拉取目录失败：{exc.__class__.__name__}") from exc
        if response.status_code >= 400:
            raise MarketError("market_fetch_failed", f"拉取目录失败：HTTP {response.status_code}")
        data = response.content
    finally:
        if own:
            client.close()
    if len(data) > 4 * 1024 * 1024:
        raise MarketError("market_catalog_too_large", "目录文件超过体积上限。")
    import json

    try:
        payload = json.loads(data.decode("utf-8", "replace"))
    except ValueError as exc:
        raise MarketError("market_catalog_bad", "目录不是合法 JSON。") from exc
    return payload, checked


def search_catalog(query: str, *, limit: int = 8, env: dict[str, str] | None = None) -> list[CatalogEntry]:
    """本地搜索：名字、说明、标签、repo 都参与，空查询返回前 limit 条。"""
    data = read_catalog(env)
    needle = (query or "").strip().lower()
    rows: list[CatalogEntry] = list(data["entries"])
    if needle:
        rows = [
            row
            for row in rows
            if needle in row.name.lower()
            or needle in row.summary.lower()
            or needle in row.repo.lower()
            or any(needle in tag.lower() for tag in row.tags)
        ]
    rows.sort(key=lambda row: row.stars, reverse=True)
    return rows[: max(1, min(limit, 50))]


def _safe_local_package(relative: str, env: dict[str, str] | None = None) -> Path:
    root = packages_dir(env).resolve()
    target = (root / relative).resolve()
    if root not in target.parents:
        raise MarketError("market_package_escape", "包路径跑到缓存目录之外了，已拒绝。")
    return target


def ensure_package(entry: CatalogEntry, *, client: httpx.Client | None = None, env: dict[str, str] | None = None) -> Path:
    """保证这个技能的 zip 在本机，返回本地路径。

    - 已缓存 → 直接返回（离线可用）；
    - ``package`` 是 http(s) → 下载到缓存（下次不再联网）；
    - 没有 package → 报错，由调用方退回"仓库 zip 直连"。
    """
    if not entry.package:
        raise MarketError("market_package_missing", "这条目没有镜像包，只能回退到仓库直连。")

    if entry.package.startswith(("http://", "https://")):
        digest = hashlib.sha256(entry.key.encode("utf-8")).hexdigest()[:16]
        target = _safe_local_package(f"{digest}.zip", env)
        if target.is_file():
            return target
        url = _check_url(entry.package)
        own = client is None
        client = client or httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=True)
        try:
            try:
                response = client.get(url)
            except httpx.HTTPError as exc:
                raise MarketError("market_package_failed", f"下载安装包失败：{exc.__class__.__name__}") from exc
            if response.status_code >= 400:
                raise MarketError("market_package_failed", f"下载安装包失败：HTTP {response.status_code}")
            data = response.content
        finally:
            if own:
                client.close()
        if len(data) > MAX_PACKAGE_BYTES:
            raise MarketError("market_package_too_large", "安装包超过体积上限。")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    # 相对路径形态只允许落在缓存目录内（自建镜像可以放在同一块盘上）。
    target = _safe_local_package(entry.package, env)
    if not target.is_file():
        raise MarketError("market_package_missing", f"本地没有这个安装包：{entry.package}")
    return target
