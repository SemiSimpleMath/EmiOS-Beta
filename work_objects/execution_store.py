"""Minimal durable receipts for attempt ownership and uncertain external calls.

Graph mutations and these admission checks use the same SQLite write transaction.
No transaction spans a network/model call. This is not an exactly-once guarantee.
"""
import json
import os
import psutil
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS work_execution_attempts (
 work_id TEXT NOT NULL, node_id TEXT NOT NULL, epoch INTEGER NOT NULL,
 process_id TEXT NOT NULL, pid INTEGER NOT NULL, process_started REAL NOT NULL,
 state TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0, reason TEXT,
 updated_at TEXT NOT NULL, PRIMARY KEY(work_id,node_id,epoch));
CREATE TABLE IF NOT EXISTS work_execution_calls (
 id TEXT PRIMARY KEY, work_id TEXT NOT NULL, node_id TEXT NOT NULL, epoch INTEGER NOT NULL,
 tool_name TEXT NOT NULL, external INTEGER NOT NULL, state TEXT NOT NULL,
 detail TEXT, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS work_execution_calls_owner ON work_execution_calls(work_id,node_id,epoch,state);
"""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _key(owner):
    return owner.work_id, owner.main_node_id, owner.dispatch_epoch


class ExecutionBlocked(ValueError):
    """A prior attempt must settle before another can be admitted."""


class ExecutionStoreMixin:
    def _execution_claim(self, wid, nid, epoch):
        from app.assistant.manager_runtime.execution import PROCESS_ID
        self._execution_reconcile_dead(wid)
        blocked = self._conn.execute(
            "SELECT 1 FROM work_execution_attempts WHERE work_id=? AND state IN ('reserved','running') "
            "UNION ALL SELECT 1 FROM work_execution_calls WHERE work_id=? AND state IN ('in_flight','unknown') LIMIT 1",
            (wid, wid)).fetchone()
        if blocked:
            raise ExecutionBlocked("dispatch blocked: prior execution is still running or its external outcome is unknown")
        self._conn.execute("INSERT INTO work_execution_attempts VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (wid, nid, epoch, PROCESS_ID, os.getpid(), psutil.Process().create_time(),
                            "reserved", 0, None, _now()))

    def _execution_reconcile_dead(self, wid):
        for row in self._conn.execute("SELECT * FROM work_execution_attempts WHERE work_id=? AND state IN ('reserved','running')", (wid,)).fetchall():
            try:
                process = psutil.Process(row['pid'])
                dead = process.create_time() != row['process_started'] or not process.is_running()
            except psutil.NoSuchProcess:
                dead = True
            except psutil.AccessDenied:
                dead = False  # Cannot prove exit; preserve the barrier.
            if dead:
                key = (wid, row['node_id'], row['epoch'])
                self._conn.execute("UPDATE work_execution_attempts SET state='interrupted',revoked=1,reason='process exited',updated_at=? WHERE work_id=? AND node_id=? AND epoch=?", (_now(), *key))
                self._conn.execute("UPDATE work_execution_calls SET state=CASE WHEN external=1 THEN 'unknown' ELSE 'settled' END,updated_at=? WHERE work_id=? AND node_id=? AND epoch=? AND state='in_flight'", (_now(), *key))

    def start_execution(self, owner):
        from app.assistant.manager_runtime.execution import PROCESS_ID
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute("SELECT * FROM work_execution_attempts WHERE work_id=? AND node_id=? AND epoch=?", _key(owner)).fetchone()
            if row is None:
                self._execution_claim(*_key(owner))  # Legacy direct dispatch.
            elif row['state'] != 'reserved' or row['process_id'] != PROCESS_ID or row['revoked']:
                raise ValueError("attempt already executing, interrupted, or revoked")
            self._execution_validate(owner)
            self._conn.execute("UPDATE work_execution_attempts SET state='running',updated_at=? WHERE work_id=? AND node_id=? AND epoch=?", (_now(), *_key(owner)))

    def finish_execution(self, owner):
        with self._lock, self._conn:
            self._conn.execute("UPDATE work_execution_attempts SET state='exited',updated_at=? WHERE work_id=? AND node_id=? AND epoch=?", (_now(), *_key(owner)))

    def _execution_validate(self, owner, wo=None):
        from app.assistant.manager_runtime.execution import ExecutionCancelled
        wo = wo or self._load(owner.work_id)
        node = wo.nodes.get(owner.main_node_id)
        row = self._conn.execute("SELECT revoked,state FROM work_execution_attempts WHERE work_id=? AND node_id=? AND epoch=?", _key(owner)).fetchone()
        if (wo.id != owner.work_id or wo.status in {'done','abandoned'} or node is None
                or wo.constraints.get('pending_work_closure')
                or node.status != 'dispatched'
                or int(node.payload.get('dispatch_epoch') or 0) != owner.dispatch_epoch
                or (row and (row['revoked'] or row['state'] not in {'reserved', 'running'}))):
            raise ExecutionCancelled("work attempt is no longer authorized")

    def revoke_execution(self, owner, reason):
        with self._lock, self._conn:
            self._conn.execute("UPDATE work_execution_attempts SET revoked=1,reason=?,updated_at=? WHERE work_id=? AND node_id=? AND epoch=?", (reason, _now(), *_key(owner)))

    def execution_revoked(self, owner):
        # Controllers must still run the finalizer after normal result recording.
        # Worker writes and tool admission separately require dispatched status.
        with self._lock:
            row = self._conn.execute("SELECT revoked,state FROM work_execution_attempts WHERE work_id=? AND node_id=? AND epoch=?", _key(owner)).fetchone()
            pending = self._conn.execute(
                "SELECT json_extract(constraints, '$.pending_work_closure') FROM work_objects WHERE id=?",
                (owner.work_id,)).fetchone()
            return bool((pending and pending[0])
                        or (row and (row['revoked'] or row['state'] not in {'reserved','running'})))

    def admit_execution_call(self, owner, call_id, name, external):
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            self._execution_validate(owner)
            self._conn.execute("INSERT INTO work_execution_calls VALUES(?,?,?,?,?,?,?,?,?)",
                (call_id, *_key(owner), name, int(external), 'in_flight', None, _now()))

    def finish_execution_call(self, call_id, state, detail):
        with self._lock, self._conn:
            self._conn.execute("UPDATE work_execution_calls SET state=?,detail=?,updated_at=? WHERE id=?",
                               (state, detail[:2000], _now(), call_id))

    def resolve_execution_call(self, call_id, *, evidence):
        """Operator/reconciler API. Require actual outcome evidence before releasing a barrier."""
        if not str(evidence or '').strip():
            raise ValueError("resolution evidence is required")
        with self._lock, self._conn:
            cursor = self._conn.execute("UPDATE work_execution_calls SET state='settled',detail=?,updated_at=? WHERE id=? AND state='unknown'", (str(evidence), _now(), call_id))
            if cursor.rowcount != 1:
                raise ValueError("only an uncertain, exited call can be reconciled")

    def reconcile_all_executions(self):
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            ids = [r[0] for r in self._conn.execute("SELECT DISTINCT work_id FROM work_execution_attempts WHERE state IN ('reserved','running')")]
            for work_id in ids:
                self._execution_reconcile_dead(work_id)

    def reconcile_execution(self, work_id):
        """Persist proven process exits independently of a later rejected claim."""
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            self._execution_reconcile_dead(work_id)

    def execution_running(self, owner):
        self.reconcile_execution(owner.work_id)
        with self._lock:
            row = self._conn.execute("SELECT state FROM work_execution_attempts WHERE work_id=? AND node_id=? AND epoch=?", _key(owner)).fetchone()
            return bool(row and row['state'] in {'reserved', 'running'})

    def settle_recovered_ticket(self, owner, ticket_id):
        """An existing ticket's recorded outcome resolves only its own wait receipt."""
        self.reconcile_execution(owner.work_id)
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            node = self._load(owner.work_id).nodes[owner.main_node_id]
            if node.payload.get('ticket_id') != ticket_id or node.payload.get('ticket_epoch') != owner.dispatch_epoch:
                raise ValueError("ticket does not belong to this attempt")
            self._conn.execute("UPDATE work_execution_calls SET state='settled',detail=?,updated_at=? WHERE work_id=? AND node_id=? AND epoch=? AND tool_name='create_dayflow_ticket' AND state='unknown'", ('Recovered existing ticket ' + ticket_id, _now(), *_key(owner)))

    def execution_status(self, work_id=None, *, unresolved_only=False):
        with self._lock:
            suffix, args = (' WHERE work_id=?', (work_id,)) if work_id else ('', ())
            result = {}
            for name in ('attempts', 'calls'):
                query = 'SELECT * FROM work_execution_' + name + suffix
                if unresolved_only:
                    query += (' AND ' if suffix else ' WHERE ') + ("state IN ('reserved','running')" if name == 'attempts' else "state IN ('in_flight','unknown')")
                result[name] = [dict(row) for row in self._conn.execute(query, args)]
            return result

    def _execution_after_mutation(self, wo, op, data, actor):
        revoked = False
        if wo.status == 'abandoned':
            revoked = True
            self._conn.execute("UPDATE work_execution_attempts SET revoked=1,reason='work abandoned',updated_at=? WHERE work_id=? AND state IN ('reserved','running')", (_now(), wo.id))
        for node in wo.nodes.values():
            if wo.is_work_unit(node) and (node.status in {'abandoned','superseded'} or (op == 'record_result' and actor == 'dispatch_sweeper' and node.id == data.get('node_id'))):
                revoked = True
                self._conn.execute("UPDATE work_execution_attempts SET revoked=1,reason=?,updated_at=? WHERE work_id=? AND node_id=? AND epoch=?", ('task abandoned or timed out', _now(), wo.id, node.id, int(node.payload.get('dispatch_epoch') or 0)))
        # A failed launch can finish before any thread starts. Release its reservation
        # only when a fenced result has actually been committed.
        if op == 'record_result':
            self._conn.execute("UPDATE work_execution_attempts SET state='exited',updated_at=? WHERE work_id=? AND node_id=? AND state='reserved'", (_now(), wo.id, data.get('node_id')))
        return revoked
