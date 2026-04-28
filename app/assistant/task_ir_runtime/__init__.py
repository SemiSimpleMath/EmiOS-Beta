from __future__ import annotations

from typing import Optional

from app.assistant.task_ir_runtime.task_ir_runner import TaskIRRunner

_task_ir_runner: Optional[TaskIRRunner] = None


def get_task_ir_runner() -> TaskIRRunner:
    global _task_ir_runner
    if _task_ir_runner is None:
        _task_ir_runner = TaskIRRunner()
    return _task_ir_runner

