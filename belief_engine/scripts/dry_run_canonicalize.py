"""Read-only matching backlog inspection; does not predict merges from embeddings."""
import argparse
import sqlite3
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True, help='SQLite database to inspect read-only')
    args = parser.parse_args()
    with sqlite3.connect(Path(args.database).resolve().as_uri()+'?mode=ro', uri=True) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'belief_match_pairs' not in tables:
            print('Incremental matching baseline has not started.')
        else:
            for state, count in conn.execute('SELECT applied,COUNT(*) FROM belief_match_pairs GROUP BY applied'):
                print(('reviewed/stale' if state else 'pending'), count)


if __name__ == '__main__':
    main()
