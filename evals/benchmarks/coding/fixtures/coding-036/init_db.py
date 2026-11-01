import sqlite3
conn = sqlite3.connect('data.db')
conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
conn.execute("INSERT INTO users (name) VALUES ('alice')")
conn.commit()
conn.close()
print('seeded')
