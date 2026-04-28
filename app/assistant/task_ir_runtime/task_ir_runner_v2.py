# 1. task_ir_runner.py
#
# Keep only the high-level orchestration here.
#
# This file should own:
#
# TaskIRRunner
#
# start_run
#
# resume_run
#
# _tick
#
# _fail_run
#
# _require_run
#
# It should mostly:
#
# load run state
#
# ask other components to do work
#
# persist updates
#
# move between steps
#
# Think of this file as the conductor, not the orchestra.

# class TaskIRRunner:
#     def __init__(self, ..., store=None):
#         self._store = store or TaskIRStateStore()
#         self._validator = TaskIRValidator()
#         self._state = TaskIRStateManager()
#         self._conditions = TaskIRConditionEvaluator()
#         self._actions = TaskIRActionExecutor(...)
#         self._waits = TaskIRWaitManager(...)
#         self._timers = TaskIRTimerManager(...)