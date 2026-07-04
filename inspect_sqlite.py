import sqlite3
import sys

con = sqlite3.connect(sys.argv[1])
cur = con.cursor()
# Compare schemas across season tables
for t in ["2024-25", "odds_2022-23_new", "odds_2016-17_new", "odds_2016-17"]:
    print(f"\n=== {t} ===")
    cur.execute(f'PRAGMA table_info("{t}")')
    for r in cur.fetchall():
        print(" ", r[1], r[2])
    cur.execute(f'SELECT * FROM "{t}" LIMIT 2')
    cols = [d[0] for d in cur.description]
    print("sample cols:", cols)
    for row in cur.fetchall():
        print(" ", row)
