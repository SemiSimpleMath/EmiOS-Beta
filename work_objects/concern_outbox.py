"""Durable concern outcomes. Only local SQLite writes; no application callbacks."""
import json
from uuid import uuid4

SCHEMA = """
CREATE TABLE IF NOT EXISTS work_concern_feedback (
    id TEXT PRIMARY KEY, work_id TEXT NOT NULL, outcome TEXT NOT NULL,
    created_at TEXT NOT NULL, payload TEXT NOT NULL
);
"""


class ConcernOutboxMixin:
    def _queue_concern_feedback(self, wo, previous_status, now):
        if wo.status not in {'done', 'abandoned'} or wo.status == previous_status:
            return
        refs = list(dict.fromkeys(str(r).strip() for r in (wo.constraints.get('concern_refs') or []) if str(r).strip()))
        if not refs:
            return
        goal = wo.nodes.get(wo.goal_node_id)
        payload = {
            'concern_refs': refs,
            'context': {
                'title': wo.title,
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
            'INSERT INTO work_concern_feedback VALUES(?,?,?,?,?)',
            (uuid4().hex, wo.id, wo.status, now, json.dumps(payload, default=str)))

    def pending_concern_feedback(self, work_id=None):
        with self._lock:
            sql = 'SELECT * FROM work_concern_feedback'
            args = ()
            if work_id is not None:
                sql += ' WHERE work_id=?'
                args = (work_id,)
            rows = self._conn.execute(sql + ' ORDER BY created_at, id', args).fetchall()
            return [{**dict(r), 'payload': json.loads(r['payload'])} for r in rows]

    def acknowledge_concern_feedback(self, receipt_id):
        with self._lock, self._conn:
            self._conn.execute('DELETE FROM work_concern_feedback WHERE id=?', (receipt_id,))
