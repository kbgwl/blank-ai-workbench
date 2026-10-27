"""REA 的实物解析层：从字节里读出格式、入口与证据，失败时指名道姓。

**为什么手写而不是找个库**：REA-1 要的是「目标识别 + 入口线索 + 证据记录」这条最小闭环，
本机没有 Ghidra / radiffx，装一个第三方解析器又会把「打包链上多一环」的债留给桌面包。
这里只解析三种真实形态——Electron 的 ``asar``、Windows 的 ``PE``、Linux/Android 的 ``ELF``——
全部按公开的字节布局自己读，每个偏移都能被脚本重算，也因此**能被截断**：
反例（畸形产物）不是模拟出来的异常，而是同一份解析器在真实偏移上读不下去了。

失败口径是本模块唯一的责任：``ArtifactParseError`` 一定带「第几步（共几步）·步骤名·被解析对象·
声明值与实际字节数」，因为症状「解析失败」对排障毫无价值，「头部声明元数据 1,264 字节，
文件只有 4,096 字节，需要读到 5,312」才有价值。
"""

from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import dataclass
from typing import Any, NoReturn

# 一次读入的上限：逆向目标可能很大，而这条链路只需要头部。
MAX_TARGET_BYTES = 64 * 1024 * 1024

# 每种格式的解析步骤（名字与顺序就是报错时要念的那串）。写死成表有两个用处：
# 报错能说「步骤 3/4」，测试能断言「步骤名不许漂」。
STEPS: dict[str, tuple[str, ...]] = {
    "asar": ("外层尺寸字段", "头部 pickle 尺寸", "元数据字符串尺寸", "元数据形状", "文件内容偏移"),
    "pe": ("MS-DOS 头", "PE 头偏移", "PE 签名", "COFF 头", "可选头", "段表", "段内容边界"),
    "elf": ("ELF 幻数与位宽", "字节序", "ELF 头字段", "段表规模", "段表边界"),
}

PE_MACHINES = {
    0x014C: "Intel 80386（x86 32 位）",
    0x0200: "Itanium IA-64",
    0x01C0: "ARM（32 位）",
    0x01C4: "ARMv7 Thumb-2",
    0xAA64: "ARM64",
    0x8664: "AMD64 / Intel 64（x86-64）",
}
PE_OPTIONAL_MAGICS = {0x10B: "PE32（32 位）", 0x20B: "PE32+（64 位）", 0x107: "ROM 镜像"}
ELF_MACHINES = {
    0x03: "x86（32 位）",
    0x28: "ARM（32 位）",
    0x3E: "AMD x86-64",
    0xB7: "AArch64",
    0xF3: "RISC-V",
}
ELF_TYPES = {0x01: "重定位对象（.o）", 0x02: "可执行文件", 0x03: "共享库 / 动态对象", 0x04: "核心转储"}
# 只有 asar 用得上（Electron 归档），PE/ELF 的入口另有其账
_KNOWN_FORMATS = ("asar", "pe", "elf")


class ParseFailure(Exception):
    """解析失败的共同形状：``reason`` 就是要端给用户的那句指名道姓的话。"""

    def __init__(
        self,
        *,
        kind: str,
        target: str,
        format_name: str,
        step: int = 0,
        step_name: str = "",
        detail: str = "",
    ) -> None:
        self.kind = kind
        self.target = target
        self.format = format_name
        self.step = step
        self.step_name = step_name
        self.detail = detail
        super().__init__(self.reason)

    @property
    def total_steps(self) -> int:
        return len(STEPS.get(self.format, ()))

    @property
    def reason(self) -> str:
        if self.kind == "unsupported_format":
            return (
                f"没有解析器认领被解析对象 <{self.target}>："
                f"已按 {'/'.join(_KNOWN_FORMATS)} 的幻数逐一比对都不符（首部字节 {self.detail}）"
            )
        where = f"步骤 {self.step}/{self.total_steps}「{self.step_name}」"
        return f"解析失败｜被解析对象 <{self.target}>｜格式 {self.format}｜{where}｜{self.detail}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "target": self.target,
            "format": self.format,
            "step": self.step,
            "step_name": self.step_name,
            "steps_total": self.total_steps,
            "detail": self.detail,
            "reason": self.reason,
        }


