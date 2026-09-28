"""One malformed belief output must not cost the whole global pass.

2026-09-25: a single hallucinated `evidence_refs` set on one belief raised inside
`_run_batch`, `update_beliefs.run()` re-raised as a RuntimeError, and the pipeline aborted —
three nights of that auto-disabled the routine. A bad row is now tolerated per belief: not
written, counted truthfully, named at ERROR, and carried out to the run record. Tolerance is
opt-in per step, so a step that does NOT declare it still aborts.
"""
from types import SimpleNamespace

import pytest

from belief_engine.pipeline import pipeline as P
from belief_engine.pipeline.steps import update_beliefs as U
from belief_engine.pipeline.steps.collect_evidence import EvidenceBundle, EvidenceItem


def item(day, ref):
    return EvidenceItem('daily_insights', day, ref, 'confirms', 'source', 'source', 1)


def _ctx(items):
    return SimpleNamespace(scope_context=None, evidence_bundle=EvidenceBundle('all', 'a', 'b', items))


def test_malformed_belief_output_does_not_abort_and_is_named():
    step = U.UpdateBeliefsStep()
    step._run_batch = lambda ctx: {
        'stats': {'updated': 1, 'errors': 1},
        'contested_keys': [],
        'failed_beliefs': [{'belief_key': 'health.movement.hydration.late_evening_overdue',
                            'action': 'update', 'error': 'Invalid or repeated evidence relation'}],
    }
    result = step.run(_ctx([item('2026-09-25', 'one')]))

    # The pass completed rather than raising.
    assert result['status'] == 'ok'
    # The count stays truthful — nothing is hidden from the pipeline's gate.
    assert result['stats']['errors'] == 1
    assert result['stats']['updated'] == 1
    # The tolerance is declared explicitly, and the offending belief is named.
    assert result['incomplete_tolerated'] is True
    assert [f['belief_key'] for f in result['failed_beliefs']] == [
        'health.movement.hydration.late_evening_overdue']


def test_a_failed_batch_no_longer_stops_the_remaining_batches():
    """The old abort fired on the CUMULATIVE error total, so the first bad batch
    ended the run and every later batch was skipped."""
    source = [item('2026-09-01', 'first'), item('2026-09-02', 'second')]
    step = U.UpdateBeliefsStep()
    seen = []

    def batch(ctx):
        records = ctx.evidence_bundle.items
        if len(records) > 1:
            raise U.UpdateContextTooLarge('force one item per batch')
        seen.append(records[0].source_ref)
        if records[0].source_ref == 'first':
            return {'stats': {'errors': 1}, 'contested_keys': [],
                    'failed_beliefs': [{'belief_key': 'bad.one', 'action': 'create', 'error': 'boom'}]}
        return {'stats': {'created': 1}, 'contested_keys': [], 'failed_beliefs': []}

    step._run_batch = batch
    result = step.run(_ctx(source))

    assert seen == ['first', 'second']          # the second batch still ran
    assert result['stats']['created'] == 1      # and its write counted
    assert len(result['failed_beliefs']) == 1


def test_no_failures_leaves_tolerance_off():
    step = U.UpdateBeliefsStep()
    step._run_batch = lambda ctx: {'stats': {'created': 1}, 'contested_keys': [], 'failed_beliefs': []}
    result = step.run(_ctx([item('2026-09-25', 'one')]))
    assert result['incomplete_tolerated'] is False
    assert result['failed_beliefs'] == []


# --- the pipeline gate -------------------------------------------------------

class _Step:
    def __init__(self, name, details, ran):
        self.name, self._details, self._ran = name, details, ran

    def run(self, ctx):
        self._ran.append(self.name)
        return self._details


