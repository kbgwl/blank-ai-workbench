def test_bench():
    import sqlite3
    conn = sqlite3.connect('data.db')
    cols = [r[1] for r in conn.execute("PRAGMA table_info(tags)")]
    assert "slug" in cols, f"缺列 slug: {cols}"
    assert conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0] >= 1, "数据丢了"
    print("ok")

