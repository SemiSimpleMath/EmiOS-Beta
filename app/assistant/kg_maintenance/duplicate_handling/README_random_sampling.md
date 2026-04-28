# Random Sampling Duplicate Detector

## Overview

The Random Sampling Duplicate Detector is an efficient approach to finding potential duplicate nodes in large knowledge graphs without the computational overhead of complex similarity algorithms.

## How It Works

### 1. Random Sampling
- Takes random samples of 1,000 node labels from the database
- Assigns simple enum IDs (1, 2, 3, ..., 1000) to each sampled node
- Processes multiple batches to cover different parts of the graph

### 2. AI Agent Review
- Presents the agent with a simple list: "1: jukka (PersonNode), 2: birth_date (PropertyNode), ..."
- Agent quickly identifies obvious duplicates like "jukka" vs "jukka_virtanen"
- Returns enum ID groups: "Group 1: 1, 45, 234"

### 3. Post-Processing
- Maps enum IDs back to actual labels
- Fetches full node data only for flagged candidates
- Runs detailed duplicate analysis on promising pairs

## Advantages

- **Efficient**: No expensive similarity calculations
- **Scalable**: Works with any graph size
- **Agent-Friendly**: Simple input format for AI agents
- **Cost-Effective**: Only fetch detailed data for promising candidates
- **Random Coverage**: No bias toward specific patterns

## Usage

```bash
# Run 10 batches of 1000 nodes each
python app/assistant/kg_maintenance/random_sampling_duplicate_detector.py
```

## Configuration

- **Sample Size**: 1000 nodes per batch (configurable)
- **Number of Batches**: 10 batches (configurable)
- **Agent**: Uses `duplicate_detector_random_sampling` agent
- **Output**: Saves results to `random_sampling_results_*.json`

## Expected Results

With 5,000 total nodes and 10 batches of 1,000:
- **Coverage**: Each batch samples ~20% of the graph
- **Probability**: Good chance of catching duplicate pairs across batches
- **Efficiency**: 10,000 total evaluations vs. 25 million pairwise comparisons

## Workflow Integration

1. **Random Sampling** → Identifies potential candidates
2. **Detailed Analysis** → Uses existing `duplicate_analysis_pipeline.py`
3. **Auto-Merge** → Uses existing `auto_merge.py`

## Example Output

```
Batch 1:
  Sample size: 1000
  Duplicate groups found: 3
    • jukka, jukka_virtanen (IDs: [1, 45])
    • birth_date, date_of_birth (IDs: [234, 567])
    • new_york, nyc (IDs: [89, 123])
```

## Why This Approach?

- **Traditional similarity algorithms** miss semantic duplicates like "jukka" vs "jukka_virtanen"
- **Random sampling** catches these naturally without complex heuristics
- **AI agent** is better at semantic understanding than string similarity
- **Efficient** for large graphs where pairwise comparison is impractical
