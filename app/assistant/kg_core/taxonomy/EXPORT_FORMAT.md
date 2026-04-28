# Taxonomy Export Format

The taxonomy can be exported in three formats:

## 1. Tree Format (Hierarchical)

**Default format.** Represents the taxonomy as a nested tree structure.

```json
{
  "metadata": {
    "exported_at": "2025-01-13T10:30:00.000000",
    "total_nodes": 150,
    "root_count": 6,
    "version": "1.0"
  },
  "taxonomy": [
    {
      "id": 1,
      "label": "entity",
      "parent_id": null,
      "description": "An identifiable thing",
      "children": [
        {
          "id": 10,
          "label": "artifact",
          "parent_id": 1,
          "description": "A human-made object",
          "children": [
            {
              "id": 20,
              "label": "product",
              "parent_id": 10,
              "description": "A commercial product",
              "children": []
            }
          ]
        }
      ]
    }
  ]
}
```

## 2. Flat Format

**List format.** All nodes in a flat list with parent_id references. Useful for importing/syncing.

```json
{
  "metadata": {
    "exported_at": "2025-01-13T10:30:00.000000",
    "format": "flat",
    "version": "1.0"
  },
  "nodes": [
    {
      "id": 1,
      "label": "entity",
      "parent_id": null,
      "description": "An identifiable thing"
    },
    {
      "id": 10,
      "label": "artifact",
      "parent_id": 1,
      "description": "A human-made object"
    },
    {
      "id": 20,
      "label": "product",
      "parent_id": 10,
      "description": "A commercial product"
    }
  ]
}
```

## 3. Paths Format

**ID-to-path mapping.** Maps each taxonomy ID to its full path. Useful for documentation and lookups.

```json
{
  "metadata": {
    "exported_at": "2025-01-13T10:30:00.000000",
    "format": "paths",
    "version": "1.0"
  },
  "paths": {
    "1": "entity",
    "10": "entity > artifact",
    "20": "entity > artifact > product",
    "30": "entity > artifact > product > service"
  }
}
```

## Usage

### From Web UI

Click the **💾 Export JSON** button in the taxonomy reviewer header to download the taxonomy as a JSON file.

### From Command Line

```bash
# Export hierarchical tree (default)
python app/assistant/kg_core/taxonomy/export.py

# Export to specific file
python app/assistant/kg_core/taxonomy/export.py -o backups/taxonomy_backup.json

# Export flat list
python app/assistant/kg_core/taxonomy/export.py --format flat

# Export paths
python app/assistant/kg_core/taxonomy/export.py --format paths

# Export minified (no formatting)
python app/assistant/kg_core/taxonomy/export.py --minified
```

### From Python

```python
from app.assistant.kg_core.taxonomy.export import TaxonomyExporter

exporter = TaxonomyExporter()

# Export to file
exporter.export_to_json('taxonomy_backup.json')

# Get as dictionary
data = exporter.export_to_dict()

# Get flat list
flat_list = exporter.export_flat_list()

# Get paths
paths = exporter.export_paths()
```

## Use Cases

- **Version Control**: Track taxonomy changes over time in git
- **Backup**: Regular snapshots of taxonomy structure
- **Documentation**: Generate human-readable taxonomy reference
- **Migration**: Move taxonomy between systems
- **Analysis**: Audit taxonomy structure and depth