class ArtifactParseError(ParseFailure):
    """头部/元数据读不下去：指名第几步、被解析对象、声明值与实际字节数。"""

    def __init__(self, *, target: str, format_name: str, step: int, step_name: str, detail: str) -> None:
        super().__init__(
            kind="parse_failed",
            target=target,
            format_name=format_name,
            step=step,
            step_name=step_name,
            detail=detail,
        )


class UnsupportedFormat(ParseFailure):
    """幻数谁都不认：这不是「没有发现」，是「没能力看」，必须与成功区分开。"""

    def __init__(self, *, target: str, head: bytes) -> None:
        super().__init__(
            kind="unsupported_format",
            target=target,
            format_name="unknown",
            detail=head[:16].hex() or "空文件",
        )


class TargetUnavailable(Exception):
    """目标读不到（不存在 / 目录 / 越出授权根 / 太大）。与解析失败是两类事。"""


@dataclass(frozen=True)
class Finding:
    """一条可核对的发现：值 + 它从哪来。界面与证据包念的都是这一条。"""

    kind: str
    value: str
    detail: str

    @property
    def evidence_id(self) -> str:
        # 内容寻址而不是自增序号：同一份目标重跑必须给出同一个 id，
        # 否则「这条结论依据哪条证据」第二次打开就对不上了。
        digest = hashlib.sha1(f"{self.kind}|{self.value}|{self.detail}".encode("utf-8")).hexdigest()
        return f"EVD-{digest[:10]}"

    def as_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "value": self.value,
            "detail": self.detail,
            "evidence_id": self.evidence_id,
        }


def _fail(target: str, format_name: str, step: int, detail: str) -> NoReturn:
    raise ArtifactParseError(
        target=target,
        format_name=format_name,
        step=step,
        step_name=STEPS[format_name][step - 1],
        detail=detail,
    )


def _need(raw: bytes, target: str, format_name: str, step: int, offset: int, size: int) -> None:
    """第 N 步要读的这段字节必须真的在文件里。截断的产物就是在这里红的。"""
    have = len(raw) - offset
    if have < size:
        _fail(
            target,
            format_name,
            step,
            f"需要读偏移 {offset} 起的 {size:,} 字节，文件总长 {len(raw):,} 字节，"
            f"只剩 {max(have, 0):,} 字节可读",
        )


def _u32(raw: bytes, offset: int) -> int:
    return int(struct.unpack_from("<I", raw, offset)[0])


def _u16(raw: bytes, offset: int) -> int:
    return int(struct.unpack_from("<H", raw, offset)[0])


def identify(raw: bytes) -> str:
    """按幻数认领格式；认不出来返回空串（由调用方给 UnsupportedFormat）。"""
    if len(raw) >= 16 and raw[:2] == b"MZ":
        return "pe"
    if len(raw) >= 4 and raw[:4] == b"\x7fELF":
        return "elf"
    if len(raw) >= 16 and _u32(raw, 0) in (4,) and _u32(raw, 4) >= 8 and _u32(raw, 12) >= 2:
        return "asar"
    return ""


# --------------------------------------------------------------------------- #
# asar（Electron 应用归档）
# --------------------------------------------------------------------------- #
def _walk_asar_files(node: dict, prefix: str, target: str) -> list[tuple[str, dict]]:
    """递归摊平 ``files`` 树，带回报文里的相对路径与条目本身。

    报错时把对象写成「<产物>→<条目>」：只说 ``gpu-guard.cjs`` 的话，用户不知道是哪个产物里的
    哪个条目——一次调查可以同时喂好几个目标。
    """
    out: list[tuple[str, dict]] = []
    for name, item in sorted((node.get("files") or {}).items()):
        path = f"{prefix}/{name}".lstrip("/")
        if not isinstance(item, dict):
            _fail(f"{target}→{path}", "asar", 4, f"条目必须是对象，实际是 {type(item).__name__}")
        if "files" in item:
            out.extend(_walk_asar_files(item, path, target))
        else:
            out.append((path, item))
    return out


