import sqlite3
conn = sqlite3.connect('data.db')
conn.execute("CREATE TABLE logs (id INTEGER PRIMARY KEY, msg TEXT)")
conn.execute("INSERT INTO logs (msg) VALUES ('boot')")
conn.commit()
conn.close()
print('seeded')
