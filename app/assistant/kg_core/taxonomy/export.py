#!/usr/bin/env python3
"""
Taxonomy Export Utility
Exports the taxonomy hierarchy to JSON format for backup, version control, or sharing.
"""
import app.assistant.tests.test_setup  # Initialize environment
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any

logger = logging.getLogger(__name__)

from app.models.base import get_session
from app.assistant.kg_core.taxonomy.models import Taxonomy


class TaxonomyExporter:
    """Export taxonomy to JSON format"""
    
    def __init__(self, session=None):
        self.session = session or get_session()
    
    def export_to_dict(self) -> Dict[str, Any]:
        """
        Export the entire taxonomy tree to a dictionary.
        
        Returns:
            Dict with metadata and hierarchical taxonomy structure
        """
        # Get all taxonomy nodes
        all_nodes = self.session.query(Taxonomy).all()
        
        # Build a map of id -> node data
        node_map = {}
        for node in all_nodes:
            node_map[node.id] = {
                'id': node.id,
                'label': node.label,
                'parent_id': node.parent_id,
                'description': node.description,
                'children': []
            }
        
        # Build the tree structure
        root_nodes = []
        for node in all_nodes:
            if node.parent_id is None:
                # Root node
                root_nodes.append(node_map[node.id])
            else:
                # Child node - add to parent's children
                if node.parent_id in node_map:
                    node_map[node.parent_id]['children'].append(node_map[node.id])
        
        # Build export structure
        export_data = {
            'metadata': {
                'exported_at': datetime.utcnow().isoformat(),
                'total_nodes': len(all_nodes),
                'root_count': len(root_nodes),
                'version': '1.0'
            },
            'taxonomy': root_nodes
        }
        
        return export_data
    
    def export_to_json(self, output_path: str = None, pretty: bool = True) -> str:
        """
        Export taxonomy to a JSON file.
        
        Args:
            output_path: Path to save JSON file. If None, generates default path.
            pretty: If True, format with indentation for readability
            
        Returns:
            Path to the saved file
        """
        # Generate default path if not provided
        if output_path is None:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            output_path = f'taxonomy_export_{timestamp}.json'
        
        # Convert to Path object
        output_file = Path(output_path)
        
        # Ensure parent directory exists
        output_file.parent.mkdir(parents=True, exist_ok=True)
        
        # Export to dict
        data = self.export_to_dict()
        
        # Write to file
        with open(output_file, 'w', encoding='utf-8') as f:
            if pretty:
                json.dump(data, f, indent=2, ensure_ascii=False)
            else:
                json.dump(data, f, ensure_ascii=False)
        
        print(f"✅ Exported {data['metadata']['total_nodes']} taxonomy nodes to: {output_file}")
        return str(output_file)
    
    def export_flat_list(self) -> List[Dict[str, Any]]:
        """
        Export taxonomy as a flat list (no hierarchy, just parent_id references).
        Useful for importing/syncing.
        
        Returns:
            List of taxonomy nodes with their properties
        """
        nodes = self.session.query(Taxonomy).order_by(Taxonomy.id).all()
        
        flat_list = []
        for node in nodes:
            flat_list.append({
                'id': node.id,
                'label': node.label,
                'parent_id': node.parent_id,
                'description': node.description
            })
        
        return flat_list
    
    def export_paths(self) -> Dict[int, str]:
        """
        Export taxonomy as a map of ID -> full path.
        Useful for documentation or quick lookups.
        
        Returns:
            Dict mapping taxonomy IDs to their full paths
        """
        nodes = self.session.query(Taxonomy).all()
        
        # Build map
        node_map = {node.id: node for node in nodes}
        
        # Build paths
        paths = {}
        for node in nodes:
            path_parts = [node.label]
            current = node
            visited = {node.id}

            # Traverse up to root (with cycle detection)
            while current.parent_id:
                if current.parent_id in visited:
                    logger.warning("Cycle detected in taxonomy at node id=%s label=%s", current.id, current.label)
                    break
                parent = node_map.get(current.parent_id)
                if parent:
                    visited.add(parent.id)
                    path_parts.insert(0, parent.label)
                    current = parent
                else:
                    break
            
            paths[node.id] = ' > '.join(path_parts)
        
        return paths


def main():
    """Command-line interface for taxonomy export"""
    import argparse
    
    parser = argparse.ArgumentParser(
        description='Export taxonomy to JSON format'
    )
    parser.add_argument(
        '-o', '--output',
        help='Output file path (default: taxonomy_export_TIMESTAMP.json)',
        default=None
    )
    parser.add_argument(
        '--format',
        choices=['tree', 'flat', 'paths'],
        default='tree',
        help='Export format: tree (hierarchical), flat (list), or paths (ID->path map)'
    )
    parser.add_argument(
        '--minified',
        action='store_true',
        help='Output minified JSON (no pretty printing)'
    )
    
    args = parser.parse_args()
    
    # Create exporter
    exporter = TaxonomyExporter()
    
    # Export based on format
    if args.format == 'tree':
        output_path = exporter.export_to_json(
            output_path=args.output,
            pretty=not args.minified
        )
        print(f"📁 Hierarchical export saved to: {output_path}")
        
    elif args.format == 'flat':
        data = {
            'metadata': {
                'exported_at': datetime.utcnow().isoformat(),
                'format': 'flat',
                'version': '1.0'
            },
            'nodes': exporter.export_flat_list()
        }
        
        output_path = args.output or f'taxonomy_flat_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
        with open(output_path, 'w', encoding='utf-8') as f:
            if args.minified:
                json.dump(data, f, ensure_ascii=False)
            else:
                json.dump(data, f, indent=2, ensure_ascii=False)
        
        print(f"✅ Flat list export saved to: {output_path}")
        
    elif args.format == 'paths':
        data = {
            'metadata': {
                'exported_at': datetime.utcnow().isoformat(),
                'format': 'paths',
                'version': '1.0'
            },
            'paths': exporter.export_paths()
        }
        
        output_path = args.output or f'taxonomy_paths_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
        with open(output_path, 'w', encoding='utf-8') as f:
            if args.minified:
                json.dump(data, f, ensure_ascii=False)
            else:
                json.dump(data, f, indent=2, ensure_ascii=False)
        
        print(f"✅ Paths export saved to: {output_path}")


if __name__ == '__main__':
    main()

