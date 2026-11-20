"""技能安装：把一份 SKILL.md（或包含它的目录 / zip）装到用户级或项目级目录。

为什么要自己写而不用 `git clone`
--------------------------------
技能目录只是一个 `SKILL.md` 加可选脚本，装它不需要版本库历史。更关键的是
**安全边界**：这是一个"从外部拿内容写进本机磁盘"的入口，必须能说清每一条限制。

硬规则（每一条都有对应测试）
--------------------------
1. **只能写到两个根里**：用户级 `~/.blank/skills`、项目级 `<工作区>/.blank/skills`。
   目标路径 resolve 之后必须仍在根内（防 `../` 与符号链接逃逸）。
2. **名字只认小写与连字符**：`^[a-z0-9][a-z0-9._-]{0,63}$`，且不能是保留目录名
   （`synced` 由同步层所有）。名字决定目录名，不接受任何路径分隔符。
3. **同名不覆盖**：默认 409，必须显式 `overwrite=true`。覆盖是"替换目录"，
   不做合并——半新半旧的技能比装错更难查。
4. **体积与数量上限**：单文件 1 MiB、总量 8 MiB、最多 60 个文件。超了就整体失败，
   不装一半。
5. **zip 只解普通文件**：拒绝绝对路径、`..`、符号链接；解压前逐条校验。
6. **落盘前先解析一遍**：`SKILL.md` 必须能被 `load_skill` 解析出正文，否则不装。
   装进去一份解析不了的技能，等于在清单里放一个永远加载失败的名字。
7. **记来源**：装完写 `.skill-source.json`（来源地址、时间、文件数）。
   没有它，半年后没人知道这份技能是哪来的、能不能删。

不做的事
--------
- 不执行技能里的任何脚本（安装只写文件）。
- 不跟随重定向之外的跳转链做"猜仓库结构"；源必须是 `.md`、`.zip` 或本地目录。
"""

from __future__ import annotations

import io
import json
import re
import shutil
import tempfile
import zipfile
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import httpx

from blank_api.services.skill_scan import scan_skill_dir, scan_summary
from blank_api.services.skills_defs import (
    MAX_BODY_CHARS,
    PROJECT_SKILLS_DIR,
    RESERVED_DIRS,
    SkillDefinition,
    load_skill,
    user_skills_dir,
)

# 单文件 / 总体 / 数量上限：技能是说明文本与少量脚本，超过这个量级基本是装错了东西。
MAX_FILE_BYTES = 1_048_576
MAX_TOTAL_BYTES = 8 * 1_048_576
MAX_FILES = 60
# 远端抓取上限：比解压后上限略宽，避免"压缩包刚好卡在边界"被误杀。
MAX_DOWNLOAD_BYTES = MAX_TOTAL_BYTES
FETCH_TIMEOUT = 20.0

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SKIP_DIRS = frozenset({".git", "__pycache__", "node_modules", ".venv", "venv"})
SOURCE_FILE = ".skill-source.json"


class SkillInstallError(Exception):
    """安装失败的统一出口：``code`` 给前端分辨，``message`` 给人看。"""

    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def skills_root(scope: str, *, project_root: str | Path | None = None) -> Path:
    """安装目标根。只有两个合法取值，别的一律拒绝。"""
    if scope == "user":
        return user_skills_dir()
    if scope == "project":
        if not project_root:
            raise SkillInstallError(
                "skill_project_root_missing",
                "装到项目里需要先绑定一个工作目录：先在对话页选一个目录，或改成安装到「我的」。",
            )
        return Path(project_root) / PROJECT_SKILLS_DIR
    raise SkillInstallError("skill_scope_invalid", f"安装范围只能是 user 或 project，收到「{scope}」。")


