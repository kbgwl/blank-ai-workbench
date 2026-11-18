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
import inspect
import json
import os
import time
from dataclasses import dataclass, field

from blank_api.services.models.base import ChatMessage, ModelAdapter, ModelResponse

GUARDIAN_SYSTEM = (
    "你是命令行与文件操作的安全审查员。你会收到一个即将执行的工具调用，"
    "其中包含工作区根目录、风险等级、是否可回滚与参数。"
    "请判断它是否会不可逆地破坏数据、泄露凭据、推送代码到远端或越出工作区。"
    "只输出 JSON：{\"verdict\": \"allow|deny\", \"reasons\": [\"一句话原因\"]}。"
    "按范围分档，别把正常的开发动作当灾难："
    "① 删除/覆盖的目标在工作区根目录之内、路径写死（如 ./build、src/x.py、dist/*.map）→ allow："
    "那是用户交代的活，交给审批卡陪他决定，审查员不该替他拒绝；"
    "② 目标在工作区之外、家目录、根目录、系统盘，或靠通配符/环境变量展开 → deny；"
    "③ 覆盖密钥与凭据文件、force push、格式化磁盘、改系统服务/注册表 → deny；"
    "④ 拿不准就选 allow，把决定权交给人。"
)
MAX_ARGUMENT_CHARS = 1_200
DEFAULT_TIMEOUT = 8.0
# 审查用的思考档位。审查的输出只是 allow / deny / abstain 加一句理由，
# 与编程任务里"想清楚再动手"不是一件事；用默认的 high 档在这里换不到任何判断力。
GUARDIAN_EFFORT = "low"


def _accepts_effort(adapter: ModelAdapter | None) -> bool:
    """这个适配器的 invoke 收不收 ``effort``。

    不看适配器类型、看签名：以后新增的适配器只要加了形参就自动用上，没加的
    也不会因为多传一个 kwarg 而抛 TypeError（async 函数的 TypeError 要到 await
    时才出，捕获点会离出错处很远）。
    """
    if adapter is None:
        return False
    try:
        return "effort" in inspect.signature(adapter.invoke).parameters
    except (TypeError, ValueError):
        return False
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
    # 这次审查的用量是供应商亲口说的，还是没拿到。不带上它，落账时只能填默认
    # "unknown"——审查员明明回了 usage，账上却记成「没说用了多少」，按 provider
    # 拆账时审查员的 token 会整块掉进 unknown 桶。
    usage_source: str = "unknown"

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
            "usage_source": self.usage_source,
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
        effort: str = GUARDIAN_EFFORT,
    ) -> None:
        self.adapter = adapter
        self.mode = mode if mode in MODES else "veto"
        self.timeout = timeout
        # 审查是一次二分类，不是推理题。以前这里不传档位，适配器就按本轮的默认档
        # （high）发出去：实测一条 run_shell 的审查稳定花 5-8 秒，还有一半直接顶到
        # 8 秒超时、退化成「问人」。档位压到 low 之后同样的判定一两秒就回来。
        self.effort = effort
        self._sends_effort = _accepts_effort(adapter)

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
        self,
        tool: str,
        arguments: dict,
        *,
        risk: str = "",
        reversible: bool = True,
        root: str = "",
    ) -> GuardianVerdict:
        if not self.enabled:
            return GuardianVerdict()
        arguments_preview = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
        if len(arguments_preview) > MAX_ARGUMENT_CHARS:
            arguments_preview = arguments_preview[:MAX_ARGUMENT_CHARS] + "…（参数已截断）"
        # 工作区根必须给：没有它，审查员分不清 `rm -rf ./build`（用户交代的活）与
        # `rm -rf ~`（灾难），只能一律拒绝——上一版就是这么把所有删除都按死的，
        # 结果是模型永远删不掉东西，审批卡也永远不出现（真机 3622e0e7）。
        scope_line = f"工作区根：{root}\n" if root else ""
        messages = [
            ChatMessage(role="system", content=GUARDIAN_SYSTEM),
            ChatMessage(
                role="user",
                content=(
                    f"工具：{tool}\n风险等级：{risk or 'unknown'}\n"
                    f"可回滚：{'是' if reversible else '否'}\n"
                    f"{scope_line}"
                    f"参数：{arguments_preview}"
                ),
            ),
        ]
        started = time.perf_counter()
        call_kwargs: dict = {"effort": self.effort} if self._sends_effort else {}
        try:
            response: ModelResponse = await asyncio.wait_for(
                self.adapter.invoke(messages, **call_kwargs), timeout=self.timeout
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
            usage_source=response.usage.source,
        )
        if result.allowed and not reversible:
            # 不可逆动作不接受模型的"放行"，只能由人来决定。
            result.verdict = "abstain"
            result.reasons = [*result.reasons, "不可逆操作，审查意见仅作参考，仍需人工确认"]
        return result

    def can_auto_approve(self, verdict: GuardianVerdict, risk: str = "") -> bool:
        """代批只在"低风险但需要询问"这一档生效。

        auto 档的本意是省掉那些"问了也白问"的卡，不是替人按下高危动作的确认键。
        旧实现只看 mode 与 verdict，风险等级一个字都没看：真机（1f17123a）里
        `rm -rf ./x` 被判 high，审查员回 allow，于是被静默代批、直接执行——
        卡没出，用户事后连"它删过东西"都无从看见。高风险是用户唯一想亲眼看见的
        那一档，这一档必须留在人手里。
        """
        return self.mode == "auto" and verdict.allowed and risk not in ("high", "critical")
