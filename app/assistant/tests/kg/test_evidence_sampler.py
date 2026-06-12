"""Date-stratified evidence sampler (fragility review #6, move 1)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.assistant.kg_core.kg_utils.evidence_sampler import sample_evidence

BASE = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _rows(n, start=BASE, step_days=10):
    return [SimpleNamespace(i=i, ts=start + timedelta(days=i * step_days))
            for i in range(n)]


def _key(r):
    return r.ts


def test_under_cap_returns_all_chronological():
    rows = list(reversed(_rows(5)))
    out = sample_evidence(rows, cap=10, date_key=_key)
    assert [r.i for r in out] == [0, 1, 2, 3, 4]


def test_endpoints_always_included_and_cap_respected():
    rows = _rows(100)
    out = sample_evidence(rows, cap=10, date_key=_key)
    assert len(out) == 10
    assert out[0].i == 0 and out[-1].i == 99  # era endpoints


def test_denser_near_present():
    rows = _rows(100)
    out = sample_evidence(rows, cap=12, date_key=_key)
    newer_half = sum(1 for r in out if r.i >= 50)
    older_half = len(out) - newer_half
    assert newer_half > older_half


def test_covers_whole_span_not_either_end():
    # The failure this exists to prevent: a 3-era node sampled as
    # "oldest 30" or "newest 30" hides an era entirely.
    rows = _rows(90)
    out = sample_evidence(rows, cap=9, date_key=_key)
    thirds = {r.i // 30 for r in out}
    assert thirds == {0, 1, 2}


def test_undated_rows_sort_oldest_and_never_crash():
    rows = _rows(20)
    rows.append(SimpleNamespace(i=99, ts=None))
    out = sample_evidence(rows, cap=5, date_key=_key)
    assert len(out) == 5
    assert out[0].i == 99  # undated counts as oldest
    assert out[-1].i == 19


def test_cap_one_returns_latest():
    rows = _rows(10)
    out = sample_evidence(rows, cap=1, date_key=_key)
    assert [r.i for r in out] == [9]
