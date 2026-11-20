"""终态码：这一轮为什么结束。纯函数，不读库、不调模型、不看 Agent 自述。

为什么要单独一个模块：``runtime._finish`` 与 ``worker`` 的审批过期收敛是**两条**
终态写入路径，两边都要产出同一个结构。放 runtime 里会逼 worker 反向依赖整个运行时，
放这里两边都只是 import 一个纯函数（它唯一的外部依赖是 verifier 的码表常量）。

与 ``agent/verifier.py`` 的分工：verifier 回答"事实对不对"（findings + codes），
这里回答"这一轮以什么方式结束"（terminal code）。后者包含前者——verification_blocked
的 ``detail`` 就是 verifier 给出的那个硬违规码。它只依赖 verifier 的码表
（``BLOCKING_CODES``），不依赖 verifier 的判定逻辑。
"""

from __future__ import annotations

from blank_api.services.agent.verifier import BLOCKING_CODES

# 终态码：这一轮**为什么**结束。封闭集合，界面与验收按码判断，不读中文。
# 命名对齐 LoopForge 的 terminal detail / qwen-code 的 TaskTerminalReasonDetail，
# 但只覆盖本仓库真实存在的分支——写一个没人置位的码，等于给下游一个永远猜错的枚举。
TERMINAL_CODES = (
    "completed",             # 跑完且事实校验没拦
    "completed_unverified",  # 跑完，但自动化校验没跑成（交付文本已如实标注）
    "waiting_approval",      # 停在审批门上
    "waiting_answer",        # 停在提问上（等用户回答）
    "cancelled",
    "paused",
    # 审批卡放太久没人处理。它不走 _finish（worker 直接收敛），是唯一一条
    # 绕开运行时收尾的终态路径，所以码必须在这里登记，否则 worker 写出来的
    # 还是一句裸中文。
    "approval_expired",
    "budget_exhausted",      # 步数/预算耗尽
    "verification_blocked",  # 独立事实校验发现硬违规（detail 给具体 finding 码）
    "validation_failed",     # 测试跑了并没通过
    "validation_timeout",    # 测试超时（≠ 不通过）
    "validation_error",      # 测试没能执行
    "validation_blocked",    # 反复跑同一条命令没新结果
    "validation_skipped",    # 这次没跑校验
    "hook_blocked",          # 提交被钩子拦下
    "model_error",           # 模型侧错误（detail 给供应商码）
    "failed",                # 兜底：确实失败了但没有更精确的码
)


