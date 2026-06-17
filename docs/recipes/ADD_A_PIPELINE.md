# Recipe: Add a new pipeline

Pipelines are sequential, step-based code that runs once when invoked. Use them for background data processing — daily insights, KG ingestion, belief synthesis, weekly insights, KG maintenance.

If you need a *conversational* loop, write a manager instead. If you need *scheduling* on top of a pipeline, see [Add a routine](ADD_A_ROUTINE.md). If you want something fired by an event rather than a cadence, write an event-triggered routine — not a pipeline.

Read [06_PIPELINES_AND_ROUTINES.md](../architecture/06_PIPELINES_AND_ROUTINES.md) for the framework, and look at `app/assistant/pipelines/daily_insights/` as the canonical small example.

## File layout

```
app/assistant/pipelines/<my_pipeline_id>/
  __init__.py
  pipeline.py                 # the Pipeline class with its steps list
  steps/
    __init__.py
    step_a.py                 # PipelineStep impls
    step_b.py
  scope.yaml                  # optional — permissions if the pipeline calls tools / writes
  README.md                   # optional but recommended
```

## The PipelineStep contract

Each step is a class implementing three methods (`pipelines/step_types.py` defines the `PipelineStep` Protocol):

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
        """Symbolic input names — recorded in the audit JSON, not enforced."""
        return ["unified_log_2026"]

    def outputs(self, ctx: PipelineContext) -> List[Path]:
        """Files this step writes. Idempotency: the runner skips this step
        when ALL these paths already exist (unless force=True)."""
        return [ctx.day_dir / "step_a_result.json"]

    def run(self, ctx: PipelineContext) -> None:
        """Do the work. Read what you need from ctx, write the outputs."""
        logger.info("[step_a] starting")
        # ... your work ...
        path = self.outputs(ctx)[0]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("...", encoding="utf-8")
        logger.info("[step_a] wrote %s", path)
```

Three rules:
- **`outputs` declares what the step produces.** The runner uses it for idempotency (`outputs_exist`): if all output paths exist, the step is skipped on the next run unless you pass `force=True`. A step with empty `outputs` always runs.
- **`run` should be safe to re-execute.** Test by running the pipeline twice and confirming the second run skips (or is a no-op).
- **Long-running steps must not hold a DB session across LLM calls.** Per the "no DB lock over LLM calls" rule, this cascades into lock errors across the whole app. Take the data, close the session, do the LLM work, reopen for writes.

## The Pipeline class

Build the runner with the step list and expose `run(...)`. Match the shipped signature so the `pipeline` routine runner can call it:

```python
# app/assistant/pipelines/my_pipeline_id/pipeline.py
from __future__ import annotations
from typing import Dict, Optional

from app.assistant.pipelines.context import PipelineContext
from app.assistant.pipelines.step_runner import PipelineRunner
from app.assistant.pipelines.my_pipeline_id.steps.step_a import StepA
from app.assistant.pipelines.my_pipeline_id.steps.step_b import StepB


class MyPipeline:
    pipeline_id = "my_pipeline_id"

    def __init__(self) -> None:
        self._runner = PipelineRunner(steps=[StepA(), StepB()])

    def run(
        self,
        *,
        target_date: Optional[str] = None,
        only_steps: Optional[list[str]] = None,
        run_id: Optional[str] = None,
        force: bool = False,
    ) -> Dict:
        ctx = PipelineContext.for_date(
            pipeline_id=self.pipeline_id, target_date=target_date, run_id=run_id,
        )
        result = self._runner.run(ctx, only_steps=only_steps, force=force)
        return {
            "pipeline_id": result.pipeline_id,
            "run_id": result.run_id,
            "date": result.date,
            "status": result.status,
            "audit_path": str(ctx.audit_path()),
            "steps": result.steps,
        }
