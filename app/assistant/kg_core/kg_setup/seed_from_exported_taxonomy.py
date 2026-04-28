#!/usr/bin/env python3
"""
Seed taxonomy from exported JSON file.

Usage:
    python seed_from_exported_taxonomy.py --input my_curated_taxonomy.json
"""

import json
import argparse
from datetime import datetime
from app.models.base import get_session
from app.assistant.kg_core.taxonomy.models import Taxonomy


def import_taxonomy_from_json(session, json_file_path):
    """Import taxonomy from exported JSON file."""
    
    print(f"Loading taxonomy from: {json_file_path}")
    
    with open(json_file_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    if 'nodes' not in data:
        print("ERROR: Invalid format: 'nodes' key not found")
        return False
    
    nodes = data['nodes']
    print(f"Found {len(nodes)} taxonomy nodes to import")
    
    # Check if taxonomy already exists
    existing_count = session.query(Taxonomy).count()
    if existing_count > 0:
        print(f"Warning: {existing_count} existing taxonomy nodes found")
        response = input("Continue anyway? This may create duplicates (y/N): ")
        if response.lower() != 'y':
            print("Aborted")
            return False
    
    try:
        # Create taxonomy nodes
        created_count = 0
        for node_data in nodes:
            # Check if node already exists
            existing = session.query(Taxonomy).filter_by(
                label=node_data['label'],
                parent_id=node_data.get('parent_id')
            ).first()
            
            if existing:
                print(f"   Skipping existing: {node_data['label']}")
                continue
            
            # Create new taxonomy node
            taxonomy_node = Taxonomy(
                label=node_data['label'],
                description=node_data.get('description'),
                parent_id=node_data.get('parent_id'),
                usage_count=node_data.get('usage_count', 0),
                last_seen=datetime.utcnow() if node_data.get('usage_count', 0) > 0 else None
            )
            
            session.add(taxonomy_node)
            created_count += 1
            
            if created_count % 50 == 0:
                print(f"   Created {created_count} nodes...")
        
        session.commit()
        print(f"Successfully imported {created_count} taxonomy nodes")
        return True
        
    except Exception as e:
        print(f"Error importing taxonomy: {e}")
        session.rollback()
        return False


def main():
    parser = argparse.ArgumentParser(description='Import taxonomy from exported JSON')
    parser.add_argument('--input', required=True, help='Path to exported taxonomy JSON file')
    
    args = parser.parse_args()
    
    print("Importing taxonomy from exported JSON...")
    session = get_session()
    
    try:
        success = import_taxonomy_from_json(session, args.input)
        if success:
            print("Taxonomy import complete!")
        else:
            print("Taxonomy import failed!")
            exit(1)
    finally:
        session.close()


if __name__ == '__main__':
    main()
