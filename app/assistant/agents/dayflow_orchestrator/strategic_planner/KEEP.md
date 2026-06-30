# RETIRED — kept as reference (do not delete)

This agent (`dayflow_orchestrator::strategic_planner`) is the LEGACY items-lane evaluator. It is no longer
wired into the dayflow manager — its driving control nodes (`StrategicPlannerPrepNode`, `PlannerPersistNode`)
were deleted and it was removed from `dayflow_orchestrator_manager`'s agents list + state_map. The live
evaluator is `strategic_planner_wo` (the work-object steward).

`prompts/system.j2` + `prompts/user.j2` are KEPT DELIBERATELY (project-owner directive): they hold the original
evaluator's plan / DAG / task-decomposition prompt logic, worth preserving as reference. Do not delete them
in a dead-code sweep.