```

Notes that bite if you get them wrong:
- `PipelineRunner.__init__` takes **only** `steps=[...]` (no `pipeline_id` argument — that lives on the context).
- You never construct `PipelineContext(...)` directly; build it with `PipelineContext.for_date(pipeline_id=..., target_date=..., run_id=...)`. It's a frozen dataclass aligned to the local day-boundary hour (`boundary_hour_local`, default 5, from `configs/dayflow_pipeline.json`) and provides `day_dir`, `snapshots_dir`, `pipeline_runs_dir`, `since_utc`/`until_utc`, and `audit_path()`.
- The runner is sequential and idempotent, stops on the first step that raises (status `error`), and **always** writes an audit JSON to `ctx.audit_path()` (`<day_dir>/pipeline_runs/<run_id>.json`), then prunes that dir to the most recent 1500 files.

## Register the pipeline

The registry is `app/assistant/pipelines/pipeline_registry.py`. It registers lazily so a missing optional dependency logs-and-skips rather than crashing boot. Add a `_try_register` call inside `_ensure_defaults_registered()`:

```python
def _ensure_defaults_registered() -> None:
    ...
    _try_register("my_pipeline_id", lambda: __import__(
        "app.assistant.pipelines.my_pipeline_id.pipeline", fromlist=["MyPipeline"]
    ).MyPipeline())
```

Routines (and other callers) resolve it via `resolve_pipeline("my_pipeline_id")`. There is no `PIPELINES = {...}` dict to edit.

## Wire to a routine

Routines are one file per routine. Add `configs/routines/public/my_pipeline.json` (or `private/` for personal):

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

See [Add a routine](ADD_A_ROUTINE.md) for the full menu of triggers, policies, windows, and guards. `daily_insights_pipeline.json` is a live example of a `pipeline`-runner routine.

## Scope policy (optional)

A pipeline OPTIONALLY declares its permissions in `app/assistant/pipelines/my_pipeline_id/scope.yaml` (permission-only; identity — `owner_id`, `actor_id`, `surface` — is stamped per run by the caller via `load_scope_for_source`, never authored here):

```yaml
approval:
  authority_level: 0
tools:
  allowed_tools: []            # fail-closed; list tools only if the pipeline calls them
resources:
  allowed_global_resources: [all]
  resource_groups: [chat, memory]
pods:
  allowed_scopes: [self]
writes:
  write_unified_log: true
  write_kg: false
  allow_fact_extraction: false
```

Load it once at run start and thread it through steps:

```python
from app.assistant.scope.loader import load_scope_for_source
scope = load_scope_for_source(kind="pipeline", source_id="my_pipeline_id")
```

The caller-supersedes-narrow-only rule applies — see [15_EMI_TEAM_AND_SCOPE.md](../architecture/15_EMI_TEAM_AND_SCOPE.md).

## Test it manually

```bash
.venv\Scripts\python.exe -c "
import app.assistant.tests.test_setup
from app.assistant.pipelines.my_pipeline_id.pipeline import MyPipeline

result = MyPipeline().run(force=True)
print(result['status'], result['audit_path'])
"
```

Or trigger via the routines UI: open `/routines`, find your pipeline, click "Run now."

## Bucket-per-stage architecture (advanced)

If your pipeline has multiple stages and you want each stage to operate independently with its own queue-depth metric, look at how `kg_pipeline` is structured (see [09_KG_PIPELINE.md](../architecture/09_KG_PIPELINE.md)). Each stage = one worker, one input bucket (table), one output bucket (table); workers loop claim → process → write. This pattern scales to multi-worker per stage when needed.

## Common pitfalls

- **Output files in unstable paths.** If `outputs` returns paths containing random tokens or timestamps, idempotency breaks — the runner can't tell whether the step ran. Use deterministic paths under `ctx.day_dir`.
- **Step holds a DB session across an LLM call.** Cascades into lock errors. Pull the data, close the session, do the LLM call, reopen for writes.
- **Step raises and you expected the rest to run.** The runner **stops** on the first failing step (`overall_status="error"`, `break`) and records the partial run. If a step is best-effort, swallow inside its own `run` — don't rely on the runner continuing.
- **Forgot to register the pipeline.** `resolve_pipeline(id)` returns `None` and the routine fails with an unknown-pipeline error. Add the `_try_register` line.
- **Passed `pipeline_id=` to `PipelineRunner`.** It only accepts `steps=`. The id lives on the `PipelineContext`.

## See also

- [06_PIPELINES_AND_ROUTINES.md](../architecture/06_PIPELINES_AND_ROUTINES.md) — pipeline + routine framework
- [09_KG_PIPELINE.md](../architecture/09_KG_PIPELINE.md) — bucket-per-stage example
- [Add a routine](ADD_A_ROUTINE.md) — scheduling your pipeline
- [15_EMI_TEAM_AND_SCOPE.md](../architecture/15_EMI_TEAM_AND_SCOPE.md) — scope policy details