def slugify_name(raw: str) -> str:
    """把来源名压成合法目录名；压不出合法名字时抛错，不猜。"""
    text = str(raw or "").strip().lower()
    # 去掉扩展名与常见压缩后缀：`my-skill.zip`、`SKILL.md` 都不该整串当名字。
    for suffix in (".zip", ".md", ".tar.gz"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    text = re.sub(r"[^a-z0-9._-]+", "-", text).strip("-._")
    if text in ("skill", "skills", ""):
        return ""
    return text[:64]


def _check_name(name: str) -> str:
    if not NAME_RE.match(name or ""):
        raise SkillInstallError(
            "skill_name_invalid",
            f"技能名「{name}」不合法：只能用小写字母、数字、点、下划线与连字符，最多 64 个字符。",
        )
    if name in RESERVED_DIRS:
        raise SkillInstallError("skill_name_reserved", f"「{name}」是保留目录名，不能作为技能名。")
    return name


def _ensure_inside(target: Path, root: Path) -> Path:
    """目标必须在根内。`resolve()` 之后比前缀，才能挡住 `../` 与符号链接。"""
    resolved_root = root.resolve()
    resolved = target.resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise SkillInstallError("skill_path_escape", "目标路径跑到技能目录之外了，已拒绝。")
    return resolved


def _walk_files(base: Path) -> list[Path]:
    """列出要复制的文件，跳过版本库与依赖目录。"""
    files: list[Path] = []
    for item in sorted(base.rglob("*")):
        if any(part in SKIP_DIRS for part in item.parts):
            continue
        if item.is_symlink():
            # 符号链接可以指到任意位置，复制它等于把外面的文件带进技能目录。
            continue
        if item.is_file():
            files.append(item)
    return files


def _copy_tree(source: Path, staged: Path) -> None:
    """把来源目录复制进暂存目录，同时执行体积与数量上限。"""
    files = _walk_files(source)
    if len(files) > MAX_FILES:
        raise SkillInstallError("skill_too_many_files", f"文件太多（{len(files)} 个），上限 {MAX_FILES} 个。")
    total = 0
    for item in files:
        size = item.stat().st_size
        if size > MAX_FILE_BYTES:
            raise SkillInstallError(
                "skill_file_too_large", f"文件 {item.name} 有 {size} 字节，单文件上限 {MAX_FILE_BYTES}。"
            )
        total += size
        if total > MAX_TOTAL_BYTES:
            raise SkillInstallError("skill_too_large", f"总体积超过上限（{MAX_TOTAL_BYTES} 字节）。")
        relative = item.relative_to(source)
        destination = staged / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, destination)


