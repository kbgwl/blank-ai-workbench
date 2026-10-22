"""附件：落盘、读回、以及给模型用的原始形态。

三类附件，走两条路：

- **图片**（png/jpeg/webp/gif）→ base64 进消息的 image block，模型"看"。
- **文本类**（md/txt/csv/json/代码…）→ 解码后进提示词，模型"读"。
- **PDF** → 抽文本后与文本类同路；抽不出来的（扫描件）明确告诉用户，而不是
  丢给模型让它猜。

为什么不放进工作区：

工作区是用户自己的仓库。把用户贴的截图写进去，会被 git 看见、被快照带上、被
agent 的目录搜索扫到——而它既不是用户想提交的东西，也不是这次任务要交付的东西。
所以落在 ``data/uploads/<project>`` 下，按项目分目录，删项目时一并清掉。
"""

from __future__ import annotations

import base64
import binascii
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_UPLOAD_ROOT = Path("data/uploads")

IMAGE_MIME = "image"

# 只收模型真正能看懂的位图。svg 是文本、heic 多数供应商不认——收进来只会
# 在调用模型时变成 400，不如在这里就说清楚。
IMAGE_EXT_BY_MIME: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

IMAGE_MIME_BY_EXT: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

# 文本类的判定看扩展名而不是 mime：浏览器给 .md 的 type 常常是空串，
# 给 .py 的是 text/x-python，各平台还不一致。扩展名反而是稳定的。
TEXT_MIME_BY_EXT: dict[str, str] = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".log": "text/plain",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".json": "application/json",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".toml": "application/toml",
    ".ini": "text/plain",
    ".cfg": "text/plain",
    ".py": "text/x-python",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".cjs": "text/javascript",
    ".ts": "text/typescript",
    ".tsx": "text/typescript",
    ".jsx": "text/javascript",
    ".sql": "application/sql",
    ".html": "text/html",
    ".css": "text/css",
    ".sh": "text/x-shellscript",
    ".xml": "application/xml",
}

PDF_MIME = "application/pdf"
PDF_EXT = ".pdf"

# 兼容旧引用：外界只认这张表是"图片的 mime → 扩展名"。
ALLOWED_MIME = IMAGE_EXT_BY_MIME

# 单份 8MB。再大 base64 之后请求体就到 11MB 了，而大多数供应商对单图有自己的上限
# （常见 5MB / 20MB），超过会在最贵的那一步才报错。
MAX_UPLOAD_BYTES = 8 * 1024 * 1024

# 一个轮次的图片总量上限。超过就不往后读了——模型侧本来就有总字节限制，
# 与其让它 400，不如少给几张；但**丢了几张必须说出来**，不能静默。
MAX_TOTAL_IMAGE_BYTES = 20 * 1024 * 1024

# 文本类：单份最多读这么多字节，单份最多留这么多字，整轮合计上限。
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_TEXT_CHARS = 20_000
MAX_TOTAL_TEXT_CHARS = 60_000

# 超过这个天数、又没有任何任务引用的附件算孤儿。
ORPHAN_DAYS = 7


class UploadError(ValueError):
    """附件的用户可见错误。message 直接给界面看，不要写成英文异常。"""


def _norm_mime(mime: str) -> str:
    return (mime or "").split(";", 1)[0].strip().lower()


def classify(name: str, mime: str) -> tuple[str, str]:
    """把一份附件归到 (扩展名, mime)。归不了就抛用户能看懂的话。"""
    ext = Path(name or "").suffix.lower()
    norm = _norm_mime(mime)

    if norm in IMAGE_EXT_BY_MIME:
        return IMAGE_EXT_BY_MIME[norm], norm
    if norm == PDF_MIME or ext == PDF_EXT:
        return PDF_EXT, PDF_MIME
    if ext in IMAGE_MIME_BY_EXT:
        return ext, IMAGE_MIME_BY_EXT[ext]
    if ext in TEXT_MIME_BY_EXT:
        return ext, TEXT_MIME_BY_EXT[ext]
    # 前端已经把 type 归一过了，走到这里基本就是真不支持（zip / exe / 视频）。
    raise UploadError("只支持图片（PNG / JPEG / WebP / GIF）、文本类文件与 PDF")


def kind_of(mime: str) -> str:
    norm = _norm_mime(mime)
    if norm in IMAGE_EXT_BY_MIME:
        return IMAGE_MIME
    if norm == PDF_MIME:
        return "pdf"
    return "text"


@dataclass(frozen=True)
class UploadRef:
    id: str
    name: str
    mime: str
    size: int
    path: str

    @property
    def kind(self) -> str:
        return kind_of(self.mime)

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "mime": self.mime, "size": self.size}


_EVERY_EXT = {*IMAGE_MIME_BY_EXT, *TEXT_MIME_BY_EXT, PDF_EXT}


