import sqlite3
conn = sqlite3.connect('data.db')
conn.execute("CREATE TABLE plans (id INTEGER PRIMARY KEY, name TEXT)")
conn.execute("INSERT INTO plans (name) VALUES ('pro')")
conn.commit()
conn.close()
print('seeded')
