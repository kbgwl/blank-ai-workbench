"""提交钩子夹具：`user_prompt_submit` 的三道决定按 goal 里的锚分派。

一台实例、一份 `BLANK_HOOKS` 要同时喂四种格（抑制 / 拦下 / 补上下文 / 无关），
而钩子配置是进程级、不按任务切换的，所以分派只能写在 goal 里。**锚与正文的唯一
来源是 `fixtures/prompt-submit-v123.json`**——夹具与判据读同一份，两处不许各抄一份
（#119 那批 `wake-text.txt` 立的同一条规矩）。

stdout 必须是 UTF-8：这份 JSON 里的正文会逐字进模型的系统消息。
"""

import json
import sys
from pathlib import Path

sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

SPEC = json.loads(
    (Path(__file__).resolve().parent.parent / "fixtures" / "prompt-submit-v123.json").read_text(
        encoding="utf-8"))
payload = json.load(sys.stdin)
goal = str(payload.get("goal") or "")


def emit(body: dict) -> None:
    json.dump({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", **body}},
              sys.stdout, ensure_ascii=False)


if SPEC["block_marker"] in goal:
    # 退出码 2 = 拦下这次提交（连一次模型调用都不该发起）。
    sys.stderr.write(SPEC["block_reason"])
    sys.exit(2)
if SPEC["suppress_marker"] in goal:
    emit({"additionalContext": SPEC["suppress_replacement"], "suppressOriginalPrompt": True})
elif SPEC["inject_marker"] in goal:
    emit({"additionalContext": SPEC["inject_text"]})