def _mime_for_stored(ext: str) -> str:
    if ext in IMAGE_MIME_BY_EXT:
        return IMAGE_MIME_BY_EXT[ext]
    if ext == PDF_EXT:
        return PDF_MIME
    return TEXT_MIME_BY_EXT.get(ext, "application/octet-stream")


class UploadStore:
    def __init__(self, root: Path | str = DEFAULT_UPLOAD_ROOT) -> None:
        self.root = Path(root)

    def project_dir(self, project_id: str) -> Path:
        return self.root / project_id

    def save(self, project_id: str, name: str, mime: str, data: bytes) -> UploadRef:
        ext, resolved = classify(name, mime)
        if not data:
            raise UploadError("附件内容为空")
        if len(data) > MAX_UPLOAD_BYTES:
            raise UploadError(f"附件超过 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB，请先压缩或裁剪")
        upload_id = uuid.uuid4().hex
        # 文件名只由 id 与扩展名决定：原始文件名可能带路径分隔符、控制字符、
        # 同名覆盖，任何一样都不该让写入落到预期之外的位置。
        target = self.project_dir(project_id) / f"{upload_id}{ext}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return UploadRef(
            id=upload_id,
            name=_safe_label(name, ext),
            mime=resolved,
            size=len(data),
            path=target.as_posix(),
        )

    def find(self, project_id: str, upload_id: str) -> UploadRef | None:
        """按 id 找文件。id 必须是纯十六进制串，防止 ``../`` 这类构造读到目录外。"""
        if not upload_id or not all(char in "0123456789abcdef" for char in upload_id):
            return None
        folder = self.project_dir(project_id)
        if not folder.is_dir():
            return None
        for ext in _EVERY_EXT:
            candidate = folder / f"{upload_id}{ext}"
            if candidate.is_file():
                return UploadRef(
                    id=upload_id,
                    name=f"{upload_id}{ext}",
                    mime=_mime_for_stored(ext),
                    size=candidate.stat().st_size,
                    path=candidate.as_posix(),
                )
        return None

    def resolve(self, project_id: str, refs: list[dict]) -> list[UploadRef]:
        """把请求里那串附件描述还原成真实存在文件。找不到的直接丢掉。

        丢掉而不是报错：附件是"锦上添花"的输入，一张图丢了不该让整轮任务发不出去。
        真正该拒绝的情况（格式、大小）在 save 那一步已经拦过了。
        """
        found: list[UploadRef] = []
        for item in refs:
            upload_id = str(item.get("id") or "")
            resolved = self.find(project_id, upload_id)
            if resolved is None:
                continue
            name = str(item.get("name") or "").strip()
            found.append(
                UploadRef(
                    id=resolved.id,
                    name=name or resolved.name,
                    mime=resolved.mime,
                    size=resolved.size,
                    path=resolved.path,
                )
            )
        return found

    def clear(self, project_id: str) -> None:
        """删项目时清掉它的附件目录。清不干净不影响删项目本身。"""
        folder = self.project_dir(project_id)
        if not folder.is_dir():
            return
        for child in folder.iterdir():
            if child.is_file():
                child.unlink(missing_ok=True)
        folder.rmdir()

    def remove(self, project_id: str, upload_ids: set[str]) -> int:
        """删掉若干附件文件。返回真正删掉的个数。"""
        folder = self.project_dir(project_id)
        if not folder.is_dir() or not upload_ids:
            return 0
        removed = 0
        for child in folder.iterdir():
            if child.is_file() and child.stem in upload_ids:
                try:
                    child.unlink()
                    removed += 1
                except OSError:
                    # 删不掉就是留着：占点磁盘远好过让删任务本身失败。
                    continue
        return removed

    def sweep(self, keep: set[str], *, older_than_days: int = ORPHAN_DAYS) -> int:
        """清掉"没被任何任务引用 + 够老"的附件。

        为什么要老：用户可能刚选完图、正在写字，这时候文件已经在盘上但还没有任务
        引用它。按"天数"而不是"引用数"单独判断，就是为了不误删这种草稿。

        返回删掉的个数；任何异常都吞掉——启动时的清理失败了不该拦住服务启动。
        """
        if not self.root.is_dir():
            return 0
        cutoff = time.time() - older_than_days * 86400
        removed = 0
        for project_dir in self.root.iterdir():
            if not project_dir.is_dir():
                continue
            for child in project_dir.iterdir():
                if not child.is_file() or child.stem in keep:
                    continue
                try:
                    if child.stat().st_mtime > cutoff:
                        continue
                    child.unlink()
                    removed += 1
                except OSError:
                    continue
        return removed


def decode_payload(data: str) -> bytes:
    """前端传来的 base64（可能带 ``data:image/png;base64,`` 前缀）。"""
    raw = (data or "").strip()
    if "," in raw and raw.lstrip().startswith("data:"):
        raw = raw.split(",", 1)[1]
    try:
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise UploadError("附件数据不是合法的 base64") from exc


