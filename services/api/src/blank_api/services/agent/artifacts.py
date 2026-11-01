"""Durable, redacted tool artifacts kept outside the model context.

工具完整输出落在这里，模型上下文里只放摘要。三件事必须同时成立，否则这个目录迟早
把用户磁盘吃满：

* **有上限**：单份落盘有硬上限，超了就地截断并在载荷里写明原始大小。截断是
  "读得回来但少了尾巴"，与"文件根本没写"完全不是一回事，读侧必须能分辨。
* **有压缩**：JSON 工具输出重复率极高（同一份文件读三遍、同一段测试日志），
  gzip 常能压到 1/5。压缩只影响磁盘，`load()` 拿到的仍是原来的载荷。
* **能算清占用、能安全清理**：`stats()` 报磁盘占用，清理由
  :mod:`blank_api.services.maintenance` 做——那里才有数据库，才知道哪些文件
  仍被 `ToolCall` 行引用。**这个模块刻意不碰数据库**：它被网关、图节点、证据包
  三处直接 import，多一条数据库依赖就会绕出循环引用（见 ``agent/__init__.py``）。
"""

from __future__ import annotations

import gzip
import json
import os
import zlib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ARTIFACT_ROOT = Path("data/artifacts")

# 单份工具输出的落盘上限。原来的红化只截到 10MB，而一次 `run_shell` 把整份构建日志
# 倒出来、或者 `read_file` 读到一个几 MB 的生成文件，就会让每个任务的磁盘占用失控。
# 8MB 足够装下一次正常构建的尾巴，超出部分本来也不会进模型上下文。
MAX_ARTIFACT_BYTES = int(os.environ.get("BLANK_ARTIFACT_MAX_BYTES") or 8 * 1024 * 1024)
# 超过这个大小才 gzip。小文件压了反而更大（gzip 头 + 无法压缩的结构字符）。
COMPRESS_ABOVE_BYTES = int(os.environ.get("BLANK_ARTIFACT_COMPRESS_ABOVE") or 32 * 1024)
# 截断时至少留这么多字符，免得把一条短结论也削成空串。
MIN_KEEP_CHARS = 200

SUFFIX_PLAIN = ".json"
SUFFIX_GZIP = ".json.gz"

TRUNCATED_NOTE = "\n\n…[artifact 超出单份上限，尾部已丢弃：原始 {original} 字节，上限 {limit} 字节]"


@dataclass(frozen=True)
class ArtifactFile:
    """磁盘上的一份产物。``size`` 是实际占用，``bytes_written`` 是写下去的字节数。"""

    task_id: str
    artifact_id: str
    path: Path
    size: int
    mtime: float
    compressed: bool

    def as_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "artifact_id": self.artifact_id,
            "path": self.path.as_posix(),
            "size": self.size,
            "mtime": self.mtime,
            "compressed": self.compressed,
        }


