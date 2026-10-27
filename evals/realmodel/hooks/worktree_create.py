"""worktree_create 钩子：非 git 目录也能给出隔离副本。

副本落在 `BLANK_REALMODEL_HOME`（默认 E:/tmp/realmodel）下的 `worktrees/`，
不在仓库里；产品只认钩子返回的那个 `path`，`mode` 记成 `hook`。
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.stdin.reconfigure(encoding="utf-8")
payload = json.load(sys.stdin)
base = Path(payload["workspace"])
home = Path(os.environ.get("BLANK_REALMODEL_HOME", "E:/tmp/realmodel"))
root = home / "worktrees"
root.mkdir(parents=True, exist_ok=True)
dest = Path(tempfile.mkdtemp(dir=root, prefix="hookcopy-"))
shutil.copytree(base, dest, dirs_exist_ok=True)
(dest / "FROM-HOOK.txt").write_text("created by worktree_create hook\n", encoding="utf-8")
sys.stdout.write(json.dumps({"path": str(dest)}))