def _install(monkeypatch, second_details, ran):
    monkeypatch.setattr(P, 'load_scope_for_source', lambda **kw: None, raising=False)
    monkeypatch.setattr('app.assistant.scope.loader.load_scope_for_source', lambda **kw: None)
    for attr, name, details in (
        ('CollectEvidenceStep', 'collect_evidence', {'status': 'ok'}),
        ('UpdateBeliefsStep', 'update_beliefs', second_details),
        ('RecomputeBeliefSnapshotStep', 'recompute_belief_snapshot', {'status': 'ok'}),
        ('ReevaluateBeliefsStep', 'reevaluate_beliefs', {'status': 'ok'}),
        ('CanonicalizeBeliefSetStep', 'canonicalize_belief_set', {'status': 'ok'}),
    ):
        monkeypatch.setattr(P, attr, (lambda n, d: lambda *a, **k: _Step(n, d, ran))(name, details))


def test_pipeline_continues_when_the_step_declares_tolerance(monkeypatch):
    ran = []
    _install(monkeypatch, {'status': 'ok', 'stats': {'errors': 1}, 'incomplete_tolerated': True,
                           'failed_beliefs': [{'belief_key': 'bad.one', 'error': 'boom'}]}, ran)
    result = P.BeliefEnginePipeline().run(run_id='t1')

    assert result['status'] == 'success'
    assert ran[-1] == 'canonicalize_belief_set'          # downstream steps still ran
    assert [t['step'] for t in result['tolerated_failures']] == ['update_beliefs']
    statuses = {s['step']: s['status'] for s in result['steps']}
    assert statuses['update_beliefs'] == 'success_with_tolerated_failures'


def test_pipeline_still_aborts_when_tolerance_is_not_declared(monkeypatch):
    """Regression guard: the gate must keep failing a step that reports errors
    without opting in, so tolerance is never inferred from the error count."""
    ran = []
    _install(monkeypatch, {'status': 'ok', 'stats': {'errors': 1}}, ran)
    result = P.BeliefEnginePipeline().run(run_id='t2')

    assert result['status'] == 'error'
    assert 'canonicalize_belief_set' not in ran           # the run stopped
    assert result['tolerated_failures'] == []
    failed = [s for s in result['steps'] if s['status'] == 'error']
    assert [s['step'] for s in failed] == ['update_beliefs']
    assert 'reported incomplete processing' in failed[0]['error']


# --- escalating a failure that keeps coming back -----------------------------

def _fail(key, error='boom'):
    return [{'belief_key': key, 'action': 'update', 'error': error}]


def test_one_off_failure_does_not_escalate(tmp_path):
    from belief_engine.pipeline import tolerated_failures as T
    db = tmp_path / 'b.db'
    assert T.record_run(db, _fail('a.one')) == []


def test_same_belief_escalates_at_the_threshold(tmp_path):
    from belief_engine.pipeline import tolerated_failures as T
    db = tmp_path / 'b.db'
    for _ in range(T.THRESHOLD - 1):
        assert T.record_run(db, _fail('a.one')) == []
    persistent = T.record_run(db, _fail('a.one', 'still boom'))
    assert [r['belief_key'] for r in persistent] == ['a.one']
    assert persistent[0]['consecutive_runs'] == T.THRESHOLD
    assert persistent[0]['last_error'] == 'still boom'


def test_a_clean_run_clears_the_streak(tmp_path):
    from belief_engine.pipeline import tolerated_failures as T
    db = tmp_path / 'b.db'
    for _ in range(T.THRESHOLD - 1):
        T.record_run(db, _fail('a.one'))
    T.record_run(db, [])                       # the belief wrote cleanly this run
    assert T.record_run(db, _fail('a.one')) == []   # counting starts over


def test_a_different_belief_does_not_inherit_the_streak(tmp_path):
    from belief_engine.pipeline import tolerated_failures as T
    db = tmp_path / 'b.db'
    for _ in range(T.THRESHOLD - 1):
        T.record_run(db, _fail('a.one'))
    assert T.record_run(db, _fail('b.two')) == []


def test_unnamed_belief_is_not_tracked(tmp_path):
    from belief_engine.pipeline import tolerated_failures as T
    db = tmp_path / 'b.db'
    for _ in range(T.THRESHOLD + 2):
        assert T.record_run(db, _fail('?')) == []
