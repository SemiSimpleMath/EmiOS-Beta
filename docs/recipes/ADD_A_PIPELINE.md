# Recipe: Add a new pipeline

Pipelines are sequential step-based code that runs once when invoked. Use them for background data processing — daily insights, KG ingestion, entity card generation, belief synthesis, wiki refresh.

If you need a *conversational* loop, write a manager instead. If you need *scheduling* on top of an existing pipeline, see [Add a routine](ADD_A_ROUTINE.md). If you want something fired by an event rather than a cadence, write an event handler — not a pipeline.

Read [06_PIPELINES_AND_ROUTINES.md](../architecture/06_PIPELINES_AND_ROUTINES.md) for the framework.

## File layout

```
app/assistant/pipelines/<my_pipeline_id>/
  __init__.py
  pipeline.py                 # the Pipeline class with steps list
  steps/
    __init__.py
    step_a.py                 # PipelineStep impls
    step_b.py
    step_c.py
  scope.json                  # what resources / write rights the pipeline gets
  README.md                   # optional but recommended
```

## The PipelineStep contract

Each step implements three methods on a class:

```python
# app/assistant/pipelines/my_pipeline_id/steps/step_a.py
from __future__ import annotations
from pathlib import Path
from typing import List

from app.assistant.pipelines.context import PipelineContext
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class StepA:
    name = "step_a"

    def inputs(self, ctx: PipelineContext) -> List[str]:
        """Symbolic input names — used for documentation, not enforcement."""
        return ["unified_log_2026"]

    def outputs(self, ctx: PipelineContext) -> List[Path]:
        """Files this step writes. Idempotency: the runner skips this step
        if all output files exist (unless force=True)."""
        return [ctx.day_dir / "step_a_result.json"]

    def run(self, ctx: PipelineContext) -> None:
        """Do the work. Read what you need from ctx, write outputs."""
        logger.info("[step_a] starting")
        # ... your work ...
        path = self.outputs(ctx)[0]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("...", encoding="utf-8")
        logger.info("[step_a] wrote %s", path)
```

Three rules:
- **`outputs` declares what the step produces.** The runner uses it for idempotency. If a file exists, the step is skipped on the next run unless you pass `force=True`.
- **`run` should be safe to re-execute.** Test by running the pipeline twice and confirming the second run is a no-op.
- **Long-running steps must not hold a DB session across LLM calls.** Per the user's "no DB lock over LLM calls" rule, this cascades into lock errors across the whole app. Take the data, close the session, do the LLM work, reopen for writes.

## The Pipeline class

```python
# app/assistant/pipelines/my_pipeline_id/pipeline.py
from __future__ import annotations
from app.assistant.pipelines.step_runner import PipelineRunner
from app.assistant.pipelines.context import PipelineContext

from app.assistant.pipelines.my_pipeline_id.steps.step_a import StepA
from app.assistant.pipelines.my_pipeline_id.steps.step_b import StepB


class MyPipeline:
    pipeline_id = "my_pipeline_id"

    def run(self, ctx: PipelineContext, *, force: bool = False) -> None:
        runner = PipelineRunner(
            pipeline_id=self.pipeline_id,
            steps=[StepA(), StepB()],
        )
        runner.run(ctx, force=force)
```

## Register the pipeline

The pipeline registry is in `app/assistant/pipelines/__init__.py` or similar — find the existing registry pattern. Typically you add an import and a mapping:

```python
PIPELINES = {
    "daily_insights": DailyInsightsPipeline,
    "kg_pipeline": KGPipeline,
    "my_pipeline_id": MyPipeline,         # <-- new
}
```

## Wire to a routine

Add an entry to `configs/routines.json` (or use the `/routines` admin UI):

```json
{
  "id": "my_pipeline",
  "enabled": true,
  "name": "My pipeline",
  "runner": "pipeline",
  "spec": { "pipeline_id": "my_pipeline_id" },
  "run_policy": { "type": "daily", "time_local": "02:15" },
  "notes": "What this pipeline does and why it runs at this time."
}
```

See [Add a routine](ADD_A_ROUTINE.md) for the full menu of policies and runner types.

## Scope policy

Create `app/assistant/pipelines/my_pipeline_id/scope.json`:

```json
{
  "resources": {
    "allowed_global_resources": ["resource_user_data", "resource_routine_status"],
    "denied_resources": []
  },
  "writes": {
    "write_kg": false,
    "write_unified_log": false
  },
  "approval": {
    "authority_level": 95
  }
}
```

The pipeline runner uses this to construct a ScopeContext for any agents the pipeline invokes. Same narrowing rule applies — see [15_EMI_TEAM_AND_SCOPE.md](../architecture/15_EMI_TEAM_AND_SCOPE.md).

## Test it manually

```bash
.venv\Scripts\python.exe -c "
import app.assistant.tests.test_setup
from app.assistant.pipelines.context import PipelineContext
from app.assistant.pipelines.my_pipeline_id.pipeline import MyPipeline

ctx = PipelineContext(pipeline_id='my_pipeline_id', force=True)
MyPipeline().run(ctx, force=True)
"
```

Or trigger via the routines UI: open `/routines`, find your pipeline, click "Run now."

## Bucket-per-stage architecture (advanced)

If your pipeline has multiple stages and you want each stage to operate independently with its own queue depth metric, look at how `kg_pipeline` is structured (see [09_KG_PIPELINE.md](../architecture/09_KG_PIPELINE.md)). Each stage = one worker, one input bucket (table), one output bucket (table). Workers loop: claim from input → process → write to output. Flushable per-stage. This pattern scales to multi-worker per stage when needed.

## Common pitfalls

- **Output files in unstable paths.** If `outputs` returns paths that include random tokens or timestamps, idempotency is broken — the runner can't tell whether the step ran. Use deterministic paths.
- **Step holds a DB session across an LLM call.** Cascades into lock errors. Pull the data, close the session, do the LLM call, reopen for writes.
- **Step throws but the pipeline continues.** Pipeline runner catches exceptions per-step and writes them to the audit log, but downstream steps still run. If a step's failure should halt the pipeline, raise from `run()` and the runner propagates.
- **Forgot to register the pipeline.** The runner can't find your pipeline_id, the routine fails with "Unknown pipeline_id". Add the import + mapping.

## See also

- [06_PIPELINES_AND_ROUTINES.md](../architecture/06_PIPELINES_AND_ROUTINES.md) — pipeline + routine framework
- [09_KG_PIPELINE.md](../architecture/09_KG_PIPELINE.md) — bucket-per-stage example
- [Add a routine](ADD_A_ROUTINE.md) — scheduling your pipeline
- [15_EMI_TEAM_AND_SCOPE.md](../architecture/15_EMI_TEAM_AND_SCOPE.md) — scope policy details