@dataclass
class AttachmentBundle:
    """一轮附件读出来的结果：给模型的、没给成的，都在这里。"""

    images: list[dict] = field(default_factory=list)
    texts: list[dict] = field(default_factory=list)
    # [{"name", "reason"}]，reason ∈ missing / unreadable / over_limit / no_text / unsupported
    dropped: list[dict] = field(default_factory=list)

    @property
    def attached(self) -> int:
        return len(self.images) + len(self.texts) + len(self.dropped)

    def summarize(self) -> dict:
        return {
            "attached": self.attached,
            "used": len(self.images) + len(self.texts),
            "images": len(self.images),
            "texts": len(self.texts),
            "dropped": [dict(item) for item in self.dropped],
        }


def _pdf_text(path: Path) -> str:
    """抽 PDF 的文本。抽不出来返回空串——扫描件就是这样，不该假装读到。"""
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover - 只在缺依赖的安装里发生
        return ""
    try:
        reader = PdfReader(str(path))
        chunks: list[str] = []
        total = 0
        for page in reader.pages:
            piece = page.extract_text() or ""
            if not piece:
                continue
            chunks.append(piece)
            total += len(piece)
            # 页数没有上限，字数要有：一份 300 页的规格书塞进去，这一轮就只剩它了。
            if total >= MAX_TEXT_CHARS:
                break
        return "\n".join(chunks).strip()
    except Exception:
        # 加密、损坏、扫描件都会走到这儿。返回空串，让上层说"没读到文字"。
        return ""


def load_attachments_for(
    project_id: str,
    refs: list[dict],
    *,
    max_image_bytes: int = MAX_TOTAL_IMAGE_BYTES,
    max_text_chars: int = MAX_TEXT_CHARS,
    max_total_text_chars: int = MAX_TOTAL_TEXT_CHARS,
    root: Path | str | None = None,
) -> AttachmentBundle:
    """把任务上的附件读成模型能直接吃的形态。

    图片返回 base64（四家供应商的请求体最终都是 JSON，这里编好只需要编一次）；
    文本返回解码后的字符串。读不到、超限的都记进 ``dropped``，由上层决定怎么告诉
    用户——**不能静默少给**：用户看到 5 个附件、模型只用上 3 个，而界面一个字不说，
    会让人怀疑模型根本没看图。
    """
    store = UploadStore(root) if root else UploadStore()
    bundle = AttachmentBundle()
    image_bytes = 0
    text_chars = 0

    for item in refs or ():
        label = str(item.get("name") or "").strip()
        ref = store.find(project_id, str(item.get("id") or ""))
        if ref is None:
            bundle.dropped.append({"name": label or "附件", "reason": "missing"})
            continue
        name = label or ref.name
        kind = ref.kind

        try:
            payload = Path(ref.path).read_bytes()
        except OSError:
            bundle.dropped.append({"name": name, "reason": "unreadable"})
            continue

        if kind == IMAGE_MIME:
            if image_bytes + len(payload) > max_image_bytes:
                bundle.dropped.append({"name": name, "reason": "over_limit"})
                continue
            image_bytes += len(payload)
            bundle.images.append(
                {
                    "name": name,
                    "media_type": ref.mime,
                    "data": base64.b64encode(payload).decode("ascii"),
                }
            )
            continue

        if kind == "pdf":
            text = _pdf_text(Path(ref.path))
            if not text:
                bundle.dropped.append({"name": name, "reason": "no_text"})
                continue
        else:
            raw = payload[:MAX_TEXT_BYTES]
            text = raw.decode("utf-8", errors="replace").strip()
            if not text:
                bundle.dropped.append({"name": name, "reason": "no_text"})
                continue

        room = max(0, max_total_text_chars - text_chars)
        if room <= 0:
            bundle.dropped.append({"name": name, "reason": "over_limit"})
            continue
        clipped = text[: min(max_text_chars, room)]
        text_chars += len(clipped)
        bundle.texts.append(
            {
                "name": name,
                "text": clipped,
                "truncated": len(clipped) < len(text),
                "kind": ref.kind,
            }
        )

    return bundle


def load_images_for(
    project_id: str,
    refs: list[dict],
    *,
    max_total_bytes: int = MAX_TOTAL_IMAGE_BYTES,
    root: Path | str | None = None,
) -> list[dict]:
    """只要图片的老接口。新代码请用 :func:`load_attachments_for`。"""
    bundle = load_attachments_for(
        project_id, refs, max_image_bytes=max_total_bytes, root=root
    )
    return bundle.images


def _safe_label(name: str, ext: str) -> str:
    """只保留最后一段文件名，并截断。空的话给个占位名。"""
    base = (name or "").replace("\\", "/").split("/")[-1].strip()
    if not base:
        return f"attachment{ext}"
    return base[:120]
