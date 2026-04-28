# Duplicate Detection and Analysis Workflow

This document describes the complete workflow for finding and analyzing duplicate nodes in the knowledge graph.

## 🎯 Overview

The workflow consists of two main stages:
1. **Duplicate Detection** - Finds potential duplicates using AI (random sampling) or algorithms
2. **Detailed Analysis** - Uses AI agent to analyze each duplicate group in detail

## 🔍 Stage 1: Random Sampling Detection

**Script**: `random_sampling_duplicate_detector.py`

**What it does**:
- Takes random samples of 1000 nodes from the database
- Uses the `duplicate_detector_random_sampling` AI agent to identify potential duplicates
- Outputs results to `random_sampling_results_YYYYMMDD_HHMMSS.json`

**Usage**:
```bash
python app/assistant/kg_maintenance/duplicate_handling/random_sampling_duplicate_detector.py
```

**Output format**:
```json
{
  "timestamp": 1756923829.8983316,
  "total_batches": 10,
  "sample_size_per_batch": 1000,
  "total_duplicate_groups_found": 53,
  "duplicate_groups": [
    ["8f304896-b285-4090-a171-5908b316ddc6", "2ff0f909-1265-4c70-ba45-20c05cd96b33"],
    ["3e336e10-f36f-4d94-bda2-a219b0d1b846", "4dac522c48b54781b708716ce0a915ec"]
  ]
}
```



## 📊 Stage 2: Detailed Analysis

**Script**: `duplicate_analysis_pipeline.py`

**What it does**:
- Loads duplicate groups from any of the supported formats
- Fetches fresh, unbiased node data from the database
- Uses the `duplicate_detector` AI agent to analyze each group
- Provides detailed recommendations for merging or keeping separate

**Usage**:
```bash
python app/assistant/kg_maintenance/duplicate_handling/duplicate_analysis_pipeline.py
```

**What it analyzes**:
- `context_window` from node attributes
- Neighborhood sample (up to 50 connected nodes)
- Edge relationships and types
- Node classification and metadata

## 🚀 Complete Workflow

### Option 1: AI-Powered Random Sampling
```bash
# 1. Find duplicates using AI agent
python app/assistant/kg_maintenance/random_sampling_duplicate_detector.py

# 2. Analyze duplicates
python app/assistant/kg_maintenance/duplicate_analysis_pipeline.py
```

### Option 2: Algorithmic Detection
```bash
# 1. Find duplicates using similarity algorithms
python app/assistant/kg_maintenance/node_deduplication_pipeline.py

# 2. Analyze duplicates
python app/assistant/kg_maintenance/duplicate_analysis_pipeline.py
```

**Both approaches output the same format**, so the analysis pipeline works seamlessly with either detection method.

## 📁 File Format

Both pipelines now output the same simple format:

```json
{
  "duplicate_groups": [
    ["node_id_1", "node_id_2"],
    ["node_id_3", "node_id_4", "node_id_5"]
  ]
}
```

The analysis pipeline automatically detects and handles this format, regardless of which detection method was used.

## 🔧 Configuration

### Random Sampling Parameters
- **Batches**: 10 (configurable in `__main__`)
- **Sample Size**: 1000 nodes per batch (configurable)
- **Total Coverage**: ~10,000 nodes (if you have 5,000 total nodes)

### Analysis Parameters
- **Max Groups**: 50 (configurable in `run_duplicate_analysis_pipeline`)
- **Neighborhood Sample**: Up to 50 connected nodes
- **Context Window**: From node attributes

## 💡 Tips

1. **Start Small**: Begin with fewer batches (e.g., 3-5) to test the workflow
2. **Review Results**: Check the JSON outputs to understand what's being found
3. **Use Conversion**: The format conversion makes debugging easier
4. **Monitor Logs**: Both scripts provide detailed logging of their progress

## 🐛 Troubleshooting

### Common Issues

1. **No reports found**: Make sure you've run the random sampling pipeline first
2. **Database connection errors**: Check your database configuration
3. **Agent errors**: Verify the `duplicate_detector_random_sampling` agent is properly configured

### Debug Mode

Both scripts include extensive logging. Check the console output for:
- Database connection details
- Batch processing progress
- Duplicate group counts
- Error messages

## 🔄 Next Steps

After analysis, you can:
1. **Manual Review**: Review the agent's structured recommendations
2. **Auto-Merge**: Use `auto_merge.py` for high-confidence duplicates (≥0.8 confidence)
3. **Custom Actions**: Implement your own merge strategies based on the structured output

## 🚀 Auto-Merge

The new structured agent output enables automated merging:

```bash
# Preview what would be merged (dry run)
python app/assistant/kg_maintenance/auto_merge.py

# Actually perform merges
python app/assistant/kg_maintenance/auto_merge.py --execute

# Custom confidence threshold
python app/assistant/kg_maintenance/auto_merge.py --execute --min-confidence 0.9
```

The agent now provides structured `merge_actions` instead of verbose explanations, making automation possible.

---

**Note**: This workflow is designed to be efficient and scalable. The random sampling approach reduces the search space from 5,000+ nodes to manageable batches, while the AI agents provide intelligent duplicate detection and analysis.