def _extract_zip(data: bytes, staged: Path, *, prefix: str = "") -> None:
    """解 zip：逐条校验路径与体积，拒绝符号链接与目录穿越。

    ``prefix`` 非空时只解这一个子目录（市场安装：仓库 zip 里只取目标技能那一支），
    并把前缀去掉——落在暂存目录里的仍然是"技能目录本身"。
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise SkillInstallError("skill_archive_bad", f"压缩包解析失败：{exc}") from exc
    entries = archive.infolist()
    if len(entries) > MAX_FILES:
        raise SkillInstallError("skill_too_many_files", f"压缩包里有 {len(entries)} 个条目，上限 {MAX_FILES}。")
    total = 0
    wanted = prefix.strip("/").rstrip("/")
    for info in entries:
        name = info.filename
        if name.endswith("/"):
            continue
        if wanted:
            # 仓库 zip 的第一层是 `<repo>-<ref>/`，所以按"包含 /prefix/"匹配，
            # 而不是前缀相等——那会把整个仓库当成一个技能。切分与判断都用
            # 补了前导斜杠的同一个串，避免"判断命中但切不动"。
            marker = f"/{wanted}/"
            normalized = f"/{name}"
            if marker not in normalized:
                continue
            name = normalized.split(marker, 1)[1]
            if not name:
                continue
        # 符号链接：zip 用 external_attr 高 16 位存 unix mode，0o120000 是链接。
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise SkillInstallError("skill_archive_symlink", f"压缩包里有符号链接：{name}，已拒绝。")
        pure = Path(name)
        if pure.is_absolute() or ".." in pure.parts:
            raise SkillInstallError("skill_archive_unsafe", f"压缩包含不安全路径：{name}，已拒绝。")
        if info.file_size > MAX_FILE_BYTES:
            raise SkillInstallError("skill_file_too_large", f"{name} 超过单文件上限。")
        total += info.file_size
        if total > MAX_TOTAL_BYTES:
            raise SkillInstallError("skill_too_large", "压缩包解压后超过总体积上限。")
        destination = staged / pure
        destination.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info) as src, destination.open("wb") as dst:
            shutil.copyfileobj(src, dst)


def _locate_skill_md(staged: Path) -> tuple[Path, Path]:
    """找到入口 SKILL.md，返回 ``(技能目录, SKILL.md 路径)``。

    接受两种形状：根目录直接有 SKILL.md（单文件 / 压缩包平铺），或唯一子目录里有
    （GitHub 下载的 zip 通常多包一层 `<repo>-main/`）。
    """
    if (staged / "SKILL.md").is_file():
        return staged, staged / "SKILL.md"
    subdirs = [item for item in staged.iterdir() if item.is_dir() and item.name not in SKIP_DIRS]
    with_skill = [item for item in subdirs if (item / "SKILL.md").is_file()]
    if len(with_skill) == 1:
        return with_skill[0], with_skill[0] / "SKILL.md"
    if not with_skill:
        raise SkillInstallError("skill_entry_missing", "这个来源里没有 SKILL.md，不像是一个技能。")
    raise SkillInstallError(
        "skill_entry_ambiguous",
        "来源里有多个技能目录，请分别安装，或在名字里指定其中一个。",
    )


def _validate_definition(skill_md: Path) -> SkillDefinition:
    # 注意 `load_skill` 的第一个参数是 **SKILL.md 文件**（内部取 parent 当技能目录），
    # 传目录会得到 None —— 那正是"装了却解析不出"的来源。
    definition, errors, _warnings = load_skill(skill_md, source="installed")
    if definition is None:
        raise SkillInstallError(
            "skill_unparsable",
            "SKILL.md 解析不出可用内容：" + ("；".join(errors) if errors else "正文为空"),
        )
    if not definition.instructions.strip():
        raise SkillInstallError("skill_body_empty", "SKILL.md 没有正文，装了也用不上。")
    if len(definition.instructions) > MAX_BODY_CHARS:
        raise SkillInstallError(
            "skill_body_too_long", f"正文 {len(definition.instructions)} 字符，超过 {MAX_BODY_CHARS} 上限。"
        )
    return definition


def _write_source_stamp(target: Path, *, source: str, files: int) -> None:
    stamp = {
        "source": source,
        "installed_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "files": files,
    }
    (target / SOURCE_FILE).write_text(json.dumps(stamp, ensure_ascii=False, indent=2), encoding="utf-8")


def read_source_stamp(skill_dir: Path) -> dict:
    """读来源戳；没有或读坏时返回空 dict（列表接口不该因为这个报错）。"""
    path = skill_dir / SOURCE_FILE
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _stage_from(source: str, staged: Path, *, prefix: str = "") -> None:
    """把来源物化到暂存目录：URL（.md / .zip）或本地路径（目录 / 文件）。"""
    text = source.strip()
    if not text:
        raise SkillInstallError("skill_source_empty", "请填一个地址或本地目录。")
    if text.startswith(("http://", "https://")):
        try:
            with httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=True) as client:
                response = client.get(text)
        except httpx.HTTPError as exc:
            raise SkillInstallError("skill_fetch_failed", f"下载失败：{exc.__class__.__name__}") from exc
        if response.status_code >= 400:
            raise SkillInstallError("skill_fetch_failed", f"下载失败：HTTP {response.status_code}")
        data = response.content
        if len(data) > MAX_DOWNLOAD_BYTES:
            raise SkillInstallError("skill_too_large", "下载内容超过体积上限。")
        if zipfile.is_zipfile(io.BytesIO(data)):
            _extract_zip(data, staged, prefix=prefix)
        else:
            (staged / "SKILL.md").write_bytes(data)
        return

    path = Path(text).expanduser()
    if not path.exists():
        raise SkillInstallError("skill_source_missing", f"本地路径不存在：{text}")
    if path.is_dir():
        _copy_tree(path, staged)
    else:
        if path.stat().st_size > MAX_DOWNLOAD_BYTES:
            raise SkillInstallError("skill_too_large", "这个文件超过体积上限。")
        # 本地文件也要认压缩包：`.zip` 直接拷成 SKILL.md 会变成一份解析不了的技能。
        if zipfile.is_zipfile(path):
            _extract_zip(path.read_bytes(), staged, prefix=prefix)
        else:
            if path.stat().st_size > MAX_FILE_BYTES:
                raise SkillInstallError("skill_file_too_large", "这个文件超过单文件上限。")
            shutil.copy2(path, staged / "SKILL.md")


def _name_from_source(source: str) -> str:
    """从来源地址里取一个候选名（URL 取最后一段，本地取目录名或文件名）。"""
    text = source.strip().rstrip("/")
    if text.startswith(("http://", "https://")):
        tail = text.split("?", 1)[0].rsplit("/", 1)[-1]
    else:
        tail = Path(text).name
    return slugify_name(tail)


def install_skill(
    source: str,
    *,
    scope: str = "user",
    project_root: str | Path | None = None,
    name: str = "",
    overwrite: bool = False,
    prefix: str = "",
    allow_unsafe: bool = False,
) -> dict:
    """安装一份技能，返回 ``{name, path, scope, files, source, scan}``。

    失败一律抛 :class:`SkillInstallError`；成功时目标目录已经完整可读（先写暂存目录，
    验证通过再整体换名），不会留下半个技能。
    """
    root = skills_root(scope, project_root=project_root)
    root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="blank-skill-") as tmp:
        staged = Path(tmp) / "stage"
        staged.mkdir()
        _stage_from(source, staged, prefix=prefix)
        skill_dir, skill_md = _locate_skill_md(staged)
        definition = _validate_definition(skill_md)

        # 静态检查放在"格式验过、还没落盘"这个位置：这时候整份内容已经在暂存目录里，
        # 拦下来不会在目标目录留下任何东西，也不必回滚。
        scan = scan_skill_dir(staged)
        if scan.level == "danger" and not allow_unsafe:
            lines = "; ".join(f"{item.detail}（{item.file}）" for item in scan.findings if item.level == "danger")
            raise SkillInstallError(
                "unsafe_skill",
                f"这份技能里有明确危险的内容，没有安装：{lines}。"
                "确认要装就在 install_market_skill 里显式传 allow_unsafe=true，那会再过一次审批。",
                status_code=409,
            )

        # 名字：用户显式给的必须**原样合法**（不替他改名，否则"我填的和它存的不一样"）；
        # 从来源推出来的才允许 slug 化（`demo-skill` / `evil.zip` 这类）。
        if name.strip():
            resolved = _check_name(name.strip())
        else:
            candidate = skill_dir.name if skill_dir != staged else ""
            if not candidate:
                candidate = _name_from_source(source) or slugify_name(definition.display_name)
            resolved = _check_name(slugify_name(candidate))

        target = _ensure_inside(root / resolved, root)
        if target.exists():
            if not overwrite:
                raise SkillInstallError(
                    "skill_exists",
                    f"技能「{resolved}」已经存在。要替换请勾上「覆盖同名」。",
                    status_code=409,
                )
            # 覆盖 = 整目录替换：合并会留下上一版独有的文件，行为最难解释。
            shutil.rmtree(target)

        # 先复制到同级临时目录再 rename：中途失败不会留下半个目标目录。
        staging_target = root / f".{resolved}.installing"
        if staging_target.exists():
            shutil.rmtree(staging_target)
        shutil.copytree(skill_dir, staging_target)
        files = sum(1 for item in staging_target.rglob("*") if item.is_file())
        _write_source_stamp(staging_target, source=source, files=files)
        staging_target.rename(target)

    return {
        "name": resolved,
        "path": str(target),
        "scope": scope,
        "files": files,
        "source": source,
        "description": definition.description,
        "scan": {**scan.as_dict(), "summary": scan_summary(scan)},
    }


def uninstall_skill(
    name: str,
    *,
    scope: str = "user",
    project_root: str | Path | None = None,
    path: str = "",
    roots: Sequence[str | Path] = (),
) -> dict:
    """删掉一份已安装的技能。

    两种定位方式：
    - ``path``：直接给技能目录（技能库里那条记录的 path），此时必须落在
      ``roots`` 的某个技能根之内——**多项目场景下按名字猜会删错项目**。
    - ``name`` + ``scope``：老口径，单项目时够用。

    无论哪种，都要求目录里有 ``SKILL.md``：防止手滑把别的目录清掉。
    """
    if path:
        target = Path(path).expanduser().resolve()
        allowed = [Path(root).expanduser().resolve() for root in roots]
        if not any(root in target.parents for root in allowed):
            raise SkillInstallError("skill_path_outside_root", "这个目录不在任何技能根里，已拒绝删除。")
        if target.name != name:
            raise SkillInstallError("skill_name_mismatch", "传进来的路径与技能名不一致，已拒绝。")
    else:
        root = skills_root(scope, project_root=project_root)
        target = _ensure_inside(root / _check_name(name), root)

    if not target.is_dir():
        raise SkillInstallError("skill_not_found", f"没有名为「{name}」的技能。", status_code=404)
    if not (target / "SKILL.md").is_file():
        raise SkillInstallError("skill_not_removable", "这个目录里没有 SKILL.md，不像技能目录，已拒绝删除。")
    shutil.rmtree(target)
    return {"name": name, "path": str(target)}
