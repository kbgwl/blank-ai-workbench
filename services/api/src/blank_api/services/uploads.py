"""图片附件：落盘、读回、以及给模型用的原始字节。

为什么不放进工作区：

工作区是用户自己的仓库。把用户贴的截图写进去，会被 git 看见、被快照带上、被
agent 的目录搜索扫到——而它既不是用户想提交的东西，也不是这次任务要交付的东西。
所以落在 ``data/uploads/<project>`` 下，按项目分目录，删项目时一并清掉。
"""

from __future__ import annotations

import base64
import binascii
import uuid
from dataclasses import dataclass
from pathlib import Path

DEFAULT_UPLOAD_ROOT = Path("data/uploads")

# 只收模型真正能看懂的位图。svg 是文本、pdf 不是图、heic 多数供应商不认——
# 收进来只会在调用模型时变成 400，不如在这里就说清楚。
ALLOWED_MIME: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

# 单张 8MB。再大 base64 之后请求体就到 11MB 了，而大多数供应商对单图有自己的上限
# （常见 5MB / 20MB），超过会在最贵的那一步才报错。
MAX_UPLOAD_BYTES = 8 * 1024 * 1024


class UploadError(ValueError):
    """附件的用户可见错误。message 直接给界面看，不要写成英文异常。"""


@dataclass(frozen=True)
class UploadRef:
    id: str
    name: str
    mime: str
    size: int
    path: str

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "mime": self.mime, "size": self.size}


class UploadStore:
    def __init__(self, root: Path | str = DEFAULT_UPLOAD_ROOT) -> None:
        self.root = Path(root)

    def project_dir(self, project_id: str) -> Path:
        return self.root / project_id

    def save(self, project_id: str, name: str, mime: str, data: bytes) -> UploadRef:
        if mime not in ALLOWED_MIME:
            raise UploadError("只支持 PNG / JPEG / WebP / GIF 图片")
        if not data:
            raise UploadError("图片内容为空")
        if len(data) > MAX_UPLOAD_BYTES:
            raise UploadError(
                f"图片超过 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB，请先压缩或裁剪"
            )
        upload_id = uuid.uuid4().hex
        # 文件名只由 id 与扩展名决定：原始文件名可能带路径分隔符、控制字符、
        # 同名覆盖，任何一样都不该让写入落到预期之外的位置。
        target = self.project_dir(project_id) / f"{upload_id}{ALLOWED_MIME[mime]}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return UploadRef(
            id=upload_id,
            name=_safe_label(name, ALLOWED_MIME[mime]),
            mime=mime,
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
        for mime, ext in ALLOWED_MIME.items():
            candidate = folder / f"{upload_id}{ext}"
            if candidate.is_file():
                return UploadRef(
                    id=upload_id,
                    name=f"{upload_id}{ext}",
                    mime=mime,
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


def decode_payload(data: str) -> bytes:
    """前端传来的 base64（可能带 ``data:image/png;base64,`` 前缀）。"""
    raw = (data or "").strip()
    if "," in raw and raw.lstrip().startswith("data:"):
        raw = raw.split(",", 1)[1]
    try:
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise UploadError("图片数据不是合法的 base64") from exc


def load_images_for(
    project_id: str,
    refs: list[dict],
    *,
    max_total_bytes: int = 20 * 1024 * 1024,
    root: Path | str | None = None,
) -> list[dict]:
    """把任务上的附件读成模型能直接吃的形式。

    返回 ``[{"name", "media_type", "data"(base64)}]``。base64 而不是 bytes：四家
    供应商的请求体最终都是 JSON，在这里就编好只需要编一次，往下走全是字符串。

    读不到的单张跳过。一张图被清理掉、或者磁盘正好抖动，代价应该是"这轮少一张图"，
    而不是"这条消息发不出去"——用户看不出区别，但前者至少还能干活。
    """
    store = UploadStore(root) if root else UploadStore()
    images: list[dict] = []
    total = 0
    for item in refs or ():
        ref = store.find(project_id, str(item.get("id") or ""))
        if ref is None:
            continue
        if total + ref.size > max_total_bytes:
            # 后面的一律不读：模型侧本来就有总图数/总字节的上限，与其让它 400，
            # 不如少给几张，让这一轮能跑起来。
            break
        try:
            payload = Path(ref.path).read_bytes()
        except OSError:
            continue
        total += len(payload)
        images.append(
            {
                "name": str(item.get("name") or ref.name),
                "media_type": ref.mime,
                "data": base64.b64encode(payload).decode("ascii"),
            }
        )
    return images


def _safe_label(name: str, ext: str) -> str:
    """只保留最后一段文件名，并截断。空的话给个占位名。"""
    base = (name or "").replace("\\", "/").split("/")[-1].strip()
    if not base:
        return f"image{ext}"
    return base[:120]
