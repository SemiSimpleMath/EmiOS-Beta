#!/usr/bin/env python3
"""
Interactive SQLite query tool for emi.db

Usage: python query_db.py
Then type SQL commands or use shortcuts like:
  - states: Show unified_items by state
  - ingested: Show INGESTED items
  - events: Show EventRepository stats
  - recent: Show recent unified_items
  - tables: List all tables
  - quit: Exit
"""

import sqlite3
import sys
from datetime import datetime

DB_PATH = 'emi.db'

def format_results(cursor, rows):
    """Format query results as a nice table"""
    if not rows:
        return "No results."
    
    # Get column names
    columns = [desc[0] for desc in cursor.description]
    
    # Calculate column widths
    widths = [len(col) for col in columns]
    for row in rows:
        for i, val in enumerate(row):
            widths[i] = max(widths[i], len(str(val)))
    
    # Print header
    header = " | ".join(col.ljust(widths[i]) for i, col in enumerate(columns))
    separator = "-+-".join("-" * w for w in widths)
    
    result = [header, separator]
    
    # Print rows
    for row in rows:
        result.append(" | ".join(str(val).ljust(widths[i]) for i, val in enumerate(row)))
    
    return "\n".join(result)

def run_query(cursor, query):
    """Run a query and display results"""
    try:
        cursor.execute(query)
        rows = cursor.fetchall()
        
        if cursor.description:  # SELECT query
            print(format_results(cursor, rows))
            print(f"\n({len(rows)} rows)")
        else:  # Non-SELECT query
            print(f"Query executed successfully. Rows affected: {cursor.rowcount}")
    except Exception as e:
        print(f"Error: {e}")

def main():
    """Main interactive loop"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        print(f"Connected to: {DB_PATH}")
        print("SQLite version:", cursor.execute("SELECT sqlite_version()").fetchone()[0])
        print("\nType SQL commands or shortcuts (type 'help' for shortcuts, 'quit' to exit)\n")
        
        shortcuts = {
            'states': "SELECT state, COUNT(*) as count FROM unified_items GROUP BY state",
            'ingested': "SELECT id, source_type, title, created_at FROM unified_items WHERE state = 'ingested' LIMIT 20",
            'recent': "SELECT id, source_type, title, state, created_at FROM unified_items ORDER BY created_at DESC LIMIT 20",
            'dismissed': "SELECT id, source_type, title, agent_decision, substr(agent_notes, 1, 100) as notes, created_at FROM unified_items WHERE state = 'dismissed' ORDER BY created_at DESC LIMIT 20",
            'dismissed_full': "SELECT id, source_type, title, agent_decision, agent_notes, created_at FROM unified_items WHERE state = 'dismissed' ORDER BY created_at DESC LIMIT 10",
            'events': "SELECT data_type, COUNT(*) as count FROM event_repository GROUP BY data_type",
            'recent_events': "SELECT data_type, created_at, substr(data, 1, 100) as data_preview FROM event_repository ORDER BY created_at DESC LIMIT 20",
            'tables': "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
            'help': None  # Special case
        }
        
        while True:
            try:
                query = input("sqlite> ").strip()
                
                if not query:
                    continue
                
                if query.lower() in ('quit', 'exit', 'q'):
                    break
                
                if query.lower() == 'help':
                    print("\nAvailable shortcuts:")
                    for shortcut, sql in shortcuts.items():
                        if sql:
                            print(f"  {shortcut:15} - {sql[:80]}")
                    print(f"  {'quit':15} - Exit")
                    print("\nOr type any SQL command directly.\n")
                    continue
                
                # Check if it's a shortcut
                if query.lower() in shortcuts:
                    query = shortcuts[query.lower()]
                
                run_query(cursor, query)
                print()
                
            except KeyboardInterrupt:
                print("\nUse 'quit' to exit.")
            except EOFError:
                break
        
        conn.close()
        print("\nGoodbye!")
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

if __name__ == '__main__':
    main()

