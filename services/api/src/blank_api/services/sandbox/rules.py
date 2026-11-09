"""受限令牌后端的规则文件：只有列进来的根可写/可读，其余一律按 NTFS 默认拒绝。

为什么要有文件：受限令牌本身只回答"这个身份能不能碰"。可用的沙箱还需要回答"工作区在
哪、工具链在哪、哪些目录绝对不能进"。把这些写死在代码里，换一台机器（uv 装 Python、
npm 全局目录在 %APPDATA%）就得改代码；写成文件，部署方能在不动执行层的前提下补齐。

三条纪律：
1. **默认拒绝**：`default_action` 只接受 `deny`。写 `allow` 的配置直接报错，不静默放行。
2. **保护路径不许被授权覆盖**：规则里任何一条可写/可读根只要包含 `protected` 里的路径，
   整份规则作废。否则「工作区设成家目录」会让 `~/.ssh` 随着工作区授权一起被读。
3. **认不出的字段直接报错**：拼错 `protected_paths` 就少了一层保护，静默忽略比没有更危险。
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_SID = "logon"  # 见 windows_restricted.capability SID：本机实测 logon SID 才能通过限制检查
_RULE_FILE = Path(__file__).with_name("rules.default.json")
_KEYS = {"version", "capability_sid", "default_action", "write_allow", "read_allow", "protected"}
_PLACEHOLDER = re.compile(r"\{[a-z_]+\}")

_DEFAULT_WRITE = ("{workspace}",)
_DEFAULT_READ = ("{python_base}", "{python_prefix}", "{appdata}/npm")
_DEFAULT_PROTECTED = (
    "{home}/.ssh",
    "{home}/.gnupg",
    "{home}/.aws",
    "{home}/.kube",
    "{home}/.netrc",
    "{home}/.config/gcloud",
    "{home}/.docker/config.json",
)


class RulesError(RuntimeError):
    """规则文件不可用，或一条授权会覆盖受保护路径。调用方必须拒绝执行，不许猜。"""


@dataclass(frozen=True, slots=True)
class SandboxRules:
    version: int
    capability_sid: str
    default_action: str
    write_allow: tuple[str, ...]
    read_allow: tuple[str, ...]
    protected: tuple[str, ...]
    source: str = "builtin"

    def context(self, workspace: str, temp: str = "") -> dict[str, str]:
        home = os.environ.get("USERPROFILE") or str(Path.home())
        appdata = os.environ.get("APPDATA") or ""
        return {
            "workspace": os.path.abspath(workspace),
            "temp": os.path.abspath(temp) if temp else "",
            "home": os.path.abspath(home),
            "python_base": os.path.abspath(sys.base_prefix),
            "python_prefix": os.path.abspath(sys.prefix),
            "appdata": os.path.abspath(appdata) if appdata else "",
            "localappdata": os.path.abspath(value) if (value := os.environ.get("LOCALAPPDATA")) else "",
        }

    def write_roots(self, ctx: dict[str, str]) -> tuple[str, ...]:
        # 工作区永远可写，不从配置里拿掉：否则规则文件一改，agent 连自己的产物都写不出来。
        roots = _resolve_all(("{workspace}", *self.write_allow), ctx)
        return _dedupe(roots)

    def read_roots(self, ctx: dict[str, str]) -> tuple[str, ...]:
        return _dedupe(_resolve_all(self.read_allow, ctx))

    def protected_paths(self, ctx: dict[str, str]) -> tuple[str, ...]:
        return _dedupe(_resolve_all(self.protected, ctx))

    def guard(self, roots: tuple[str, ...], ctx: dict[str, str]) -> None:
        """任何授权根包含受保护路径 ⇒ 整份规则作废（不是"跳过这一条"）。"""
        for root in roots:
            for protected in self.protected_paths(ctx):
                if _contains(root, protected):
                    raise RulesError(
                        f"规则会把受保护路径 {protected} 一起授权出去（授权根 {root}）；"
                        "请收窄 write_allow/read_allow，绝不授权家目录本身"
                    )

    def as_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "capability_sid": self.capability_sid,
            "default_action": self.default_action,
            "write_allow": list(self.write_allow),
            "read_allow": list(self.read_allow),
            "protected": list(self.protected),
            "source": self.source,
        }


def load(path: str | Path | None = None) -> SandboxRules:
    """读规则文件。显式给路径就必须成功；没给就用包内默认文件，文件丢了退回内置常量。"""
    if path:
        target = Path(path)
        if not target.is_file():
            raise RulesError(f"规则文件不存在：{target}")
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RulesError(f"规则文件读不了：{target}：{error}") from error
        return _parse(data, source=str(target))
    if _RULE_FILE.is_file():
        try:
            data = json.loads(_RULE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RulesError(f"内置规则文件损坏：{_RULE_FILE}：{error}") from error
        return _parse(data, source=str(_RULE_FILE))
    return SandboxRules(
        version=1,
        capability_sid=DEFAULT_SID,
        default_action="deny",
        write_allow=_DEFAULT_WRITE,
        read_allow=_DEFAULT_READ,
        protected=_DEFAULT_PROTECTED,
        source="builtin",
    )


def _parse(data: object, *, source: str) -> SandboxRules:
    if not isinstance(data, dict):
        raise RulesError(f"规则文件顶层必须是对象：{source}")
    unknown = set(data) - _KEYS
    if unknown:
        raise RulesError(f"规则文件有认不出的字段：{sorted(unknown)}（拼错的保护项会被静默忽略）")
    version = data.get("version")
    if version != 1:
        raise RulesError(f"规则版本不认识：{version!r}（本代码只认 1）")
    sid = str(data.get("capability_sid") or DEFAULT_SID).strip()
    if sid.lower() != "logon" and not sid.startswith("S-1-5-"):
        raise RulesError(f"capability_sid 只接受 logon 或 S-1-5-* 形式：{sid!r}")
    action = str(data.get("default_action") or "").strip().lower()
    if action != "deny":
        raise RulesError(f"default_action 只允许 deny（当前 {action!r}）：默认放行的规则不是边界")

    def _strings(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
        raw = data.get(name, default)
        if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
            raise RulesError(f"{name} 必须是字符串数组")
        return tuple(item.strip() for item in raw if item.strip())

    return SandboxRules(
        version=1,
        capability_sid=sid,
        default_action=action,
        write_allow=_strings("write_allow", _DEFAULT_WRITE),
        read_allow=_strings("read_allow", _DEFAULT_READ),
        protected=_strings("protected", _DEFAULT_PROTECTED),
        source=source,
    )


def _expand(template: str, ctx: dict[str, str]) -> str:
    """展开占位符。占位符为空就整条作废——`{appdata}/npm` 在 APPDATA 为空时绝不能
    退化成盘根下的 `\\npm`，那会把一个不存在的目录当成合法读根。"""
    text = template
    for key, value in ctx.items():
        token = "{" + key + "}"
        if token not in text:
            continue
        if not value:
            return ""
        text = text.replace(token, value)
    leftover = _PLACEHOLDER.search(text)
    if leftover:
        raise RulesError(f"规则里有不认识的占位符：{leftover.group(0)}（模板 {template!r}）")
    return os.path.abspath(os.path.expandvars(os.path.expanduser(text)))


def _resolve_all(templates: tuple[str, ...], ctx: dict[str, str]) -> list[str]:
    out: list[str] = []
    for template in templates:
        value = _expand(template, ctx)
        if value:
            out.append(value)
    return out


def _dedupe(values: list[str]) -> tuple[str, ...]:
    seen: list[str] = []
    keyed: set[str] = set()
    for value in values:
        key = os.path.normcase(os.path.normpath(value))
        if key in keyed:
            continue
        keyed.add(key)
        seen.append(value)
    return tuple(seen)


def _contains(root: str, target: str) -> bool:
    """root 是否等于 target 或包含 target。用 normcase 比较，Windows 上大小写不敏感。"""
    try:
        root_key = os.path.normcase(os.path.normpath(os.path.abspath(root)))
        target_key = os.path.normcase(os.path.normpath(os.path.abspath(target)))
        if target_key == root_key:
            return True
        prefix = root_key if root_key.endswith(os.sep) else root_key + os.sep
        return target_key.startswith(prefix)
    except OSError:
        return False


__all__ = ["DEFAULT_SID", "RulesError", "SandboxRules", "load"]