def terminal_detail(status: str, reason: str, extra: dict | None = None) -> dict[str, str]:
    """把「这一轮为什么结束」折成封闭枚举，附一句人话。

    过去这个信息只有一个 ``reason``：有时是码（``approval_expired``），有时是一整句
    中文，有时是模型的报错原文。界面于是只能拿正则去猜
    （``web/src/components/tasks/error-copy.ts`` 的 ``BY_TEXT``）——同一件事换个说法
    就认不出来，猜错了还会给用户一句错的建议。

    所以在这一处（唯一的终态点）把事实翻成码：``code`` 给机器，``reason`` 给人。
    纯函数、只读入参，不写库不调模型——终态判定必须能被重复计算，重放时才不会漂。
    """
    extra = extra or {}
    state = str(status or "")
    if state == "succeeded":
        # unverified 不是失败，但"跑完了"和"跑完了且验过"对用户是两件事，
        # 混成一个码会让界面把未验证的成功画成绿的。人话取 `unverified` 那句：
        # 成功的 reason 是空的，缺了它界面只能写"完成"却说不清为什么没验。
        if extra.get("unverified"):
            return {"code": "completed_unverified", "reason": str(extra.get("unverified"))}
        return {"code": "completed", "reason": str(reason or "")}
    if state == "waiting_approval":
        interrupt = extra.get("interrupt") or {}
        kind = str((interrupt or {}).get("kind") or "") if isinstance(interrupt, dict) else ""
        return {"code": "waiting_answer" if kind == "question" else "waiting_approval", "reason": str(reason or "")}
    if state in ("cancelled", "paused"):
        return {"code": state, "reason": str(reason or "")}

    # 调用方明确知道自己为什么结束时，直接采信。这一档存在的意义是别让下面的
    # 推断去猜：钩子拦下的 reason 是一整句中文，靠"含不含'钩子'两个字"判断，
    # 用户改一个字就失效——配置事实应当由配置侧声明。
    explicit = str(extra.get("terminal_code") or "")
    if explicit in TERMINAL_CODES:
        return {"code": explicit, "reason": str(reason or "")}

    # 有的收尾路径把码直接写在 reason 里（worker 的 `approval_expired` 就是这样）。
    # 认得出就直接用：它比后面所有推断都精确，而且不必逼那几处改签名。
    bare = str(reason or "").strip()
    if bare in TERMINAL_CODES:
        return {"code": bare, "reason": bare}

    # 失败侧按"证据强度"排序：模型错误是外部事实，verdict 是磁盘事实，
    # validation 是命令事实，钩子是配置事实。先说最硬的那个。
    model_code = str(extra.get("code") or "")
    if model_code and model_code not in TERMINAL_CODES:
        return {"code": "model_error", "detail": model_code, "reason": str(reason or "")}
    verdict = extra.get("verdict") or {}
    if isinstance(verdict, dict) and verdict.get("blocking"):
        codes = [str(item) for item in (verdict.get("codes") or []) if str(item)]
        # 必须挑**硬违规**码：一张 verdict 的 codes 里，第一个往往是
        # `no_files_changed`（"本轮没有改动"只是一句信息）。直接取 codes[0]
        # 会把"零改动伪成功"说成"没有任何改动"，界面据此给的下一步也是错的。
        blocking_codes = [item for item in codes if item in BLOCKING_CODES]
        detail = blocking_codes[0] if blocking_codes else (codes[0] if codes else "")
        return {
            "code": "verification_blocked",
            "detail": detail,
            "reason": str(reason or ""),
        }
    if extra.get("budget_exhausted"):
        return {"code": "budget_exhausted", "reason": str(reason or "")}
    validation = extra.get("validation") or {}
    if isinstance(validation, dict):
        val_state = str(validation.get("status") or "")
        by_state = {
            "timeout": "validation_timeout",
            "error": "validation_error",
            "blocked": "validation_blocked",
            "skipped": "validation_skipped",
        }
        if val_state in by_state:
            return {"code": by_state[val_state], "reason": str(reason or "")}
        if val_state and val_state != "ok":
            return {"code": "validation_failed", "reason": str(reason or "")}
        if validation.get("exit_code") not in (None, 0):
            return {"code": "validation_failed", "reason": str(reason or "")}
    return {"code": "failed", "reason": str(reason or "")}


# ── 取消 / 暂停事件的载荷 ─────────────────────────────────────────────────
# 取消不经过 ``runtime._finish``，所以它有**四处**独立写入点（runtime 开跑前发现已取消、
# 执行节点进入时发现已取消、执行中工具被中断、以及 _finish 的映射表）。四处各自手写
# ``{"reason", "terminal"}`` 时，漏一处不会有任何测试红——而 ``_finish`` 见到已有终态
# 会跳过自己那条，于是用户看到的恰好是没码的那个事件。
#
# 抽成函数之后，"取消的载荷长什么样"只有一个答案。判据分两层：函数本身的形状，
# 以及真实分支确实调用了它（见 ``tests/test_terminal_payload_v433.py``）。
def cancelled_payload(reason: str, *, fallback: str = "用户已取消任务") -> dict:
    text = str(reason or fallback)
    return {"reason": text, "terminal": terminal_detail("cancelled", text)}


def paused_payload(reason: str, *, fallback: str = "用户暂停了任务") -> dict:
    # 暂停以前连码都没有：它只是"停下等人"，但界面同样要回答"这一轮为什么停"，
    # 而"停"与"结束"的区别是 code 而不是文案。
    text = str(reason or fallback)
    return {"reason": text, "terminal": terminal_detail("paused", text)}


