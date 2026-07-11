"""Task runtime — the pipeline runner that executes a compiled task as a durable work object.

A task is a template work object; running it clones a durable instance that THIS runner drives.
Unlike dayflow (a paced, LLM-per-tick autonomous room), the task runner is a lean pipeline: it
evaluates the work object's ready-set, fans the frontier out in parallel, dispatches deterministic
`tool` nodes inline (gated) and `action` nodes to managers, and parks only at genuine waits — no
scheduler round-trip between nodes. Conditionals live in the graph as per-node guards; loops are
re-arm; completion is reach-end. See scratch/TASK_TO_WORKOBJECT_REFACTOR_PLAN.md.
"""