def _asar_meta(target: str, raw: bytes) -> dict[str, Any]:
    """按 asar 公开的头部布局读四段尺寸 + 元数据 JSON。"""
    _need(raw, target, "asar", 1, 0, 4)
    outer = _u32(raw, 0)
    if outer != 4:
        _fail(target, "asar", 1, f"外层载荷尺寸必须是 4，实际声明 {outer:,}")

    _need(raw, target, "asar", 2, 4, 4)
    header_size = _u32(raw, 4)
    if header_size < 8:
        _fail(target, "asar", 2, f"头部 pickle 尺寸必须 ≥8，实际声明 {header_size:,}")
    if 8 + header_size > len(raw):
        _fail(
            target,
            "asar",
            2,
            f"头部声明 pickle 长 {header_size:,} 字节（需读到 {8 + header_size:,}），"
            f"文件总长只有 {len(raw):,} 字节",
        )

    _need(raw, target, "asar", 3, 8, 4)
    string_size = _u32(raw, 8)
    _need(raw, target, "asar", 3, 12, 4)
    json_size = _u32(raw, 12)
    if string_size != header_size - 4:
        _fail(
            target,
            "asar",
            3,
            f"两段尺寸自相矛盾：头部 pickle 声明 {header_size:,}，"
            f"字符串 pickle 声明 {string_size:,}（应为 {header_size - 4:,}）",
        )
    if json_size > string_size - 4:
        _fail(
            target,
            "asar",
            3,
            f"元数据 JSON 声明 {json_size:,} 字节，装不下：字符串 pickle 载荷只有 {string_size - 4:,} 字节",
        )
    if 16 + json_size > len(raw):
        _fail(
            target,
            "asar",
            3,
            f"头部声明元数据 {json_size:,} 字节（需读到 {16 + json_size:,}），"
            f"文件总长只有 {len(raw):,} 字节",
        )

    body = raw[16 : 16 + json_size].rstrip(b"\x00")
    try:
        meta = json.loads(body.decode("utf-8"))
    except UnicodeDecodeError as error:
        _fail(target, "asar", 4, f"元数据不是合法 UTF-8：{error}")
    except json.JSONDecodeError as error:
        _fail(target, "asar", 4, f"元数据不是合法 JSON（第 {error.lineno} 行第 {error.colno} 列）：{error.msg}")
    if not isinstance(meta, dict) or not isinstance(meta.get("files"), dict):
        got = "顶层不是对象" if not isinstance(meta, dict) else f"没有对象形的 files 键（实际 {type(meta.get('files')).__name__}）"
        _fail(target, "asar", 4, f"元数据形状不对：asar 头部必须是 {{\"files\": {{…}}}}，{got}")

    data_start = 8 + header_size
    entries = _walk_asar_files(meta, "", target)
    for path, item in entries:
        if "files" in item:
            continue
        size = item.get("size")
        offset = item.get("offset")
        if not isinstance(size, int) or not isinstance(offset, str):
            _fail(
                f"{target}→{path}",
                "asar",
                4,
                f"文件条目必须带整数 size 与字符串 offset，实际 size={size!r}、offset={offset!r}",
            )
        try:
            start = int(offset)
        except ValueError:
            _fail(f"{target}→{path}", "asar", 4, f"offset 不是十进制整数：{offset!r}")
        if start < 0 or size < 0 or data_start + start + size > len(raw):
            _fail(
                f"{target}→{path}",
                "asar",
                5,
                f"条目在数据区的范围越界：数据区起点 {data_start:,} + 偏移 {start:,} + 长度 {size:,}"
                f" = {data_start + start + size:,}，文件总长 {len(raw):,} 字节",
            )
    return {"meta": meta, "entries": entries, "data_start": data_start}


def _read_asar_file(info: dict[str, Any], raw: bytes, wanted: str) -> bytes | None:
    for path, item in info["entries"]:
        if path == wanted or path.endswith("/" + wanted):
            start = info["data_start"] + int(item["offset"])
            return raw[start : start + int(item["size"])]
    return None