def _encode(payload: dict, limit: int) -> tuple[bytes, bool]:
    """序列化并夹到 ``limit`` 以内。返回 (字节, 是否截断过)。

    截断顺序是「先砍最长的可读文本，再砍结构化数据里的字符串」：工具结果里最占地方
    的永远是 output/message，而 `data` 里常带着模型后续要用的小字段（路径、行号、
    退出码）。砍 data 会改变语义，砍 output 只是少了一段日志尾巴。
    """
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(raw) <= limit:
        return raw, False

    original = len(raw)
    shrunk = dict(payload)
    original_bytes = shrunk.get("original_bytes")
    shrunk["truncated"] = True
    shrunk["original_bytes"] = int(original_bytes or original)
    note = TRUNCATED_NOTE.format(original=int(original_bytes or original), limit=limit)

    # 先整段砍 output / message：它们没有结构，砍掉不影响任何下游解析。
    for key in ("output", "message"):
        value = shrunk.get(key)
        if not isinstance(value, str) or not value:
            continue
        budget = max(MIN_KEEP_CHARS, limit // 2)
        if len(value.encode("utf-8")) > budget:
            shrunk[key] = _cut_text(value, budget) + note
        raw = json.dumps(shrunk, ensure_ascii=False).encode("utf-8")
        if len(raw) <= limit:
            return raw, True

    # 还不够就把 data 里最长的字符串按长度递降往下砍，直到塞进上限。
    data = shrunk.get("data")
    if isinstance(data, dict):
        shrunk["data"] = _shrink_value(data, limit - len(raw) + _measure(data), note)
        raw = json.dumps(shrunk, ensure_ascii=False).encode("utf-8")

    if len(raw) > limit:
        # 兜底：连结构都装不下时只留骨架。宁可丢内容，也不让写入把磁盘占满。
        raw = json.dumps(
            {
                "tool": shrunk.get("tool") or payload.get("tool") or "",
                "status": shrunk.get("status") or payload.get("status") or "",
                "truncated": True,
                "original_bytes": int(original_bytes or original),
                "message": note.strip(),
            },
            ensure_ascii=False,
        ).encode("utf-8")
    return raw, True


def _measure(value) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def _cut_text(text: str, budget: int) -> str:
    """按字节预算保留开头。多字节字符在边界处不切断（解码回来仍是合法 utf-8）。"""
    encoded = text.encode("utf-8")
    if len(encoded) <= budget:
        return text
    # 头 3/4、尾 1/4：失败摘要通常在开头，结论通常在结尾，中间才是可丢的部分。
    head = encoded[: int(budget * 0.75)].decode("utf-8", errors="ignore")
    tail = encoded[-(budget - len(head.encode("utf-8"))):].decode("utf-8", errors="ignore")
    return f"{head}\n…[中间省略]…\n{tail}"


def _shrink_value(value, budget: int, note: str):
    """把 dict/list 里最长的字符串砍到能塞进预算。预算为负时一律砍到最小。"""
    if isinstance(value, str):
        keep = max(MIN_KEEP_CHARS, budget)
        return _cut_text(value, keep) + (note if len(value.encode("utf-8")) > keep else "")
    if isinstance(value, dict):
        return {key: _shrink_value(item, budget // max(1, len(value)), note) for key, item in value.items()}
    if isinstance(value, list):
        return [_shrink_value(item, budget // max(1, len(value)), note) for item in value]
    return value


class ArtifactStore:
    def __init__(self, root: Path | str | None = None, *, max_bytes: int | None = None) -> None:
        # 显式传 root 优先（测试与清理工具用它），否则认 ``BLANK_ARTIFACT_DIR``，
        # 再退回仓库内的 data/artifacts。桌面壳把前者指到用户数据目录，
        # 否则"换一个包"就等于换一个产物目录，旧任务的完整输出全部读不出来。
        if root is not None:
            self.root = Path(root)
        else:
            self.root = Path(os.environ.get("BLANK_ARTIFACT_DIR") or DEFAULT_ARTIFACT_ROOT)
        self.max_bytes = int(max_bytes if max_bytes is not None else MAX_ARTIFACT_BYTES)

    # ---- 路径 ---------------------------------------------------------------
    def candidates(self, task_id: str, artifact_id: str) -> list[Path]:
        """同一个 id 可能落在 `.json.gz`（压缩过）或 `.json`（老版本/小文件）上。"""
        base = self.root / task_id
        return [base / f"{artifact_id}{SUFFIX_GZIP}", base / f"{artifact_id}{SUFFIX_PLAIN}"]

    def locate(self, task_id: str, artifact_id: str) -> Path | None:
        for path in self.candidates(task_id, artifact_id):
            if path.exists():
                return path
        return None

    # ---- 读写 ---------------------------------------------------------------
    def save(self, task_id: str, artifact_id: str, payload: dict) -> str:
        """写一份产物，返回**引用键**（``<task>/<id>.<ext>``，相对产物根）。"""
        return self.write(task_id, artifact_id, payload)[0]

    def write(self, task_id: str, artifact_id: str, payload: dict) -> tuple[str, dict]:
        """写一份产物，返回 (引用键, 写入读数)。

        引用键是**相对路径**（``<task_id>/<artifact_id>.json[.gz]``），不是磁盘绝对路径。
        这条口径是硬要求：``artifact_path`` 会进 ``tool.completed`` 事件、证据包和工具
        回执，一旦写进绝对路径，宿主目录结构就跟着端出去了（实测：把产物根指到用户
        数据目录后，证据包里出现了 ``C:/Users/<名字>/…``）。相对键在"换机器"、
        "换安装目录"之后仍指得回同一份产物，也正好是清理时要比对的那个身份。

        第二项给指标用（字节数、是否压缩、是否截断）。截断必须能被看见——一份被削过
        的工具输出如果和完整输出长得一样，模型会以为自己读到了全部。
        """
        raw, truncated = _encode(payload, self.max_bytes)
        compressed = len(raw) >= COMPRESS_ABOVE_BYTES
        target = (
            self.root / task_id / f"{artifact_id}{SUFFIX_GZIP if compressed else SUFFIX_PLAIN}"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        data = gzip.compress(raw, compresslevel=6) if compressed else raw
        # 先写同名临时文件再替换：中途被打断时不会留下半截 JSON，
        # 而半截文件在读侧会变成 JSONDecodeError——那看起来像"产物损坏"，不是"写了一半"。
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
        # 同一 id 换了后缀时把旧的那份删掉，避免两份并存让"占用统计"翻倍。
        stale = target.with_name(
            f"{artifact_id}{SUFFIX_PLAIN if compressed else SUFFIX_GZIP}"
        )
        if stale.exists():
            try:
                stale.unlink()
            except OSError:
                pass
        return f"{task_id}/{target.name}", {
            "bytes": len(data),
            "raw_bytes": len(raw),
            "compressed": compressed,
            "truncated": truncated,
        }

    def load(self, task_id: str, artifact_id: str) -> dict:
        path = self.locate(task_id, artifact_id)
        if path is None:
            raise FileNotFoundError(artifact_id)
        data = path.read_bytes()
        if path.name.endswith(SUFFIX_GZIP):
            try:
                data = gzip.decompress(data)
            except (OSError, EOFError, zlib.error) as error:  # 被外部截断：读不出来，不当成空载荷
                raise ValueError(f"artifact 解压失败：{artifact_id}（{error}）") from None
        return json.loads(data.decode("utf-8"))

    # ---- 统计 ---------------------------------------------------------------
    def iter_files(self, *, task_id: str | None = None) -> list[ArtifactFile]:
        """列出磁盘上的产物。只认两个后缀，临时文件与其它残留不算产物。"""
        if not self.root.exists():
            return []
        rows: list[ArtifactFile] = []
        pattern = f"{task_id}/*" if task_id else "*/*"
        for path in sorted(self.root.glob(pattern)):
            if not path.is_file():
                continue
            name = path.name
            if name.endswith(SUFFIX_GZIP):
                artifact_id, compressed = name[: -len(SUFFIX_GZIP)], True
            elif name.endswith(SUFFIX_PLAIN):
                artifact_id, compressed = name[: -len(SUFFIX_PLAIN)], False
            else:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            rows.append(
                ArtifactFile(
                    task_id=path.parent.name,
                    artifact_id=artifact_id,
                    path=path,
                    size=stat.st_size,
                    mtime=stat.st_mtime,
                    compressed=compressed,
                )
            )
        return rows

    def stats(self, *, task_id: str | None = None) -> dict:
        """磁盘占用。给"这个功能正在悄悄吃磁盘"一个能查的数字，而不是靠感觉。"""
        files = self.iter_files(task_id=task_id)
        tasks = {item.task_id for item in files}
        total = sum(item.size for item in files)
        compressed = [item for item in files if item.compressed]
        return {
            "root": self.root.as_posix(),
            "tasks": len(tasks),
            "files": len(files),
            "bytes": total,
            "compressed_files": len(compressed),
            "compressed_bytes": sum(item.size for item in compressed),
            "largest": max((item.size for item in files), default=0),
            "max_bytes_per_file": self.max_bytes,
            "compress_above_bytes": COMPRESS_ABOVE_BYTES,
        }
