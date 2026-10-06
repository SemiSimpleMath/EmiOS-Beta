"""Durable work feedback. Only local SQLite writes; no application callbacks.

A concern the work cites keeps a record of that work (owner, 2026-09-30: "the concern does not become
a work object, it has a work object attached to it now so we can see how it is progressing"). And
whatever is done reaches the brain, which attaches it to every concern it bears on (owner, 2026-10-03,
docs/design/what_was_done_reaches_the_brain_2026-10-03.md), so every work object's judgments and ending
are receipts, whether it cites a concern or not. Each is written in the same transaction as the change:

- `attached`  the work cites a concern it did not cite before (created citing it, or revised to);
- `judged`    the finalizer judged one of the work's tasks: the verdict, its account, the owner's
              replies that task received, verbatim, and the research findings it recorded (every work
              object);
- `done` / `abandoned`  the work ended (every work object).

A judged or ended receipt carries the work's progress at that moment (work_objects/progress.py: what is
done, what is still to come and what each step waits for), so whoever reads the report sees whether the
work goes on (docs/design/work_progress_for_outside_readers_2026-10-04.md).

Receipts of one transaction keep their order (the id starts with its position). Delivery lives in
app/assistant/subconscious/concern_feedback.py.
"""
import json
from uuid import uuid4

from work_objects.progress import work_progress

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


def _finding_refs(wo, node_id):
    """The research-finding pods recorded on one task or under it (pod_ref on the task or its own records)."""
    nodes = [wo.nodes[node_id], *wo.provenance_for(node_id)]
    return list(dict.fromkeys(n.pod_ref for n in nodes
                              if str(n.pod_ref or '').startswith('datapod:research_finding:')))


class ConcernOutboxMixin:
    def _queue_concern_feedback(self, wo, previous_status, now, before):
        refs = _concern_refs(wo)
        constraints = wo.constraints or {}
        work = {'title': wo.title, 'objective': constraints.get('objective') or wo.title}
        rows = []
        progress = work_progress(wo)
        added = [r for r in refs if r not in before['refs']]
        if added:
            rows.append(('attached', {'concern_refs': added, 'work': {
                'title': wo.title, 'objective': constraints.get('objective') or wo.title,
                'success_criteria': constraints.get('success_criteria') or '', 'attached_at': now}}))
        for node in wo.nodes.values():
            fin = node.payload.get('finalizer') or {}
            if wo.is_work_unit(node) and fin and fin.get('at') != before['judged'].get(node.id):
                rows.append(('judged', {'concern_refs': refs, 'work': work, 'task': {
                    'node_id': node.id, 'title': node.title, 'status': node.status,
                    'finalizer': fin, 'replies': _replies(wo, node.id),
                    'finding_refs': _finding_refs(wo, node.id)}, 'progress': progress}))
        if wo.status in {'done', 'abandoned'} and wo.status != previous_status:
            goal = wo.nodes.get(wo.goal_node_id)
            rows.append((wo.status, {
                'concern_refs': refs,
                'work': work,
                'progress': progress,
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
