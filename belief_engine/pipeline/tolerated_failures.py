"""Escalate a tolerated belief-update failure that keeps coming back.

A single malformed belief output is noise: it is skipped, not written, and the next night's
run re-collects the same evidence and tries again, so a one-off model slip heals itself. The
SAME belief failing run after run is a defect, and nothing else will surface it — a tolerated
failure makes the run SUCCEED, so the routine's own 3-strike auto-disable ticket can never
fire for it (2026-09-25).

The tally lives in the belief db beside the matching receipts, under one table created by
`matching.history.SCHEMA`.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.assistant.utils.logging_config import get_logger
from belief_engine.matching.history import connection

logger = get_logger(__name__)

# Matches the routine manager's own failure budget, so "persistent" means the same thing to
# the owner whichever path reports it.
THRESHOLD = 3


def record_run(path, failed) -> list[dict]:
    """Fold ONE run's tolerated failures into the cross-run tally.

    Returns the beliefs that have now failed `THRESHOLD` consecutive runs. A belief absent
    from this run's failures is cleared: it either wrote cleanly or produced no output this
    time, and either way it is not currently stuck.
    """
    now = datetime.now(timezone.utc).isoformat()
    current = {}
    for entry in failed or []:
        key = (entry or {}).get('belief_key')
        if key and key != '?':
            current[key] = str((entry or {}).get('error') or '')[:300]

    with connection(path, initialize=True) as conn:
        conn.execute('BEGIN IMMEDIATE')
        if current:
            marks = ','.join('?' * len(current))
            conn.execute(
                f'DELETE FROM belief_update_failures WHERE belief_key NOT IN ({marks})',
                tuple(current),
            )
        else:
            conn.execute('DELETE FROM belief_update_failures')
        for key, error in current.items():
            conn.execute(
                '''INSERT INTO belief_update_failures
                   (belief_key, consecutive_runs, first_seen, last_seen, last_error)
                   VALUES (?,1,?,?,?)
                   ON CONFLICT(belief_key) DO UPDATE SET
                     consecutive_runs = consecutive_runs + 1,
                     last_seen = excluded.last_seen,
                     last_error = excluded.last_error''',
                (key, now, now, error),
            )
        rows = conn.execute(
            'SELECT belief_key, consecutive_runs, first_seen, last_error '
            'FROM belief_update_failures WHERE consecutive_runs >= ? ORDER BY belief_key',
            (THRESHOLD,),
        ).fetchall()
    return [dict(row) for row in rows]


def surface_ticket(persistent) -> None:
    """Tell the owner about beliefs that are reliably failing, once they cross the threshold.

    Best-effort by design: a missing ticket manager must not fail a belief run that otherwise
    succeeded. The ERROR log above it is the durable record either way.
    """
    if not persistent:
        return
    try:
        from app.assistant.ServiceLocator.service_locator import DI
        tm = getattr(DI, 'ticket_manager', None)
    except Exception:
        tm = None
    if tm is None:
        logger.warning('[belief_engine] ticket_manager unavailable; %d persistently failing '
                       'belief(s) reported to the log only', len(persistent))
        return

    listed = '\n'.join(
        f"- {row['belief_key']} ({row['consecutive_runs']} runs): {row['last_error']}"
        for row in persistent)
    try:
        ticket = tm.create_ticket(
            ticket_type='dayflow_notify',
            suggestion_type='belief_update_persistently_failing',
            title=f'Belief engine: {len(persistent)} belief(s) failing every run',
            message=('These beliefs have been skipped on every run for at least '
                     f'{THRESHOLD} runs, so their evidence is not being recorded. The belief '
                     f'engine itself is still running.\n\n{listed}'),
            action_type='none',
            action_params=None,
            trigger_context={'belief_keys': [row['belief_key'] for row in persistent],
                             'threshold': THRESHOLD},
            trigger_reason='belief_update_persistent_failure',
            valid_hours=168,
        )
        if ticket is not None and hasattr(tm, 'mark_proposed'):
            tm.mark_proposed(ticket.ticket_id)
    except Exception as exc:
        logger.error('[belief_engine] create_ticket failed for persistent belief failures: %s',
                     exc, exc_info=True)
