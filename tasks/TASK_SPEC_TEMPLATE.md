---
schema_version: 1
task_id: example_task_id
manager: emi_team_manager
description: Short description of the task.

# Resources and includes
includes:
  - tasks/<task_name>/rules.md
  - resources/resource_chat_guidelines.md
  # Use "resource:<id>" to reference a loaded resource by id.
  # - resource:resource_chat_guidelines

allowed_resources:
  - resource_chat_guidelines

# Input/output contract
inputs:
  - id: input_file
    path: tasks/<task_name>/input_file.txt
    type: text
    required: true

outputs:
  - id: output_file
    path: tasks/<task_name>/output_file.txt
    format: text
    overwrite: true

# Execution constraints (optional)
acceptance_tests:
  - id: out_exists
    type: file_exists
    target: outputs.output_file

idempotency: overwrite_outputs

limits:
  timeout_seconds: 900
  max_cycles: 50

# Deterministic parsing rules (optional)
parsing_rules:
  date_marker_regex: '^\\*[0-9]{1,2}/[0-9]{1,2}/[0-9]{2}\\*$'
---

# Task

1) Describe the task steps in a clear, human-readable way.
2) Use the input/output contract above for file operations.
3) Reference any included rules by name only; the loader will inject them.

