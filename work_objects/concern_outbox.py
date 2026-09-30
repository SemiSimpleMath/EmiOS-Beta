"""Durable concern feedback. Only local SQLite writes; no application callbacks.

A concern the work cites keeps a record of that work (owner, 2026-09-30: "the concern does not become
a work object, it has a work object attached to it now so we can see how it is progressing"). Each
change the concern must hear about is a receipt written in the same transaction as the change:

- `attached`  the work cites a concern it did not cite before (created citing it, or revised to);
- `judged`    the finalizer judged one of the work's tasks: the verdict, its account, and the owner's
              replies that task received, verbatim;
- `done` / `abandoned`  the work ended.

Receipts of one transaction keep their order (the id starts with its position). Delivery lives in
app/assistant/subconscious/concern_feedback.py.
"""
import json
from uuid import uuid4

SCHEMA = """
CREATE TABLE IF NOT EXISTS work_concern_feedback (
    id TEXT PRIMARY KEY, work_id TEXT NOT NULL, outcome TEXT NOT NULL,
    created_at TEXT NOT NULL, payload TEXT NOT NULL
);
"""


def _concern_refs(wo):
    return list(dict.fromkeys(str(r).strip() for r in ((wo.constraints or {}).get('concern_refs') or [])
                              if str(r).strip()))


def concern_snapshot(wo):
    """What the receipts are computed against: the refs cited and each task's last judgment time.
    `wo` is None before a create."""
    if wo is None:
        return {'refs': [], 'judged': {}}
    return {'refs': _concern_refs(wo),
            'judged': {n.id: (n.payload.get('finalizer') or {}).get('at') for n in wo.nodes.values()}}


def _replies(wo, node_id):
    """The owner's replies recorded under one task (its own evidence records), verbatim."""
    return [n.payload['user_reply'] for n in wo.provenance_for(node_id)
            if n.type == 'evidence' and isinstance(n.payload.get('user_reply'), dict)]


class ConcernOutboxMixin:
    def _queue_concern_feedback(self, wo, previous_status, now, before):
        refs = _concern_refs(wo)
        if not refs:
            return
        rows = []
        added = [r for r in refs if r not in before['refs']]
        if added:
            constraints = wo.constraints or {}
            rows.append(('attached', {'concern_refs': added, 'work': {
                'title': wo.title, 'objective': constraints.get('objective') or wo.title,
                'success_criteria': constraints.get('success_criteria') or '', 'attached_at': now}}))
        for node in wo.nodes.values():
            fin = node.payload.get('finalizer') or {}
            if wo.is_work_unit(node) and fin and fin.get('at') != before['judged'].get(node.id):
                rows.append(('judged', {'concern_refs': refs, 'work': {'title': wo.title}, 'task': {
                    'node_id': node.id, 'title': node.title, 'status': node.status,
                    'finalizer': fin, 'replies': _replies(wo, node.id)}}))
        if wo.status in {'done', 'abandoned'} and wo.status != previous_status:
            goal = wo.nodes.get(wo.goal_node_id)
            rows.append((wo.status, {
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
            }))
        for position, (kind, payload) in enumerate(rows):
            self._conn.execute(
                'INSERT INTO work_concern_feedback VALUES(?,?,?,?,?)',
                (f'{position:03d}-{uuid4().hex}', wo.id, kind, now, json.dumps(payload, default=str)))

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
