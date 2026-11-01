import sqlite3
conn = sqlite3.connect('data.db')
cols = [r[1] for r in conn.execute("PRAGMA table_info(logs)")]
assert "created_at" in cols, f"缺列 created_at: {cols}"
assert conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0] >= 1, "数据丢了"
print("ok")
