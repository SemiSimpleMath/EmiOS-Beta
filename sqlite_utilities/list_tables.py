"""List all tables in SQLite database"""
import sqlite3

conn = sqlite3.connect('emi.db')
cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
tables = [row[0] for row in cursor.fetchall()]

print(f'SQLite tables ({len(tables)}):')
print('=' * 50)
for t in tables:
    cursor2 = conn.execute(f"SELECT COUNT(*) FROM {t}")
    count = cursor2.fetchone()[0]
    print(f'  {t:30} ({count:,} rows)')

conn.close()




