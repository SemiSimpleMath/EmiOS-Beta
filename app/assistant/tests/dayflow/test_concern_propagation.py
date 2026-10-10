"""Concern back-propagation (2026-08-01 subconscious audit; attached work 2026-09-30; references 2026-10-09).

Work outcomes never reached the concerns register (19 AC-service re-mints, 4 after an explicit user decline). Now
work made for a concern carries it (`concern_refs`), the concern holds a reference to the work, and the work's
judgments and ending reach the brain, which decides (test_concern_outbox.py pins delivery).
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.tests.concern_store_helpers import ScratchRegister

from app.assistant.subconscious.concern_feedback import propagate_work_outcome
from app.assistant.subconscious.persist import attach_work

_CID = "a2a8a4b0-2d34-4c94-9d2e-f5e6f9c6e5d7"


def _register(tmp_path) -> Path:
    path = ScratchRegister(tmp_path)
    path.write({
        "schema_version": 1,
        "last_updated_utc": None,
        "last_noticer_tick_utc": None,
        "active": [{
            "concern_id": _CID,
            "title": "AC service should be handled before summer heat",
            "severity": "high",
            "reinforcement_count": 23,
            "reinforcement_notes": "\n[2026-07-29] seasonal window arrived",
            "last_disposition_at_count": 18,
        }],
        "addressing": [], "resolved": [], "dormant": [],
    })
    return path


class TestAttachWork:

    def test_a_reference_once_per_receipt(self, tmp_path):
        path = _register(tmp_path)
        assert attach_work(f"concern:{_CID[:8]}", work_id="work_x", receipt_id="r1", connect=path.connect) == "attached"
        assert attach_work(_CID, work_id="work_x", receipt_id="r1", connect=path.connect) == "already_applied"
        [concern] = path.read()["active"]
        assert list(concern["attached_work"]) == ["work_x"] and concern["work_receipts"] == ["r1"]

    def test_unknown_ref_is_unresolved(self, tmp_path):
        path = _register(tmp_path)
        assert attach_work("concern:ffffffff", work_id="work_x", connect=path.connect) == "unresolved"
        assert "attached_work" not in path.read()["active"][0]


class TestPropagateWorkOutcome:

    @staticmethod
    def _wo(refs, reply_text=""):
        nodes = {}
        if reply_text:
            nodes["reply_1"] = SimpleNamespace(type="evidence", created_by="reply",
                                               content=reply_text)
        return SimpleNamespace(id="work_x", constraints={"concern_refs": refs}, nodes=nodes)

    def test_work_citing_no_concern_still_leaves_a_receipt_for_the_brain(self, tmp_path):
        # Everything done reports to the brain (2026-10-03); delivery is pinned in test_concern_outbox.py.
        from work_objects.store import WorkStore
        store = WorkStore(str(tmp_path / "work.db"))
        try:
            wo = store.apply("create_work_object", {"title": "Unlinked", "constraints": {}})
            store.apply("set_work_status", {"work_id": wo.id, "status": "done", "reason": "done"})
            assert [r["outcome"] for r in store.pending_concern_feedback()] == ["done"]
        finally:
            store.close()

    def test_failure_never_raises_into_closure(self):
        def _boom(wid):
            raise RuntimeError("store unavailable")
        propagate_work_outcome(SimpleNamespace(load=_boom), "work_x", "done")
