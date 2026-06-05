# Schema migrations

Numbered, idempotent migrations applied automatically on startup by
`app/database/migration_runner.py` (hooked into `initialize_all_tables()`).

These are the authority for **alterations to existing tables**. They exist so an
auto-updated install on an older DB doesn't silently miss schema changes.

## The rule (all contributors)

- **New table** → register it on `Base.metadata`; `create_all(checkfirst=True)`
  picks it up. **Do not** write a migration for it.
- **Any change to an existing table** (add/alter column, index, backfill, data
  fixup) → add a numbered migration here. **No more ad-hoc `ALTER` scripts.**

## Writing one

Create `NNNN_short_slug.py` (next number, zero-padded) exposing:

```python
def up(conn):          # conn is a sqlite3.Connection; the runner commits per-migration
    # MUST be idempotent — guard every change so re-running is a no-op.
    cols = [r[1] for r in conn.execute("PRAGMA table_info(my_table)")]
    if "new_col" not in cols:
        conn.execute("ALTER TABLE my_table ADD COLUMN new_col TEXT")
```

Guidelines:
- Idempotent: guard with `PRAGMA table_info(...)` / `IF NOT EXISTS`. The runner
  records each id in `schema_migrations`, but idempotency is your safety net.
- SQLite-only dialect: no `information_schema`, no `NOW()`/`WITH TIME ZONE`, no
  non-constant `DEFAULT` on `ADD COLUMN`. Dropping NOT NULL / changing a column
  needs the table-rebuild dance (create-new → copy → drop → rename).
- Don't `commit()` inside `up()` — the runner owns the transaction.

## Baseline behaviour

On a **fresh** DB, `create_all` already builds the latest schema, so the runner
**stamps** all known migrations as applied without running them. Only an
**existing older** DB actually runs the pending `up()`s. (Freshness is detected
by table presence before `create_all`.)

## Pending conversions

The legacy ad-hoc scripts below still need converting into numbered migrations
here (they're baseline-stamped on fresh installs, so this is a correctness/
completeness task for upgrading *existing* DBs, not a fresh-install blocker):

- `scripts/migrate_add_confidence_tier.py`
- `scripts/migrate_entity_card_v2_nullable_node_id.py` (table-rebuild — needs care)
- `app/assistant/kg_core/kg_setup/add_count_and_last_seen_to_taxonomy_links.py`
- `app/assistant/kg_core/kg_setup/add_original_sentence_column.py`
