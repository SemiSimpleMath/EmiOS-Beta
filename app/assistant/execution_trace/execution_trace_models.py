"""SQLAlchemy models for execution trace persistence."""
from sqlalchemy import Column, String, Integer, Float, Boolean, DateTime, JSON, Text, Index
from app.models.base import Base


class ExecutionTraceRun(Base):
    __tablename__ = "execution_trace_runs"

    run_id = Column(String, primary_key=True)
    task_id = Column(String, index=True, nullable=True)
    manager_name = Column(String, nullable=False)
    task_text = Column(String(500), nullable=True)
    start_time = Column(DateTime, nullable=False)
    end_time = Column(DateTime, nullable=True)
    exit_reason = Column(String, nullable=True)
    success = Column(Boolean, default=False)
    total_steps = Column(Integer, default=0)
    total_critic_interventions = Column(Integer, default=0)
    total_replans = Column(Integer, default=0)
    final_answer_preview = Column(String(200), nullable=True)
    trace_artifact_path = Column(String, nullable=True)
    metadata_json = Column(JSON, nullable=True)


class ExecutionTraceStep(Base):
    __tablename__ = "execution_trace_steps"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String, index=True, nullable=False)
    step_number = Column(Integer, default=0)
    agent_name = Column(String, nullable=True)
    action = Column(String, nullable=True)
    action_input_preview = Column(String(300), nullable=True)
    tool_name = Column(String, nullable=True, index=True)
    tool_result_type = Column(String, nullable=True)
    tool_result_preview = Column(Text, nullable=True)
    tool_timing_ms = Column(Float, nullable=True)
    page_url = Column(String, nullable=True)
    selectors_used_json = Column(JSON, nullable=True)
    timestamp = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_trace_step_run_step", "run_id", "step_number"),
    )
