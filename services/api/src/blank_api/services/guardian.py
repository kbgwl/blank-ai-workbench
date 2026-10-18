"""Guardian: an independent reviewer for risky actions.

Codex runs a separate review pass with its own model, reasoning effort and
timeout before a dangerous command is executed. The point is not to replace the
human — it is to catch the obvious mistakes (``rm -rf ~``, pushing to main,
writing secrets) before the user is even asked, and to give the approval card a
reason instead of a bare command.

Two rules keep it safe:

* **Fail-open, never fail-silent.** If the guardian model times out, errors or
  says something unparsable, the verdict is ``abstain`` and the normal approval
  path runs. A broken reviewer must not block work.
* **It can only tighten.** ``veto`` mode (default) may deny but never approve.
  ``auto`` mode may approve only low-risk-but-asking cases; irreversible
  commands always go to the human regardless of what the model says.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field

from blank_api.services.models.base import ChatMessage, ModelAdapter, ModelResponse

GUARDIAN_SYSTEM = (
    "你是命令行与文件操作的安全审查员。你会收到一个即将执行的工具调用，"
    "请判断它是否会不可逆地破坏数据、泄露凭据、推送代码到远端或越出工作区。"
    "只输出 JSON：{\"verdict\": \"allow|deny\", \"reasons\": [\"一句话原因\"]}。"
    "不确定就选 allow，把决定权交给人；但看到删除、覆盖密钥、force push、"
    "磁盘格式化这类动作必须选 deny。"
)
MAX_ARGUMENT_CHARS = 1_200
DEFAULT_TIMEOUT = 8.0
MODES = ("off", "veto", "auto")


@dataclass
class GuardianVerdict:
    verdict: str = "abstain"  # allow | deny | abstain
    reasons: list[str] = field(default_factory=list)
    model: str = ""
    latency_ms: int = 0
    timed_out: bool = False
    error: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def denied(self) -> bool:
        return self.verdict == "deny"

    @property
    def allowed(self) -> bool:
        return self.verdict == "allow"

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "reasons": list(self.reasons),
            "model": self.model,
            "latency_ms": self.latency_ms,
            "timed_out": self.timed_out,
            "error": self.error,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }


def parse_verdict(text: str) -> tuple[str, list[str]]:
    """Tolerant parse: a model that writes prose must not silently become "allow"."""
    stripped = (text or "").strip()
    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            raw = str(parsed.get("verdict") or parsed.get("decision") or "").strip().lower()
            reasons = parsed.get("reasons") or parsed.get("reason") or []
            if isinstance(reasons, str):
                reasons = [reasons]
            clean = [str(item).strip() for item in reasons if str(item).strip()][:6]
            if raw in ("allow", "deny"):
                return raw, clean
            if raw in ("approve", "ok", "pass"):
                return "allow", clean
            if raw in ("block", "reject", "forbid"):
                return "deny", clean
    lowered = stripped.lower()
    if "deny" in lowered or "拒绝" in stripped:
        return "deny", [stripped[:200]] if stripped else []
    return "abstain", []


class Guardian:
    def __init__(
        self,
        adapter: ModelAdapter | None = None,
        *,
        mode: str = "veto",
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.adapter = adapter
        self.mode = mode if mode in MODES else "veto"
        self.timeout = timeout

    @classmethod
    def from_env(cls, adapter: ModelAdapter | None = None, raw: str | None = None) -> "Guardian":
        value = (raw if raw is not None else os.getenv("BLANK_GUARDIAN", "")).strip().lower()
        if not value or value in ("0", "off", "false", "no"):
            return cls(None, mode="off")
        mode = "auto" if value in ("auto", "approve") else "veto"
        try:
            timeout = float(os.getenv("BLANK_GUARDIAN_TIMEOUT", DEFAULT_TIMEOUT))
        except ValueError:
            timeout = DEFAULT_TIMEOUT
        return cls(adapter, mode=mode, timeout=timeout)

    @classmethod
    def from_settings(
        cls,
        adapter: ModelAdapter | None,
        *,
        settings: dict | None = None,
        store=None,
    ) -> "Guardian":
        """设置页是唯一真源；没有设置文件时才回落到环境变量。

        桌面版用户改的是设置页，不该被一个残留的 BLANK_GUARDIAN 环境变量悄悄覆盖；
        反过来，无设置文件的 CI / 无头环境仍然沿用环境变量，行为不变。
        """
        from blank_api.services.settings import SettingsStore, guardian_settings

        store = store or SettingsStore()
        try:
            exists = store.path.exists()
        except OSError:
            exists = False
        # 文件存在但里面没有 guardian 段（例如只配了安全策略）时，这一段仍然算"没配过"：
        # 此时环境变量还是那个显式开关，不该被 normalize 出来的默认 "off" 顶掉。
        configured = exists and store.has_section("guardian")
        if not configured and os.getenv("BLANK_GUARDIAN") is not None:
            return cls.from_env(adapter)
        config = guardian_settings(settings)
        mode = str(config.get("mode") or "veto")
        timeout = float(config.get("timeout_seconds") or DEFAULT_TIMEOUT)
        if mode == "off":
            return cls(None, mode="off", timeout=timeout)
        return cls(adapter, mode=mode, timeout=timeout)

    @property
    def enabled(self) -> bool:
        return self.adapter is not None and self.mode != "off"

    async def review(
        self, tool: str, arguments: dict, *, risk: str = "", reversible: bool = True
    ) -> GuardianVerdict:
        if not self.enabled:
            return GuardianVerdict()
        arguments_preview = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
        if len(arguments_preview) > MAX_ARGUMENT_CHARS:
            arguments_preview = arguments_preview[:MAX_ARGUMENT_CHARS] + "…（参数已截断）"
        messages = [
            ChatMessage(role="system", content=GUARDIAN_SYSTEM),
            ChatMessage(
                role="user",
                content=(
                    f"工具：{tool}\n风险等级：{risk or 'unknown'}\n"
                    f"可回滚：{'是' if reversible else '否'}\n参数：{arguments_preview}"
                ),
            ),
        ]
        started = time.perf_counter()
        try:
            response: ModelResponse = await asyncio.wait_for(
                self.adapter.invoke(messages), timeout=self.timeout
            )
        except asyncio.TimeoutError:
            return GuardianVerdict(
                timed_out=True,
                latency_ms=int((time.perf_counter() - started) * 1000),
                error=f"审查超时（{self.timeout:.0f}s）",
            )
        except Exception as exc:  # 审查失败一律 abstain，绝不因此卡住任务
            return GuardianVerdict(
                latency_ms=int((time.perf_counter() - started) * 1000),
                error=exc.__class__.__name__,
            )
        verdict, reasons = parse_verdict(response.text)
        result = GuardianVerdict(
            verdict=verdict,
            reasons=reasons,
            model=response.model or self.adapter.config.model,
            latency_ms=int((time.perf_counter() - started) * 1000),
            prompt_tokens=response.usage.prompt_tokens,
            completion_tokens=response.usage.completion_tokens,
        )
        if result.allowed and not reversible:
            # 不可逆动作不接受模型的"放行"，只能由人来决定。
            result.verdict = "abstain"
            result.reasons = [*result.reasons, "不可逆操作，审查意见仅作参考，仍需人工确认"]
        return result

    def can_auto_approve(self, verdict: GuardianVerdict) -> bool:
        return self.mode == "auto" and verdict.allowed
