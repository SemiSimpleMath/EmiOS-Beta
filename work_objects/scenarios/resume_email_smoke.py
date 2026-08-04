"""RESUME-FROM-GRAPH demo: a WorkObject is durable, self-describing state — pause it at any
point, feed the partial graph back to work_emi_team, and it figures out what's left.

We take the COMPLETED leonardo WorkObject from work.db, PRUNE the email step's `done` status
(back to not-done), reopen the goal (recovering its ORIGINAL task from the event log — the
events are the source of truth, the projection had overwritten the goal content with the
answer), then hand the goal node to work_emi_team_manager and watch it:
  - re-send the email (its planner sees email=not-done in the work_projection), and
  - NOT re-run the research (still marked done in the graph).

This is the foundational long-horizon capability (resume-from-graph). Park/wake is just a
timed trigger that re-feeds the graph later; this proves the part underneath it.

  PYTHONPATH=. .venv/Scripts/python.exe work_objects/scenarios/resume_email_smoke.py
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from dotenv import load_dotenv
load_dotenv()

import app.assistant.tests.test_setup  # noqa: F401 - bootstrap DI
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import add_file_sink
from app.assistant.utils.pydantic_classes import ToolResult
from work_objects import discharge, scope as work_scope
from work_objects.store import WorkStore

CAPTURED = []


def _stub_send_email():
    cfg = DI.tool_registry.get_tool("send_email")
    cls = cfg.get("tool_class") if isinstance(cfg, dict) else None
    if cls is None:
        print("WARN: send_email tool not found — cannot stub", flush=True)
        return
    def _stub(self, tool_message):
        args = (tool_message.tool_data or {}).get("arguments", {}) or {}
        CAPTURED.append({k: args.get(k) for k in ("to", "subject", "body")})
        return ToolResult(result_type="send_email",
                          content=f"Email successfully sent to {args.get('to')}",
                          data={"message_id": "stubbed-resume"})
    cls.execute = _stub
    cls.requires_approval = False


def ck(label, ok):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}", flush=True)
    assert ok, label


def main():
    log = add_file_sink("resume_email_smoke")
    print(f"full logs -> {log}\n", flush=True)
    src = "work_objects/work.db"
    if not os.path.exists(src):
        print("no work.db — first run: "
              "WORK_DB=work_objects/work.db python work_objects/scenarios/node_handoff_smoke.py", flush=True)
        return

    # Operate on a COPY so the seeded UI data stays intact + the demo is reproducible.
    db = os.path.join(tempfile.mkdtemp(prefix="resume_"), "w.db")
    shutil.copy(src, db)
    for ext in ("-wal", "-shm"):
        if os.path.exists(src + ext):
            shutil.copy(src + ext, db + ext)

    discharge._ensure_registered()
    DI.manager_registry.preload_all()
    _stub_send_email()

    # ---- PRUNE (fixture surgery): done->active is a guarded spine transition + there is no
    # 'reopen' op yet, so we write the projection directly to craft the partial graph. ----
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    wid = con.execute("SELECT id FROM work_objects ORDER BY updated_at DESC LIMIT 1").fetchone()["id"]
    goal = con.execute("SELECT goal_node_id FROM work_objects WHERE id=?", (wid,)).fetchone()["goal_node_id"]
    rows = con.execute(
        "SELECT id, COALESCE(title,'')||' '||COALESCE(content,'') AS t, status FROM nodes "
        "WHERE work_id=? AND parent_id=? AND type='subtask'", (wid, goal)).fetchall()
    email = next((r for r in rows if "email" in r["t"].lower() or "jukka" in r["t"].lower()), None)
    if email is None:
        print("could not find an email subtask under the goal; nodes:", flush=True)
        for r in rows:
            print(f"  {r['id']} [{r['status']}] {r['t'][:60]}", flush=True)
        return
    # Recover the ORIGINAL goal task from the create_work_object event (source of truth).
    ev = con.execute("SELECT data FROM events WHERE work_id=? AND op='create_work_object' ORDER BY seq LIMIT 1",
                     (wid,)).fetchone()
    orig_task = (json.loads(ev["data"]) or {}).get("goal_content", "") if ev else ""

    print(f"BEFORE  goal_status=done  email={email['status']!r} :: {email['t'][:60]}", flush=True)
    # Reverting a step means reverting its OUTPUTS, not just its status flag: the planner reasons
    # from recorded EVIDENCE, so the email node's result content + the "email was sent" evidence
    # finding must go too — else it reads them and (correctly) concludes the email is already done.
    con.execute("UPDATE nodes SET status='proposed', content='' WHERE id=?", (email["id"],))
    con.execute("UPDATE nodes SET status='active', content=? WHERE id=?", (orig_task or None, goal))
    con.execute("UPDATE work_objects SET status='active' WHERE id=?", (wid,))
    pruned_ev = 0
    for r in con.execute("SELECT id, COALESCE(content,'') AS c FROM nodes WHERE work_id=? AND type='evidence'",
                         (wid,)).fetchall():
        cl = r["c"].lower()
        if "email" in cl and ("sent" in cl or "jukka" in cl):
            con.execute("DELETE FROM nodes WHERE id=?", (r["id"],))
            pruned_ev += 1
    con.commit()
    con.close()
    print(f"PRUNED  email -> proposed (content cleared), {pruned_ev} 'email-sent' evidence removed, "
          f"goal -> active, task restored: {orig_task[:70]!r}\n", flush=True)

    store = WorkStore(db)
    before = store.load(wid)
    web_before = sum(1 for n in before.nodes.values() if (n.owner_agent or "").lower() == "work_web_manager")

    print("=== RESUME: hand the partial goal to work_emi_team_manager ===\n", flush=True)
    discharge.discharge_node(store, wid, goal, manager_name="work_emi_team_manager", scope_context=work_scope.orchestrator_scope(work_id=wid))

    after = store.load(wid)
    email_after = after.nodes[email["id"]].status
    web_after = sum(1 for n in after.nodes.values() if (n.owner_agent or "").lower() == "work_web_manager")

    print("\n=== RESULT ===", flush=True)
    print(f"emails re-sent: {len(CAPTURED)}", flush=True)
    for c in CAPTURED:
        print(f"  to={c['to']} subj={c['subject']!r}\n  body={(c['body'] or '')[:180]!r}", flush=True)
    print(f"email node: proposed -> {email_after}", flush=True)
    print(f"web-research handoffs: before={web_before} after={web_after} "
          f"({'NO new research' if web_after == web_before else 'RE-RAN research!'})", flush=True)

    print("\n=== checks ===", flush=True)
    ck("the email was RE-SENT on resume", len(CAPTURED) >= 1)
    ck("the re-sent email carries the graph's researched answer (Ceretti)",
       any("ceretti" in (c.get("body") or "").lower() for c in CAPTURED))
    ck("research was NOT re-run (no new web handoff)", web_after == web_before)
    ck("the email node closed again (proposed -> done)", email_after == "done")
    store.close()
    print("\nRESUME OK: a partial WorkObject fed back to work_emi_team finished the remaining step "
          "(email) reusing the graph's prior research — no scheduler, the graph IS the resume point.", flush=True)


if __name__ == "__main__":
    main()
