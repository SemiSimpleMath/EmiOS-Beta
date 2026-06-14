"""mint_pod persists content as a pod and returns its pod_id.

This is the 'make a datapod' capability the agent layer was missing — the
phantom-mint diagnosis (2026-06-13): a worker emitted a datapod *spec* and
deferred persistence to an upstream system that didn't exist, so nothing was
saved. mint_pod is that upstream system.

USE_TEST_DB is set at module top (before any project import) so all writes hit
test_emidb, never the live emi.db.
"""
import os

os.environ["USE_TEST_DB"] = "true"  # MUST precede project imports

import app.assistant.tests.test_setup  # noqa: F401,E402  bootstraps DI

from app.assistant.lib.tools.mint_pod.mint_pod import MintPodTool  # noqa: E402
from app.assistant.pod_store.pod_store import PodStore  # noqa: E402
from app.assistant.utils.pydantic_classes import ToolMessage  # noqa: E402


def _mint(args):
    msg = ToolMessage(tool_name="mint_pod", tool_data={"arguments": args})
    return MintPodTool().execute(msg)


def test_mint_pod_persists_and_returns_id():
    res = _mint({
        "title": "Friends & Family Emails",
        "body": "Alice: alice@example.com\nBob: bob@example.com",
        "tags": ["contacts"],
        "importance": 7,
    })
    assert res.data.get("ok") is True
    pod_id = res.data["pod_id"]
    assert pod_id.startswith("datapod:note:")

    # It's actually in the store, verbatim — not just narrated.
    pod = PodStore().get(pod_id)
    assert pod is not None
    assert pod.kind == "note"
    assert pod.one_liner == "Friends & Family Emails"
    assert pod.body == "Alice: alice@example.com\nBob: bob@example.com"
    assert "contacts" in pod.tags
    assert pod.importance == 7.0


def test_mint_pod_requires_title_and_body():
    assert _mint({"title": "", "body": "x"}).result_type == "error"
    assert _mint({"title": "x", "body": ""}).result_type == "error"


def test_mint_pod_rejects_non_note_kind():
    # An LLM tool must never mint identity/auth/secret kinds.
    res = _mint({"title": "x", "body": "y", "kind": "identity.ssn"})
    assert res.result_type == "error"


def test_mint_pod_clamps_importance():
    res = _mint({"title": "t", "body": "b", "importance": 99})
    assert res.data["importance"] == 10.0
