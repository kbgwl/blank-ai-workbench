import sqlite3
conn = sqlite3.connect('data.db')
conn.execute("CREATE TABLE tags (id INTEGER PRIMARY KEY, label TEXT)")
conn.execute("INSERT INTO tags (label) VALUES ('ops')")
conn.commit()
conn.close()
print('seeded')