def _parse_asar(target: str, raw: bytes, findings: list[Finding]) -> dict[str, Any]:
    info = _asar_meta(target, raw)
    meta = info["meta"]
    files = [path for path, item in info["entries"] if "files" not in item]
    dirs = len({path.rsplit("/", 1)[0] for path in files if "/" in path})
    findings.append(
        Finding(
            "容器结构",
            f"{len(files)} 个文件 / {dirs} 个子目录",
            f"asar 数据区起点 {info['data_start']:,} 字节，元数据 {len(raw[16:16 + _u32(raw, 12)]):,} 字节",
        )
    )
    findings.append(
        Finding("文件表", ", ".join(files[:12]) or "（空表）", f"共 {len(files)} 条，界面只展开前 12 条")
    )

    package_raw = _read_asar_file(info, raw, "package.json")
    entry: dict[str, Any] = {}
    if package_raw is None:
        findings.append(
            Finding("入口线索", "未找到 package.json", "asar 表里没有这个条目；不能据此断言目标没有入口")
        )
    else:
        try:
            package = json.loads(package_raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            _fail(
                f"{target}→package.json",
                "asar",
                4,
                f"数据区里读出来的 package.json 不是合法 JSON：{error.__class__.__name__}：{error}",
            )
        main = str(package.get("main") or "")
        entry = {
            "package_name": str(package.get("name") or ""),
            "version": str(package.get("version") or ""),
            "main": main,
        }
        findings.append(
            Finding(
                "包声明",
                f"{entry['package_name']} {entry['version']}".strip(),
                "取自 asar 数据区里的 package.json（按表内 size/offset 读出）",
            )
        )
        if main:
            hit = next((path for path in files if path == main or path.endswith("/" + main)), "")
            where = f"在文件表第 {files.index(hit) + 1} 条找到" if hit else "文件表核对不中，声明与实际内容不一致"
            findings.append(
                Finding(
                    "入口" if hit else "入口线索",
                    main if hit else f"{main}（表里没有这个条目）",
                    f"package.json 的 main 字段声明入口；{where}",
                )
            )
        else:
            findings.append(Finding("入口线索", "package.json 没有 main 字段", "入口得从别处找（如 Electron 主进程参数）"))
    return entry


# --------------------------------------------------------------------------- #
# PE（Windows）
# --------------------------------------------------------------------------- #
def _parse_pe(target: str, raw: bytes, findings: list[Finding]) -> dict[str, Any]:
    _need(raw, target, "pe", 1, 0, 0x40)
    if raw[:2] != b"MZ":
        _fail(target, "pe", 1, f"MS-DOS 头幻数应为 4D 5A（MZ），实际是 {raw[:2].hex().upper()}")

    _need(raw, target, "pe", 2, 0x3C, 4)
    lfanew = _u32(raw, 0x3C)
    if lfanew < 0x40:
        _fail(target, "pe", 2, f"e_lfanew 指向 {lfanew:#x}，早于 MS-DOS 头结束位置 0x40")
    if lfanew + 24 > len(raw):
        _fail(
            target,
            "pe",
            2,
            f"e_lfanew 指向 {lfanew:#x}（PE 签名 + COFF 头要 24 字节，需读到 {lfanew + 24:,}），"
            f"文件总长只有 {len(raw):,} 字节",
        )

    _need(raw, target, "pe", 3, lfanew, 4)
    if raw[lfanew : lfanew + 4] != b"PE\x00\x00":
        _fail(target, "pe", 3, f"PE 签名应为 50 45 00 00，实际是 {raw[lfanew:lfanew + 4].hex().upper()}")

    _need(raw, target, "pe", 4, lfanew + 4, 20)
    machine, sections, _timestamp, _sym_ptr, _sym_count, opt_size, characteristics = struct.unpack_from(
        "<HHIIIHH", raw, lfanew + 4
    )
    if opt_size < 68:
        _fail(target, "pe", 4, f"可选头长度声明 {opt_size:,} 字节，不足 PE 可选头最小的 68 字节")
    section_table = lfanew + 24 + opt_size
    if sections == 0:
        _fail(target, "pe", 4, "COFF 头声明 0 个段：链接产物不可能没有段表")
    if section_table > len(raw):
        _fail(
            target,
            "pe",
            4,
            f"段表起点 {section_table:,} 已在文件之外（总长 {len(raw):,} 字节）",
        )
    findings.append(
        Finding(
            "目标类型",
            PE_MACHINES.get(machine, f"机器码 {machine:#06x}（未在册）"),
            f"COFF 头 characteristics={characteristics:#06x}，链接器时间戳 {_timestamp}",
        )
    )

    _need(raw, target, "pe", 5, lfanew + 24, 2)
    magic = _u16(raw, lfanew + 24)
    if magic not in PE_OPTIONAL_MAGICS:
        _fail(target, "pe", 5, f"可选头魔数应为 0x10B/0x20B/0x107，实际 {magic:#06x}")
    width = 64 if magic == 0x20B else 32
    _need(raw, target, "pe", 5, lfanew + 24 + 16, 4)
    entry_rva = _u32(raw, lfanew + 24 + 16)
    subsystem_at = lfanew + 24 + (68 if width == 32 else 84) - 2
    subsystem = _u16(raw, subsystem_at) if subsystem_at + 2 <= len(raw) else 0
    findings.append(
        Finding(
            "位数与入口",
            f"{PE_OPTIONAL_MAGICS[magic]}，入口 RVA {entry_rva:#x}",
            f"可选头长 {opt_size:,} 字节，子系统代码 {subsystem}（2=GUI，3=控制台）",
        )
    )

    _need(
        raw,
        target,
        "pe",
        6,
        section_table,
        sections * 40,
    )
    table: list[dict[str, int]] = []
    for index in range(sections):
        base = section_table + index * 40
        name = raw[base : base + 8].rstrip(b"\x00").decode("latin-1")
        virtual_size, virtual_address, raw_size, raw_pointer = struct.unpack_from("<IIII", raw, base + 8)
        table.append(
            {
                "index": index,
                "name": name,
                "virtual_size": virtual_size,
                "virtual_address": virtual_address,
                "raw_size": raw_size,
                "raw_pointer": raw_pointer,
            }
        )
    findings.append(
        Finding(
            "段表",
            ", ".join(row["name"] or f"#{row['index']}" for row in table[:12]),
            f"共 {sections} 个段，每个 40 字节，段表起点 {section_table:,}",
        )
    )

    # 段内容边界：声明了却没在文件里的，就是截断的产物
    for row in table:
        if row["raw_size"] == 0:
            continue
        end = row["raw_pointer"] + row["raw_size"]
        if end > len(raw):
            _fail(
                f"{target}#{row['name'] or row['index']}",
                "pe",
                7,
                f"段「{row['name']}」声明文件偏移 {row['raw_pointer']:,} 起 {row['raw_size']:,} 字节"
                f"（需读到 {end:,}），文件总长 {len(raw):,} 字节，缺 {end - len(raw):,} 字节",
            )

    entry_section = next(
        (
            row["name"]
            for row in table
            if row["virtual_address"] <= entry_rva < row["virtual_address"] + max(row["virtual_size"], 1)
        ),
        "",
    )
    return {
        "width": width,
        "entry_rva": entry_rva,
        "entry_section": entry_section,
        "sections": sections,
    }


# --------------------------------------------------------------------------- #
# ELF（Linux / Android 目标）
# --------------------------------------------------------------------------- #
def _parse_elf(target: str, raw: bytes, findings: list[Finding]) -> dict[str, Any]:
    _need(raw, target, "elf", 1, 0, 16)
    if raw[:4] != b"\x7fELF":
        _fail(target, "elf", 1, f"ELF 幻数应为 7F 45 4C 46，实际 {raw[:4].hex().upper()}")
    elf_class = raw[4]
    if elf_class not in (1, 2):
        _fail(target, "elf", 1, f"EI_CLASS 应为 1（32 位）或 2（64 位），实际 {elf_class}")
    data = raw[5]
    if data not in (1, 2):
        _fail(target, "elf", 2, f"EI_DATA 应为 1（小端）或 2（大端），实际 {data}")
    width = 64 if elf_class == 2 else 32
    endian = "little" if data == 1 else "big"
    prefix = "<" if data == 1 else ">"
    head_size = 64 if width == 64 else 52

    _need(raw, target, "elf", 3, 0, head_size)
    if width == 64:
        e_type, e_machine, _version, entry, _phoff, shoff = struct.unpack_from(prefix + "HHIQQQ", raw, 16)
        e_shentsize, e_shnum = struct.unpack_from(prefix + "HH", raw, 58)
    else:
        e_type, e_machine, _version, entry, _phoff, shoff = struct.unpack_from(prefix + "HHIIII", raw, 16)
        e_shentsize, e_shnum = struct.unpack_from(prefix + "HH", raw, 46)
    findings.append(
        Finding(
            "目标类型",
            f"{ELF_TYPES.get(e_type, f'类型 {e_type}')} / {ELF_MACHINES.get(e_machine, f'机器码 {e_machine:#x}')}",
            f"ELF{width}，{endian}端，入口 {entry:#x}",
        )
    )
    expected_entry = 40 if width == 32 else 64
    if e_shentsize not in (40, 64):
        _fail(
            target,
            "elf",
            4,
            f"段表条目长度 e_shentsize 应为 40（ELF32）或 64（ELF64），实际 {e_shentsize}",
        )
    if e_shentsize != expected_entry:
        _fail(
            target,
            "elf",
            4,
            f"段表条目长度与位宽不符：ELF{width} 应为 {expected_entry}，实际 {e_shentsize}",
        )
    if e_shnum == 0 and shoff != 0:
        _fail(target, "elf", 4, f"段表偏移 {shoff:,} 非零，却声明 0 个段条目")

    total = shoff + int(e_shentsize) * int(e_shnum)
    if shoff and total > len(raw):
        _fail(
            target,
            "elf",
            5,
            f"段表声明 {e_shnum} 条 × {e_shentsize} 字节，起点 {shoff:,}，需读到 {total:,}，"
            f"文件总长 {len(raw):,} 字节，缺 {total - len(raw):,} 字节",
        )
    return {
        "width": width,
        "endianness": endian,
        "entry_rva": entry,
        "sections": e_shnum,
    }


# --------------------------------------------------------------------------- #
# 对外入口
# --------------------------------------------------------------------------- #
def load_target(root: str, relative: str, *, limit: int = MAX_TARGET_BYTES) -> tuple[str, bytes]:
    """在授权根内读出目标字节。返回 (对外名字, 内容)。

    名字只到「相对路径」这一层：宿主绝对路径一旦进 finding 或报错文本，就等于把
    用户目录结构端给了前端——这条链路的脱敏口径与项目其它处一致（``<workspace:名字>``）。
    """
    from pathlib import Path

    from blank_api.api.errors import ApiError
    from blank_api.services.workspace import resolve_inside

    try:
        path = resolve_inside(Path(root), relative)
    except ApiError as error:
        raise TargetUnavailable(str(error)) from error
    if path.is_dir():
        raise TargetUnavailable(f"目标是目录（{relative}），REA 这一层只解析单文件产物")
    size = path.stat().st_size
    if size > limit:
        raise TargetUnavailable(
            f"目标 {size:,} 字节，超过单次解析上限 {limit:,} 字节；这一层只读头部，请用切片后的样本"
        )
    # 对外名字用相对路径的正斜杠形状，绝不带盘符与反斜杠
    name = path.relative_to(Path(root).resolve()).as_posix() or path.name
    return name, path.read_bytes()


def inspect_bytes(target: str, raw: bytes) -> dict[str, Any]:
    """真解析：幻数认领 → 逐步读头部 → 一组可核对的发现。

    失败只抛两种：``UnsupportedFormat``（没能力看）与 ``ArtifactParseError``（看见了但读不下去，
    指名第几步）。两者都不是「没有结果」。
    """
    format_name = identify(raw)
    if not format_name:
        raise UnsupportedFormat(target=target, head=raw)

    findings: list[Finding] = []
    if format_name == "asar":
        entry = _parse_asar(target, raw, findings)
    elif format_name == "pe":
        entry = _parse_pe(target, raw, findings)
    else:
        entry = _parse_elf(target, raw, findings)

    findings.insert(
        0,
        Finding(
            "实物指纹",
            f"{format_name}，{len(raw):,} 字节",
            f"sha256 {hashlib.sha256(raw).hexdigest()[:16]}，首部 {raw[:8].hex().upper() or '空'}",
        ),
    )
    return {
        "target": target,
        "format": format_name,
        "size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "head_hex": raw[:16].hex().upper(),
        "steps": list(STEPS[format_name]),
        "entry": entry,
        "findings": [item.as_dict() for item in findings],
        "limitations": [
            "这一层只读文件头部与元数据，没有反汇编、没有运行目标，也看不到加壳/加密后的正文",
        ],
    }
