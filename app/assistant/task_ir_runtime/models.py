from __future__ import annotations

from sqlalchemy import Column, JSON, String, TIMESTAMP, func

from app.models.base import Base


class TaskIRRunRow(Base):
    __tablename__ = "task_ir_run"

    run_id = Column(String, primary_key=True)
    task_id = Column(String, nullable=False, index=True)
    status = Column(String, nullable=False, index=True)
    source_task_file = Column(String, nullable=True)
    compiled_task_json = Column(JSON, nullable=False)
    context_json = Column(JSON, nullable=False, default=dict)
    current_step_id = Column(String, nullable=False)
    waiting_event_name = Column(String, nullable=True, index=True)
    last_error = Column(String, nullable=True)
    created_at_utc = Column(String, nullable=False)
    started_at_utc = Column(String, nullable=False)
    finished_at_utc = Column(String, nullable=True)
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, default=func.now(), onupdate=func.now())


class TaskIRWaitSubscriptionRow(Base):
    __tablename__ = "task_ir_wait_subscription"

    id = Column(String, primary_key=True)
    run_id = Column(String, nullable=False, index=True)
    event_name = Column(String, nullable=False, index=True)
    created_at_utc = Column(String, nullable=False)
