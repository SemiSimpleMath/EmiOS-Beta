"""Regression for the empty-pod bug (work-effort pod visibility).

Bug: orchestrator_scope() minted a RANDOM per-dispatch scope_id and NO room_id, so a reader's pod
allowed_scopes ['self'] expanded to ['__none__'] -> every pod a node minted read back as PodNotFound
for the next node (mint stamps pod.scope_id = the minter's room_id, mint_pod.py:76). That drove the
re-research storm in the run-a-business run.

Fix: one stable scope_id+room_id per work_id, shared by every node of the effort.

  PYTHONPATH=. .venv/Scripts/python.exe work_objects/scenarios/pod_scope_repro.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from dotenv import load_dotenv
load_dotenv()

import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.pod_store.pod_utils import resolve_allowed_scopes, pod_in_scope, read_pod_gated, PodNotFound
from app.assistant.pod_store.pod_store import PodStore
from app.assistant.pod_store.contracts import Pod
from work_objects.scope import orchestrator_scope


def main():
    fails = []

    # Three scopes: two nodes of effort W1, one node of a different effort W2.
    a = orchestrator_scope(work_id="W1")   # node A of effort W1 (the minter)
    b = orchestrator_scope(work_id="W1")   # node B of the SAME effort (the reader)
    c = orchestrator_scope(work_id="W2")   # a DIFFERENT effort
    pod_scope = a.room_id                  # mint stamps pod.scope_id = the minter's room_id

    # --- scope-logic level (the layer the bug lived in) ---
    print(f"effort identity stable across nodes: {a.room_id == b.room_id}  ({a.room_id})")
    print(f"reader allowed_scopes (was ['__none__'] before fix): {resolve_allowed_scopes(b)}")
    if not (a.room_id and a.room_id == b.room_id):
        fails.append("nodes of one effort do not share a stable identity")
    if not pod_in_scope(pod_scope, resolve_allowed_scopes(b)):
        fails.append("sibling node cannot read same-effort pod (THE BUG)")
    if pod_in_scope(pod_scope, resolve_allowed_scopes(c)):
        fails.append("cross-effort isolation broken")

    # --- end-to-end through the real read gate (read_pod_gated == what pod_fetch dereferences) ---
    pid = "datapod:research_finding:podscoperepro01"
    PodStore().put(Pod(pod_id=pid, kind="research_finding", one_liner="repro pod",
                       body="five vetted Sarasota partners ...", scope_id=pod_scope))
    try:
        got = read_pod_gated(pid, b)       # node B reads node A's pod (same effort) -> must succeed
        print(f"end-to-end: node B read node A's pod OK (scope_id={got.get('scope_id')})")
    except PodNotFound:
        fails.append("read_pod_gated: same-effort read still PodNotFound")
    try:
        read_pod_gated(pid, c)             # different effort -> must be denied
        fails.append("read_pod_gated: cross-effort read leaked")
    except PodNotFound:
        print("end-to-end: cross-effort read correctly denied")

    print("\n" + ("EMPTY-POD BUG FIXED — effort pods mutually visible, efforts isolated"
                  if not fails else f"FAILURES: {fails}"))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
