import sqlite3
conn = sqlite3.connect('data.db')
conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, item TEXT)")
conn.execute("INSERT INTO orders (item) VALUES ('book')")
conn.commit()
conn.close()
print('seeded')
