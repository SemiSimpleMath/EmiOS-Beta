#!/usr/bin/env python3
"""
Complete setup using exported taxonomy.

This script:
1. Clears KG data (preserves taxonomy)
2. Recreates KG tables
3. Imports your exported taxonomy
4. Seeds core nodes (Alex, Emi)

Usage:
    python setup_with_exported_taxonomy.py --taxonomy my_curated_taxonomy.json
"""

import argparse
import subprocess
import sys
from pathlib import Path


def run_script(script_path, description):
    """Run a Python script and handle errors."""
    print(f"\n🔄 {description}...")
    try:
        result = subprocess.run([sys.executable, script_path], 
                              capture_output=True, text=True, check=True)
        print(f"✅ {description} completed")
        if result.stdout:
            print(result.stdout)
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ {description} failed:")
        print(f"   Error: {e.stderr}")
        return False


def main():
    parser = argparse.ArgumentParser(description='Complete KG setup with exported taxonomy')
    parser.add_argument('--taxonomy', required=True, 
                       help='Path to exported taxonomy JSON file')
    parser.add_argument('--skip-clear', action='store_true',
                       help='Skip clearing KG data (if already cleared)')
    parser.add_argument('--skip-tables', action='store_true', 
                       help='Skip recreating KG tables (if already exist)')
    
    args = parser.parse_args()
    
    # Check if taxonomy file exists
    taxonomy_path = Path(args.taxonomy)
    if not taxonomy_path.exists():
        print(f"❌ Taxonomy file not found: {taxonomy_path}")
        exit(1)
    
    print("Starting complete KG setup with exported taxonomy...")
    print(f"Using taxonomy: {taxonomy_path}")
    
    # Step 1: Clear KG data (unless skipped)
    if not args.skip_clear:
        if not run_script("app/assistant/kg_core/kg_setup/clear_kg_data_only.py", 
                         "Clearing KG data"):
            exit(1)
    else:
        print("Skipping KG data clear (--skip-clear)")
    
    # Step 2: Recreate KG tables (unless skipped)
    if not args.skip_tables:
        print("\nRecreating KG tables...")
        try:
            from app.assistant.kg.db.knowledge_graph_db import initialize_knowledge_graph_db
            initialize_knowledge_graph_db()
            print("KG tables recreated")
        except Exception as e:
            print(f"Failed to recreate KG tables: {e}")
            exit(1)
    else:
        print("Skipping KG table recreation (--skip-tables)")
    
    # Step 3: Import exported taxonomy
    if not run_script(f"app/assistant/kg_core/kg_setup/seed_from_exported_taxonomy.py --input {args.taxonomy}", 
                     "Importing exported taxonomy"):
        exit(1)
    
    # Step 4: Seed core nodes
    try:
        from app.database.table_initializer import _seed_kg_core_nodes
        _seed_kg_core_nodes()
        print("Core nodes seeded (user + assistant)")
    except Exception as e:
        print(f"Failed to seed core nodes: {e}")
        exit(1)

    print("\nComplete setup finished!")
    print("Your KG is now ready with:")
    print("   - Clean KG data")
    print("   - Your curated taxonomy structure")
    print("   - Core nodes (user, assistant, relationship state)")
    print("   - Ready for new knowledge extraction")


if __name__ == '__main__':
    main()
