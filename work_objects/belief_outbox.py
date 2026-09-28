"""Durable belief outcomes. Only local SQLite writes; no application callbacks.

The mirror of concern_outbox for the belief store. A belief that caused work to exist wants to
know how that work ended: the 2026-09-25 annual-physical case had a belief saying "do not treat
the request as completed until he confirms it is scheduled" while its work object closed itself
on a single delivery, and nothing carried the outcome back either way.

Same contract as the concern lane: the receipt is written inside the SAME transaction as the
terminal status change, so a crash between "work closed" and "belief updated" leaves a pending
row rather than a silent divergence. Delivery is post-commit and best-effort; acknowledging is
the consumer's job once every linked belief has durably received the outcome.
"""
import json
from uuid import uuid4

SCHEMA = """
CREATE TABLE IF NOT EXISTS work_belief_feedback (
    id TEXT PRIMARY KEY, work_id TEXT NOT NULL, outcome TEXT NOT NULL,
    created_at TEXT NOT NULL, payload TEXT NOT NULL
);
"""


class BeliefOutboxMixin:
    def _queue_belief_feedback(self, wo, previous_status, now):
        if wo.status not in {'done', 'abandoned'} or wo.status == previous_status:
            return
        refs = list(dict.fromkeys(str(r).strip() for r in (wo.constraints.get('belief_refs') or [])
                                  if str(r).strip()))
        if not refs:
            return
        goal = wo.nodes.get(wo.goal_node_id)
        constraints = wo.constraints or {}
        payload = {
            'belief_refs': refs,
            'context': {
                'title': wo.title,
                'objective': constraints.get('objective') or '',
                # The criterion the work closed against. The belief's own condition may be
                # stricter, which is exactly what the reader has to be able to notice.
                'success_criteria': constraints.get('success_criteria') or '',
                'completed_at': now,
                'terminal': (goal.payload.get('terminal') or {}) if goal else {},
                'tasks': [{'node_id': n.id, 'title': n.title, 'status': n.status,
                           'finalizer': n.payload.get('finalizer') or {}}
                          for n in wo.nodes.values() if wo.is_work_unit(n)],
            },
            'reply_nodes': [dict(id=n.id, type=n.type, created_by=n.created_by,
                                 created_at=str(n.created_at), content=n.content,
                                 payload={'user_reply': n.payload.get('user_reply')})
                            for n in wo.nodes.values() if n.type == 'evidence'
                            and (n.payload.get('user_reply') or n.created_by == 'reply')],
        }
        self._conn.execute(
            'INSERT INTO work_belief_feedback VALUES(?,?,?,?,?)',
            (uuid4().hex, wo.id, wo.status, now, json.dumps(payload, default=str)))

    def pending_belief_feedback(self, work_id=None):
        with self._lock:
            sql = 'SELECT * FROM work_belief_feedback'
            args = ()
            if work_id is not None:
                sql += ' WHERE work_id=?'
                args = (work_id,)
            rows = self._conn.execute(sql + ' ORDER BY created_at, id', args).fetchall()
            return [{**dict(r), 'payload': json.loads(r['payload'])} for r in rows]

    def acknowledge_belief_feedback(self, receipt_id):
        with self._lock, self._conn:
            self._conn.execute('DELETE FROM work_belief_feedback WHERE id=?', (receipt_id,))
