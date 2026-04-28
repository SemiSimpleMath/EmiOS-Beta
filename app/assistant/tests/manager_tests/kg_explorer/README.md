# KG Explorer Tests

This directory contains tests for the Knowledge Graph Explorer system, following the same pattern as other manager tests.

## Files

- `kg_explorer_tests.py` - Main manager test (follows kg_team pattern)
- `run_tests.py` - Tool-only tests for quick validation
- `__init__.py` - Package initialization

## Running Tests

### Manager Test (Full System)
```bash
python app/assistant/tests/manager_tests/kg_explorer/kg_explorer_tests.py
```

### Tool Test (Quick Validation)
```bash
python app/assistant/tests/manager_tests/kg_explorer/run_tests.py
```

## Test Structure

### kg_explorer_tests.py
- **Pattern**: Follows `kg_team_manager.py` structure
- **Purpose**: Test the full KG Explorer manager as a standalone entity
- **Features**:
  - Full system initialization via `test_setup`
  - Manager preloading and creation
  - End-to-end manager execution
  - Real database queries

### run_tests.py
- **Pattern**: Tool-only testing
- **Purpose**: Quick validation of KG Explorer tool functionality
- **Features**:
  - Direct tool testing without full system
  - Database queries for overview, missing dates, orphaned nodes, data quality
  - No manager dependencies

## Test Coverage

1. **Manager Integration**:
   - Full system initialization
   - Manager creation and preloading
   - End-to-end KG exploration workflow
   - Real database analysis

2. **Tool Functionality**:
   - Overview queries
   - Missing dates analysis
   - Orphaned nodes detection
   - Data quality assessment
   - Temporal and relationship analysis

## Requirements

- **Manager Test**: Full system initialization, database connection
- **Tool Test**: Database connection only
- **Database**: PostgreSQL with knowledge graph tables

## Usage Examples

**Test Full KG Explorer Manager**:
```python
# This will run the complete KG Explorer analysis
python app/assistant/tests/manager_tests/kg_explorer/kg_explorer_tests.py
```

**Test KG Explorer Tool Only**:
```python
# This will test individual tool queries
python app/assistant/tests/manager_tests/kg_explorer/run_tests.py
```

## Notes

- Manager test follows the exact same pattern as `kg_team_manager.py`
- Tool test is useful for debugging individual queries
- Both tests require database access to the knowledge graph
- Results are printed to console for review
