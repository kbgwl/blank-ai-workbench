def test_bench():
    import sqlite3
    conn = sqlite3.connect('data.db')
    cols = [r[1] for r in conn.execute("PRAGMA table_info(orders)")]
    assert "total_cents" in cols, f"缺列 total_cents: {cols}"
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] >= 1, "数据丢了"
    print("ok")

