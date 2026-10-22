"""把已种下的结论挤出默认 120 条窗口，再用生产代码回填索引。

直插历史消息只是为了造出「索引里有、默认窗口里没有」的检索场景；
建索引走 blank_api.services.chat_index.backfill，不自己写第二套。
"""

import asyncio
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

WORK = Path(os.environ["TEAM_E2E_WORK"])
DB = Path(os.environ["TEAM_E2E_DB"])
seed = json.loads((WORK / "seed.json").read_text(encoding="utf-8"))

conn = sqlite3.connect(DB)
group_id = seed["group_id"]
now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
for index in range(130):
    conn.execute(
        "insert into team_messages "
        "(group_id, round_id, role, bot_id, bot_name, content, status, task_id, meta, created_at) "
        "values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            group_id,
            None,
            "user",
            None,
            "",
            f"填充消息 {index + 1}：登录联调无关记录",
            "",
            None,
            "{}",
            now,
        ),
    )
conn.commit()
total, max_id, old = conn.execute(
    "select count(*), max(id), min(id) from team_messages where group_id = ?", (group_id,)
).fetchone()
conn.close()
print(json.dumps({"total": total, "min_id": old, "max_id": max_id}, ensure_ascii=False))

from blank_api.services.chat_index import backfill

print("backfilled", asyncio.run(backfill()))
