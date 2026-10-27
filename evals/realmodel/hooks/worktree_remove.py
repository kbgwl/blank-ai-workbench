"""worktree_remove 钩子：把清理通知落到 home 下的审计文件，点验装置读它。"""

import json
import os
import sys
from pathlib import Path

sys.stdin.reconfigure(encoding="utf-8")
payload = json.load(sys.stdin)
home = Path(os.environ.get("BLANK_REALMODEL_HOME", "E:/tmp/realmodel"))
home.mkdir(parents=True, exist_ok=True)
with (home / "hook-events.log").open("a", encoding="utf-8") as fh:
    fh.write(json.dumps({"event": "worktree_remove", **payload}, ensure_ascii=False) + "\n")
