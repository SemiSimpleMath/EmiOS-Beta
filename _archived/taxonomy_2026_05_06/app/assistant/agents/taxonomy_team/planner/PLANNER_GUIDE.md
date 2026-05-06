# Taxonomy Team Planner Guide

## Overview

The **taxonomy_team::planner** is responsible for executing taxonomy maintenance operations. It receives structured input from the `taxonomy_integrity_pipeline` and uses taxonomy tools to fix issues.

## What the Planner Receives

### Input Format

The planner receives two fields from the `taxonomy_team_manager`:

1. **`task`** - A short summary of the problem (max 200 chars)
2. **`information`** - Full context with all details needed to execute

### Example Input

```
task: "Duplicate machine/robot subtrees exist under both 'artifact' and 'product > physical_product'..."

information: """
PROBLEM:
Duplicate machine/robot subtrees exist under both 'artifact' and 'product > physical_product'. 
This splits identical concepts across two branches and duplicates 'robot' and 'competition_robot'.

AFFECTED CATEGORIES:

  📦 'machine' (DUPLICATE - 2 instances):
    • ID: 746
      Path: entity > artifact > machine
      Parent: artifact
      Description: (no description)
    • ID: 492
      Path: entity > artifact > product > physical_product > machine
      Parent: physical_product
      Description: (no description)

  📦 'robot' (DUPLICATE - 2 instances):
    • ID: 747
      Path: entity > artifact > machine > robot
      Parent: machine
      Description: (no description)
    • ID: 493
      Path: entity > artifact > product > physical_product > machine > robot
      Parent: machine
      Description: (no description)

ACTIONS TO TAKE (IN ORDER):
  1. merge_categories(746, 492)
     → Merge 'machine' (ID 746) at 'entity > artifact > machine' 
       INTO 'machine' (ID 492) at 'entity > artifact > product > physical_product > machine'
  2. merge_categories(747, 493)
     → Merge 'robot' (ID 747) at 'entity > artifact > machine > robot' 
       INTO 'robot' (ID 493) at 'entity > artifact > product > physical_product > machine > robot'

CONFIDENCE: 93.0%
"""
```

## Available Tools

The planner has access to these taxonomy tools:

### 1. `taxonomy_merge_categories`
**Purpose**: Merge two duplicate categories into one

**Parameters**:
- `source_id` (int) - Category to merge FROM (will be deleted)
- `destination_id` (int) - Category to merge INTO (will be kept)

**What it does**:
- Moves all node classifications from source to destination
- Moves all child categories from source to destination
- Deletes the source category

**Example**:
```json
{
  "source_id": 746,
  "destination_id": 492
}
```

### 2. `taxonomy_move_category`
**Purpose**: Move a category to a new parent

**Parameters**:
- `category_id` (int) - Category to move
- `new_parent_id` (int) - New parent category

**What it does**:
- Changes the parent_id of the category
- All children move with it

**Example**:
```json
{
  "category_id": 749,
  "new_parent_id": 500
}
```

### 3. `taxonomy_rename_category`
**Purpose**: Rename a category

**Parameters**:
- `category_id` (int) - Category to rename
- `new_label` (str) - New name for the category

**What it does**:
- Updates the label field
- Does not affect node classifications or children

**Example**:
```json
{
  "category_id": 456,
  "new_label": "email_address"
}
```

### 4. `taxonomy_update_description`
**Purpose**: Update a category's description

**Parameters**:
- `category_id` (int) - Category to update
- `new_description` (str) - New description text

**What it does**:
- Updates the description field

**Example**:
```json
{
  "category_id": 123,
  "new_description": "An electronic mail address used for digital communication"
}
```

### 5. `ask_user`
**Purpose**: Ask the user for clarification or approval

**Parameters**:
- `question` (str) - Question to ask the user

**When to use**:
- You're unsure about an operation
- You encounter an unexpected error
- You need to confirm a risky operation

## Planner Workflow

### Step 1: Analyze the Problem
- Read the PROBLEM description
- Review all AFFECTED CATEGORIES
- Understand the full context (paths, parents, descriptions)

### Step 2: Create Execution Plan
- Parse the ACTIONS TO TAKE list
- Create a checklist with one item per action
- Verify each action makes sense given the context

