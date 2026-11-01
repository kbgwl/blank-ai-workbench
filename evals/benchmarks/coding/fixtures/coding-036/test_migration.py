def test_bench():
    import sqlite3
    conn = sqlite3.connect('data.db')
    cols = [r[1] for r in conn.execute("PRAGMA table_info(users)")]
    assert "email" in cols, f"缺列 email: {cols}"
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] >= 1, "数据丢了"
    print("ok")

