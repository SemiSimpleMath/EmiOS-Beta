"""LLM call telemetry — one row per LLM API call.

Phase 1 of the token-tracking program (proposal: chat 2026-05-26).
Authoritative per-call usage from API responses, attributed to the
calling agent, with cost in USD pre-computed from a static price table
so daily / weekly rollups are simple GROUP BY sums.

Deliberately stores NO prompt or response text — accounting only.
Prompts are already in agent log files for debugging; storing them
here would balloon table size and carry PII risk.

Open questions for later phases (see todo: "Better token tracking"):
- caller_request_id propagation through nested manager calls
- per-task rollup view in /tokens admin page
- anomaly check vs 7-day baseline
"""

import uuid

from sqlalchemy import Column, Float, Index, Integer, String

from app.assistant.utils.time_utils import AwareUtcDateTime, utc_now
from app.models.base import Base


class LLMCallLog(Base):
    __tablename__ = "llm_call_log"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    ts_utc = Column(AwareUtcDateTime, nullable=False, default=utc_now)

    # Caller attribution. agent_name is the resolved name passed via
    # contextvar from the agent-runtime LLMClient. "(unknown)" when the
    # context isn't set (direct llm_client usage from scripts/tests).
    agent_name = Column(String, nullable=False, index=True)
    caller_request_id = Column(String, nullable=True, index=True)
    caller_manager_id = Column(String, nullable=True)
    caller_scope_id = Column(String, nullable=True)

    # Model + provider. provider is denormalized from engine for fast
    # group-by-provider queries.
    engine = Column(String, nullable=False)
    provider = Column(String, nullable=False)

    # Usage. Pulled from API response usage block; 0 if the response
    # didn't include usage (some streaming paths, some failed calls).
    input_tokens = Column(Integer, nullable=False, default=0)
    output_tokens = Column(Integer, nullable=False, default=0)
    # Cached input tokens (OpenAI returns this as a sub-count of input).
    # Billing impact varies by provider; recorded for visibility but
    # NOT subtracted from input_tokens to keep the schema simple.
    cached_tokens = Column(Integer, nullable=False, default=0)

    # USD cost. Computed at write time from configs/llm_prices.json so
    # rollups don't need a join. Missing engine in price table → 0.
    input_cost_usd = Column(Float, nullable=False, default=0.0)
    output_cost_usd = Column(Float, nullable=False, default=0.0)
    total_cost_usd = Column(Float, nullable=False, default=0.0)

    # Wall-clock duration of the API call. Useful for catching slow calls
    # / runaway-generation patterns (the 32K-char gpt-4.1 runaway we saw
    # earlier this session lasted 3.5 minutes).
    duration_ms = Column(Integer, nullable=False, default=0)

    # ok / timeout / parse_error / quota / other. Set in the finally
    # block of the call site so we capture failures too — they still
    # consume real tokens before failing.
    status = Column(String, nullable=False, default="ok")

    __table_args__ = (
        Index("ix_llm_call_log_ts", "ts_utc"),
        Index("ix_llm_call_log_agent_ts", "agent_name", "ts_utc"),
        Index("ix_llm_call_log_engine_ts", "engine", "ts_utc"),
    )
