---
job_schema_version: 1
job_id: example_job_id
description: Batch job description.

global_context:
  summary: Overall objective for the job bundle.
  stop_conditions:
    - id: objective_moot
      trigger: child_signal
      signal: objective_moot
      action: cancel_remaining

tasks:
  - job_id: task1
    manager: emi_team_manager
    task_file: tasks/<task_name>/task_spec.md
    depends_on: []
    information: Optional job-level guidance for this task.
    budget:
      max_cycles: 50
      timeout_seconds: 900

execution:
  max_parallel_managers: 2
  fail_fast: false
  resume_policy: resume
  output_policy: overwrite
---

# Job

Describe the batch objective and any constraints. The orchestrator will spawn tasks listed in the frontmatter.
