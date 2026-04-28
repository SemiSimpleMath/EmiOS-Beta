#!/usr/bin/env python3
"""
Seed taxonomy from the official taxonomy ontology JSON file.

This script loads the hierarchical taxonomy tree from:
app/assistant/kg_core/taxonomy/taxonomy_ontology/taxonomy_export_tree_*.json

Usage:
    python seed_taxonomy_from_ontology.py [--clear]
    
Options:
    --clear     Clear existing taxonomy before importing
"""

import json
import argparse
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional
from app.models.base import get_session
from app.assistant.kg_core.taxonomy.models import Taxonomy


def get_latest_ontology_file() -> Optional[Path]:
    """Find the latest taxonomy ontology JSON file."""
    ontology_dir = Path(__file__).parent.parent / 'taxonomy' / 'taxonomy_ontology'
    
    if not ontology_dir.exists():
        print(f"ERROR: Ontology directory not found: {ontology_dir}")
        return None
    
    json_files = sorted(ontology_dir.glob('taxonomy_export_tree_*.json'), reverse=True)
    
    if not json_files:
        print(f"ERROR: No taxonomy ontology files found in: {ontology_dir}")
        return None
    
    return json_files[0]


def flatten_taxonomy_tree(nodes: List[Dict], parent_id: Optional[int] = None) -> List[Dict]:
    """
    Recursively flatten a hierarchical taxonomy tree into a flat list.
    
    Args:
        nodes: List of taxonomy nodes (each may have 'children')
        parent_id: ID of the parent node (None for root nodes)
        
    Returns:
        Flat list of taxonomy nodes in correct insertion order (parents before children)
    """
    flat_list = []
    
    for node in nodes:
        # Create flat node (without children)
        flat_node = {
            'id': node['id'],
            'label': node['label'],
            'description': node.get('description'),
            'parent_id': node.get('parent_id')
        }
        flat_list.append(flat_node)
        
        # Recursively process children
        children = node.get('children', [])
        if children:
            flat_list.extend(flatten_taxonomy_tree(children, parent_id=node['id']))
    
    return flat_list


def import_taxonomy_from_ontology(session, json_file_path: Path, clear_existing: bool = False):
    """Import taxonomy from the hierarchical ontology JSON file."""
    
    print(f"📂 Loading taxonomy ontology from: {json_file_path.name}")
    
    with open(json_file_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    # Validate format
    if 'taxonomy' not in data:
        print("❌ ERROR: Invalid format: 'taxonomy' key not found")
        return False
    
    metadata = data.get('metadata', {})
    print(f"📊 Metadata:")
    print(f"   Version: {metadata.get('version')}")
    print(f"   Exported: {metadata.get('exported_at')}")
    print(f"   Total nodes: {metadata.get('total_nodes')}")
    print(f"   Root categories: {metadata.get('root_count')}")
    
    # Check existing taxonomy
    existing_count = session.query(Taxonomy).count()
    if existing_count > 0:
        print(f"\n⚠️  Warning: {existing_count} existing taxonomy nodes found")
        if clear_existing:
            print("🗑️  Clearing existing taxonomy...")
            session.query(Taxonomy).delete()
            session.commit()
            print("✅ Existing taxonomy cleared")
        else:
            print("❌ Aborted: Use --clear flag to replace existing taxonomy")
            return False
    
    try:
        # Flatten the hierarchical tree
        print("\n🔄 Flattening taxonomy tree...")
        nodes = flatten_taxonomy_tree(data['taxonomy'])
        print(f"   Flattened {len(nodes)} nodes")
        
        # Create taxonomy nodes
        print("\n📝 Creating taxonomy nodes...")
        created_count = 0
        id_mapping = {}  # Map old IDs to new IDs
        
        for node_data in nodes:
            old_id = node_data['id']
            old_parent_id = node_data.get('parent_id')
            
            # Map parent_id to new ID if it exists
            new_parent_id = None
            if old_parent_id is not None:
                new_parent_id = id_mapping.get(old_parent_id)
                if new_parent_id is None:
                    print(f"   ⚠️  Warning: Parent ID {old_parent_id} not found for '{node_data['label']}'")
            
            # Create new taxonomy node
            taxonomy_node = Taxonomy(
                label=node_data['label'],
                description=node_data.get('description'),
                parent_id=new_parent_id
            )
            
            session.add(taxonomy_node)
            session.flush()  # Get the new ID
            
            # Store the mapping
            id_mapping[old_id] = taxonomy_node.id
            
            created_count += 1
            
            if created_count % 100 == 0:
                print(f"   Created {created_count}/{len(nodes)} nodes...")
        
        session.commit()
        print(f"\n✅ Successfully imported {created_count} taxonomy nodes")
        print(f"   Root categories: {session.query(Taxonomy).filter_by(parent_id=None).count()}")
        print(f"   Total nodes: {session.query(Taxonomy).count()}")
        return True
        
    except Exception as e:
        print(f"\n❌ Error importing taxonomy: {e}")
        import traceback
        traceback.print_exc()
        session.rollback()
        return False


def main():
    parser = argparse.ArgumentParser(description='Import taxonomy from official ontology')
    parser.add_argument('--clear', action='store_true', help='Clear existing taxonomy before importing')
    parser.add_argument('--file', help='Specific ontology file to use (defaults to latest)')
    
    args = parser.parse_args()
    
    print("="*80)
    print("🌳 TAXONOMY ONTOLOGY IMPORTER")
    print("="*80)
    
    # Find or use specified ontology file
    if args.file:
        ontology_file = Path(args.file)
        if not ontology_file.exists():
            print(f"❌ ERROR: File not found: {ontology_file}")
            exit(1)
    else:
        ontology_file = get_latest_ontology_file()
        if not ontology_file:
            exit(1)
    
    session = get_session()
    
    try:
        success = import_taxonomy_from_ontology(session, ontology_file, clear_existing=args.clear)
        if success:
            print("\n" + "="*80)
            print("✅ TAXONOMY IMPORT COMPLETE!")
            print("="*80)
        else:
            print("\n" + "="*80)
            print("❌ TAXONOMY IMPORT FAILED!")
            print("="*80)
            exit(1)
    finally:
        session.close()


if __name__ == '__main__':
    main()

