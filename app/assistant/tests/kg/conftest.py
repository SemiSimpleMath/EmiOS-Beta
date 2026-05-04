"""Shared fixtures for KG promoter / proposal tests.

Sets USE_TEST_DB and a kg-specific TEST_DB_NAME *before* any project import
so every DB call hits an isolated SQLite file, then forces the schema to
include every KG model (Node/Edge, claim_proposal*, kg_node_evidence,
kg_edge_evidence, etc.) so cross-conftest test ordering can't leave us
with a partial schema.

The dayflow conftest sets its own TEST_DB_NAME at import time. Whichever
imports first wins for the shared os.environ key, so tests that need a
particular schema must explicitly create their own tables on each run.
"""
from __future__ import annotations

import os

# ── Force test DB before any project import ──────────────────────────────
# The dayflow conftest also sets TEST_DB_NAME at import time; whichever
# conftest pytest imports first wins. We force ours regardless of what was
# already there — it doesn't matter that a separate suite uses a different
# DB file because the suites don't share data anyway.
os.environ["USE_TEST_DB"] = "true"
os.environ["TEST_DB_NAME"] = "test_kg_promoter"


import pytest

# Bootstrap DI (agents, tools, managers, resources, blackboard).
import app.assistant.tests.test_setup  # noqa: F401

# Import every model module that contributes tables we exercise here.
# This forces them to register with the shared Base.metadata before we
# call create_all — otherwise dayflow-only setup runs first and leaves
# the kg tables out of the freshly-created schema.
import app.assistant.kg.db.knowledge_graph_db_sqlite  # noqa: F401  Node, Edge
import app.assistant.database.kg_chat_projection      # noqa: F401  KGNodeEvidence, KGEdgeEvidence
import app.assistant.database.claim_proposals         # noqa: F401  ClaimProposal*
import app.assistant.database.db_handler              # noqa: F401  UnifiedLog2026 + others

from app.models.base import Base, get_session


@pytest.fixture(autouse=True)
def kg_clean_db():
    """Recreate every Base.metadata table before each test, then truncate
    the ones we touch. Idempotent — re-running tests doesn't accumulate.
    """
    session = get_session()
    engine = session.bind
    Base.metadata.create_all(engine)
    session.close()

    # Truncate only the tables our tests write to. Other tables (UnifiedLog2026,
    # taxonomy, etc.) stay alone so we don't fight neighbor suites.
    from app.assistant.database.kg_chat_projection import KGEdgeEvidence, KGNodeEvidence
    from app.assistant.database.claim_proposals import (
        ClaimProposal, ClaimProposalEdge, ClaimProposalEvidence, ClaimProposalNode,
    )
    from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node

    session = get_session()
    try:
        for tbl in (
            KGNodeEvidence, KGEdgeEvidence,
            ClaimProposalEdge, ClaimProposalEvidence, ClaimProposalNode, ClaimProposal,
            Edge, Node,
        ):
            session.query(tbl).delete()
        session.commit()
    finally:
        session.close()
    yield
