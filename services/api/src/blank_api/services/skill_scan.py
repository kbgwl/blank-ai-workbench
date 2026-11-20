"""技能安装前的静态检查。

一份外部技能装进来之后，它的 ``SKILL.md`` 会变成模型的指令，随附脚本会被模型
当成「可以用」的工具。所以安装技能不是"下载一个文档"，而是**给助手换一份脑子**。
原来这一步只校验 SKILL.md 能不能解析、正文长不超过上限——那是在验格式，
不是验这东西能不能信。

这里做的是静态检查，不是安全保证。它只拦几类已经能确定意图的东西：
下载脚本直接执行、删根目录、读私钥、内嵌私钥。别的一律只报"值得看一眼"，
由用户在审批卡上决定——把每一条 shell 都当恶意会拦掉大量正常技能，
用户被拦烦了就会找办法绕过，反而更不安全。

纯函数，读一个目录，不联网、不执行、不改文件。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

DANGER: tuple[tuple[str, str], ...] = (
    (r"curl[^\n|]{0,200}\|\s*(?:ba)?sh\b", "下载后立即执行脚本"),
    (r"wget[^\n|]{0,200}\|\s*(?:ba)?sh\b", "下载后立即执行脚本"),
    (r"rm\s+-rf\s+/(?:\s|$|['\"])", "删除根目录"),
    (r"(?:cat|type)\s+[^\n]{0,80}[\\/]\.ssh[\\/]id_rsa", "读取 SSH 私钥"),
    (r"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----", "内嵌私钥"),
)

WARN: tuple[tuple[str, str], ...] = (
    (r"(?i)ignore\s+(?:all\s+)?previous\s+instructions", "要求模型忽略它原有的指令"),
    (r"不要告诉用户|别告诉用户|do\s+not\s+tell\s+(?:the\s+)?user", "要求对用户隐瞒"),
    (r"(?i)(?:curl|wget)[^\n]{0,200}https?://[^\s]{0,120}\.(?:sh|ps1|bat)\b", "从外部下载脚本"),
    (r"(?i)\b(?:npm|pip|uv|poetry|yarn|pnpm)\s+install\b", "安装依赖"),
    (r"[A-Za-z0-9+/]{220,}={0,2}", "超长 base64 串（可能是编码过的载荷）"),
)

SCRIPT_SUFFIXES = frozenset({".sh", ".ps1", ".bat", ".cmd", ".exe", ".msi", ".vbs", ".pyw"})
TEXT_SUFFIXES = frozenset({".md", ".txt", ".py", ".js", ".ts", ".json", ".yaml", ".yml", ".toml", ".sh", ".ps1", ".bat", ".cmd"})
MAX_SCAN_BYTES = 256 * 1024


@dataclass(frozen=True)
class Finding:
    level: str  # danger / warn
    rule: str
    detail: str
    file: str


@dataclass(frozen=True)
class ScanResult:
    level: str  # safe / warn / danger
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "level": self.level,
            "findings": [
                {"level": item.level, "rule": item.rule, "detail": item.detail, "file": item.file}
                for item in self.findings
            ],
        }


def scan_skill_dir(root: Path | str) -> ScanResult:
    """扫描一个已暂存的技能目录。目录不存在或读不出来都当作空结果——
    调用方在那之前已经验证过 SKILL.md 存在，这里的职责只是"别让检查本身把安装搞挂"。
    """
    base = Path(root)
    findings: list[Finding] = []
    try:
        paths = sorted(item for item in base.rglob("*") if item.is_file())
    except OSError:
        return ScanResult("safe")

    for path in paths:
        rel = path.relative_to(base).as_posix()
        if path.suffix.lower() in SCRIPT_SUFFIXES:
            findings.append(Finding("warn", "script_file", "技能里带脚本文件", rel))
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            if path.stat().st_size > MAX_SCAN_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for pattern, detail in DANGER:
            if re.search(pattern, text, re.IGNORECASE | re.MULTILINE):
                findings.append(Finding("danger", "dangerous_pattern", detail, rel))
        for pattern, detail in WARN:
            if re.search(pattern, text, re.IGNORECASE | re.MULTILINE):
                findings.append(Finding("warn", "suspicious_pattern", detail, rel))

    # 同一条规则在同一个文件里命中十次也只报一次：用户要的是"有这个问题"，
    # 不是"这个问题出现了十次"。十条重复项会把真正的那一条也淹没。
    unique = tuple(dict.fromkeys(findings))
    level = "danger" if any(item.level == "danger" for item in unique) else "warn" if unique else "safe"
    return ScanResult(level, unique)


def scan_summary(result: ScanResult) -> str:
    """给用户看的一句话。没有发现时留空——成功安装后面再补一句"没发现问题"
    只会让人误以为真做过什么彻底的检查。"""
    if not result.findings:
        return ""
    danger = sum(1 for item in result.findings if item.level == "danger")
    warn = len(result.findings) - danger
    parts = []
    if danger:
        parts.append(f"{danger} 项高危")
    if warn:
        parts.append(f"{warn} 项需要留意")
    return "；".join(parts)
