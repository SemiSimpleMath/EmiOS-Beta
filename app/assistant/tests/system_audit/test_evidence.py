"""Evidence assembler: dossier from a seeded case — chat window, work graph,
id harvesting, log excerpt with declared windows."""
from __future__ import annotations

import app.assistant.tests.test_setup  # noqa: F401

from datetime import timedelta

import pytest

from app.assistant.system_audit import case_store as cs
from app.assistant.system_audit import evidence
from app.assistant.utils.time_utils import utc_now, utc_to_local


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    from app.models.base import Base, get_current_engine, get_session
    from app.assistant.database.system_audit_case import SystemAuditCase
    Base.metadata.create_all(get_current_engine(), checkfirst=True)
    s = get_session()
    try:
        s.query(SystemAuditCase).delete()
        s.commit()
    finally:
        s.close()
    monkeypatch.setattr(evidence, "INBOX_DIR", tmp_path / "inbox")
    yield


def _seed_chat(room, when, msg_id="ulx1", text="why am I being asked this again?"):
    from app.assistant.database.db_handler import UnifiedLog2026
    from app.models.base import get_session
    s = get_session()
    try:
        s.merge(UnifiedLog2026(id=msg_id, timestamp=when, role="user", message=text,
                               source="chat", room_id=room, speaker_name="User",
                               speaker_role="user"))
        s.commit()
    finally:
        s.close()


def _seed_work():
    from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
    store = get_dayflow_work_store()
    wo = store.apply("create_work_object", {"title": "Audit fixture WO",
                                            "goal_content": "fixture goal"})
    return wo.id


def test_assemble_builds_dossier_and_harvests_ids(tmp_path):
    now = utc_now()
    room = "master_room"
    _seed_chat(room, now - timedelta(minutes=2), msg_id="ul_evidence_1")
    wid = _seed_work()

    cid = cs.open_case(trigger_kind="user_friction", room_id=room,
                       bound_ids={"message_ids": ["ul_evidence_1"], "work_ids": [wid]},
                       summary="fixture friction", anchor_at=now,
                       quote={"quote": "why again?", "message_id": "ul_evidence_1",
                              "kind": "repeat_ask", "at": now.isoformat()})

    logf = tmp_path / "emi_test.log"
    stamp = utc_to_local(now).strftime("%Y-%m-%d %H:%M")
    logf.write_text(
        f"{stamp}:01,000 - app.x - ERROR - N/A - dispatch exploded near the anchor\n"
        f"2020-01-01 00:00:01,000 - app.y - ERROR - N/A - ancient line outside window\n",
        encoding="utf-8")

    path = evidence.assemble(cid, log_path=str(logf))
    text = open(path, encoding="utf-8").read()

    assert f"case_id: {cid}" in text and "status: assembled" in text
    assert "why again?" in text                       # friction verbatim
    assert "ul_evide" in text                         # chat window row id prefix
    assert "Audit fixture WO" in text                 # work graph rendered
    assert "dispatch exploded near the anchor" in text  # in-window log line
    assert "ancient line outside window" not in text    # out-of-window excluded
    assert "±30 min" in text and "±5 min" in text       # declared windows

    row = [c for c in cs.list_cases() if c["id"] == cid][0]
    assert row["status"] == "assembled" and row["dossier_path"] == path


def test_assemble_requires_open_case():
    with pytest.raises(KeyError):
        evidence.assemble("sac_nope")