### Step 3: Execute Actions in Order
For each action:
1. Verify the category IDs exist in the context
2. Call the appropriate tool with correct parameters
3. Record the result in your summary
4. Update the checklist (DONE ✓, FAILED ✗, etc.)
5. Move to the next action

### Step 4: Handle Errors
If a tool call fails:
- Mark it as FAILED ✗ in checklist
- Analyze why (category doesn't exist? Already merged?)
- Decide: skip, retry, or ask user
- Continue with remaining actions if possible

### Step 5: Complete
When all actions are done (or failed):
- Set `action` to `return_control`
- The final_answer agent will summarize results

## Output Schema

The planner must output:

```python
{
  "what_i_am_thinking": str,  # Your reasoning and analysis
  "summary": str,              # Summary of most recent tool result
  "checklist": List[str],      # Progress tracking
  "plan": str,                 # Remaining steps
  "action": str,               # Tool name or "return_control"
  "action_input": str          # JSON string of tool parameters
}
```

## Example Execution

### Action 1: First Merge
```json
{
  "what_i_am_thinking": "I need to merge the duplicate 'machine' categories. ID 746 is under 'artifact' and ID 492 is under 'product > physical_product'. The action says to merge 746 INTO 492, so 746 is the source and 492 is the destination. This makes sense because we want to consolidate under the 'product' branch.",
  
  "summary": "",
  
  "checklist": [
    "→ IN PROGRESS: merge_categories(746, 492) - Merge duplicate 'machine' categories",
    "merge_categories(747, 493) - Merge duplicate 'robot' categories"
  ],
  
  "plan": "1. Merge machine categories (746 -> 492)\n2. Merge robot categories (747 -> 493)\n3. Exit when complete",
  
  "action": "taxonomy_merge_categories",
  
  "action_input": "{\"source_id\": 746, \"destination_id\": 492}"
}
```

### Action 2: After First Merge
```json
{
  "what_i_am_thinking": "The merge of machine categories succeeded. Now I need to merge the robot categories. Since machine 746 was merged into 492, robot 747 (which was under machine 746) should now be merged into robot 493 (which is under machine 492).",
  
  "summary": "Successfully merged 'machine' category ID 746 into ID 492. All node classifications and child categories from 746 have been moved to 492. Category 746 has been deleted.",
  
  "checklist": [
    "✓ DONE: merge_categories(746, 492) - Merge duplicate 'machine' categories",
    "→ IN PROGRESS: merge_categories(747, 493) - Merge duplicate 'robot' categories"
  ],
  
  "plan": "1. Merge robot categories (747 -> 493)\n2. Exit when complete",
  
  "action": "taxonomy_merge_categories",
  
  "action_input": "{\"source_id\": 747, \"destination_id\": 493}"
}
```

### Action 3: Complete
```json
{
  "what_i_am_thinking": "Both merges have completed successfully. All duplicate categories have been consolidated. The task is complete.",
  
  "summary": "Successfully merged 'robot' category ID 747 into ID 493. All node classifications and child categories from 747 have been moved to 493. Category 747 has been deleted.",
  
  "checklist": [
    "✓ DONE: merge_categories(746, 492) - Merge duplicate 'machine' categories",
    "✓ DONE: merge_categories(747, 493) - Merge duplicate 'robot' categories"
  ],
  
  "plan": "All actions completed successfully. Exiting.",
  
  "action": "return_control",
  
  "action_input": ""
}
```

## Important Notes

1. **Order Matters**: Actions are ordered for a reason (e.g., merge parent before child). Follow the order exactly.

2. **Verify IDs**: Always check that category IDs exist in the provided context before calling tools.

3. **Record Everything**: Your summary is the only record of what happened. Be thorough.

4. **Handle Failures**: If a tool fails, don't give up. Analyze, decide, and continue with remaining actions.

5. **Action Limit**: You have 15 actions max. Use them wisely. Most tasks need 2-5 actions.

6. **Test Mode**: Currently, all taxonomy tools are in TEST MODE and return mock success responses without modifying the database.

## Testing

To test the planner:

```bash
python app/assistant/tests/manager_tests/taxonomy_team/taxonomy_team_manager.py
```

This will run the planner with a real example from the taxonomy_integrity_validator.


