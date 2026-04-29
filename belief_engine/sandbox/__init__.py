"""
Belief Engine sandbox — isolated test harness for decay logic.

Runs against a temporary SQLite file (NOT emi.db). Lets you seed beliefs with
specific scopes and last_confirmed dates, advance synthetic time, run decay
steps, and observe what changes.

Usage from a script:

    from belief_engine.sandbox.sandbox import BeliefSandbox
    from datetime import datetime, timedelta, timezone

    with BeliefSandbox() as sb:
        sb.seed_belief(key="routine.coffee", scope="temporary",
                       last_confirmed=sb.days_ago(31), domain="routine")
        sb.run_decay(domain="routine", now_utc=datetime.now(timezone.utc))
        sb.print_state(domain="routine")

Or run scripted scenarios:

    python -m belief_engine.sandbox.scenarios

The harness sets DEV_DATABASE_URI_EMI to a temp file before any belief module
is imported. This isolates the sandbox from emi.db.
"""
