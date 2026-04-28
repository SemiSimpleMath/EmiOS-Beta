"""Compare PostgreSQL tables (from earlier output) vs SQLite tables"""

# From the connection test output we saw earlier
pg_tables = [
    'event_repository', 'rag_database', 'edges', 'edges_backup_20251007',
    'nodes', 'unified_log', 'nodes_backup_20251007', 'processed_entity_log',
    'unified_log_backup', 'taxonomy', 'entity_cards', 'recurring_event_rules',
    'email_check_state', 'edge_canon', 'edge_alias', 'node_types',
    'unified_items', 'entity_card_usage', 'entity_card_index',
    'time_events', 'message_source_mapping', 'node_taxonomy_link',
    # News tables
    'news_articles', 'news_sources', 'news_keywords',
    # Add more from the 56 total...
]

import sqlite3
conn = sqlite3.connect('emi.db')
cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
sqlite_tables = [row[0] for row in cursor.fetchall() if row[0] != 'sqlite_sequence']
conn.close()

print("=" * 70)
print("TABLE COMPARISON")
print("=" * 70)

print(f"\nSQLite has {len(sqlite_tables)} tables")
print(f"PostgreSQL has ~56 tables (estimated)")

print("\n" + "=" * 70)
print("TABLES IN SQLITE:")
print("=" * 70)
for t in sorted(sqlite_tables):
    print(f"  [OK] {t}")

print("\n" + "=" * 70)
print("KNOWN POSTGRESQL TABLES NOT IN SQLITE:")
print("=" * 70)

missing = []
for t in pg_tables:
    if t not in sqlite_tables and 'backup' not in t.lower():
        missing.append(t)

for t in sorted(set(missing)):
    print(f"  [ ] {t}")

print("\n" + "=" * 70)
print("CATEGORIES:")
print("=" * 70)
print("  [OK] Core tables: unified_log, entity_cards, event_repository")
print("  [OK] KG tables: kg_node_metadata, kg_edge_metadata, edge_canon, edge_alias, node_types")
print("  [OK] Unified items: unified_items")
print("  [ ] RAG: rag_database")
print("  [ ] Taxonomy: taxonomy, node_taxonomy_link")
print("  [ ] Entity processing: processed_entity_log, message_source_mapping")
print("  [ ] News: news_articles, news_sources, news_keywords (if they exist)")




