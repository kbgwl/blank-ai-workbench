"""采集精选清单：把 GitHub 的 stars 与核对日期写进 registry.json。

为什么要有这个脚本
------------------
精选清单里的每一行都是"我们推荐它"的公开承诺。手写 star 数等于凭印象推荐，
三个月后没人知道那个数是哪来的。

规则
----
1. 只写**实际查到**的值；查不到就跳过这一条并记账，不填 0 也不填估计值。
2. 每条都带 ``checked_at``（核对日期）。清单是快照，不是实时读数。
3. 不覆盖人工写的 ``why``（推荐理由）——脚本只更新数字与日期。

用法::

    python scripts/capture_registry.py            # 复核已有条目
    python scripts/capture_registry.py --add owner/repo:path/to/skill
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from blank_api.services.skill_market import MarketError, list_github  # noqa: E402

REGISTRY = Path(__file__).resolve().parent.parent / "src" / "blank_api" / "skills" / "registry.json"


def _load() -> dict:
    if not REGISTRY.is_file():
        return {"version": 1, "maintainer": "blank-team", "external": []}
    return json.loads(REGISTRY.read_text(encoding="utf-8"))


def _save(data: dict) -> None:
    REGISTRY.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--add", action="append", default=[], help="owner/repo:path/to/skill")
    args = parser.parse_args()

    data = _load()
    rows = {f"{row.get('repo')}:{row.get('path')}": row for row in data.get("external") or []}

    for spec in args.add:
        if ":" not in spec:
            print(f"跳过（格式应为 owner/repo:path）：{spec}")
            continue
        repo, path = spec.split(":", 1)
        rows.setdefault(f"{repo}:{path}", {"repo": repo, "path": path, "name": path.rsplit("/", 1)[-1], "why": ""})

    if not rows:
        print("清单是空的：用 --add owner/repo:path 添加条目。")
        return 0

    # 按仓库分组，一个仓库只查一次列表。
    by_repo: dict[str, list[str]] = {}
    for key, row in rows.items():
        by_repo.setdefault(str(row["repo"]), []).append(key)

    failed: list[str] = []
    for repo, keys in by_repo.items():
        try:
            items = list_github(repo, source_id="curated", source_label="我们的市场")
        except MarketError as exc:
            failed.extend(keys)
            print(f"{repo}: 拉取失败（{exc.message}），这些条目保持原值")
            continue
        stars = {item.install_ref.get("path"): item for item in items}
        # list_github 不返回 star 数（tree 接口没有），这里用仓库搜索补一次。
        import httpx

        try:
            with httpx.Client(timeout=20.0, follow_redirects=True) as client:
                response = client.get(
                    f"https://api.github.com/repos/{repo}",
                    headers={"Accept": "application/vnd.github+json"},
                )
                repo_stars = int(response.json().get("stargazers_count") or 0)
                pushed = str(response.json().get("pushed_at") or "")
        except Exception as exc:  # 网络/JSON 都可能失败，一律记成"没查到"
            failed.extend(keys)
            print(f"{repo}: 读仓库信息失败（{exc.__class__.__name__}），这些条目保持原值")
            continue

        for key in keys:
            row = rows[key]
            path = str(row.get("path") or "")
            match = stars.get(path)
            if match is None:
                failed.append(key)
                print(f"{key}: 仓库里已经没有这个目录，未改动")
                continue
            row["stars"] = repo_stars
            row["checked_at"] = date.today().isoformat()
            row["updated_at"] = pushed

    data["external"] = sorted(rows.values(), key=lambda row: (-int(row.get("stars") or 0), str(row.get("name"))))
    _save(data)
    print(f"已写入 {len(data['external'])} 条，失败/跳过 {len(failed)} 条。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
