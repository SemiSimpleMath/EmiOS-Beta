"""Pages must be USABLE, not merely 200 (2026-09-11).

On 2026-09-11 every settings page returned 200 while being unusable: the
browser's data fetches were answered by a different service, so tabs rendered
empty and saves reported failure. A status-code-only check saw nothing wrong.
Three properties that would have caught it, and that a status check cannot:

  1. an HTML page really is HTML (not another app's page)
  2. an API route returns JSON -- content-type, not just 200
  3. a save round-trips: GET a document, PUT it back unchanged, get success

Also pins the two files whose ABSENCE silently broke a whole feature, because
the image shipped them but the seeder never copied them to the data volume:
resource_subscriptions.json (killed every chat turn with a FileNotFoundError)
and the belief export's output dir.

Opt-in: set EMIOS_SMOKE_URL to a running instance, e.g.
    EMIOS_SMOKE_URL=http://127.0.0.1:9300 pytest -k live_surface
Skips entirely when unset or unreachable, so CI without a deployment is green.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

import pytest

BASE = os.environ.get("EMIOS_SMOKE_URL", "").rstrip("/")
pytestmark = pytest.mark.skipif(not BASE, reason="EMIOS_SMOKE_URL not set")

_TIMEOUT = 20


def _get(path: str):
    req = urllib.request.Request(BASE + path, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read()
    except OSError as e:
        pytest.skip(f"{BASE} unreachable: {e}")


HTML_PAGES = [
    "/chat_bot", "/settings/assistant", "/settings/api-keys",
    "/settings/integrations", "/settings/features", "/settings/voice",
    "/routines", "/meals", "/personalize",
]

JSON_APIS = [
    "/api/routines", "/api/assistant-core", "/api/settings/features",
    "/api/settings/preferences", "/api/voice/settings",
]


@pytest.mark.parametrize("path", HTML_PAGES)
def test_page_renders_emios_html(path):
    status, ctype, body = _get(path)
    assert status == 200, f"{path} -> {status}"
    assert "text/html" in ctype.lower(), f"{path} content-type {ctype!r}"
    # A different app answering (the failure mode we actually hit) would not
    # carry EmiOS's own markup.
    assert b"<html" in body.lower(), f"{path} returned no HTML document"


@pytest.mark.parametrize("path", JSON_APIS)
def test_api_returns_json_not_someone_elses_html(path):
    status, ctype, body = _get(path)
    assert status == 200, f"{path} -> {status}"
    assert "application/json" in ctype.lower(), (
        f"{path} returned content-type {ctype!r}. This is the 2026-09-11 bug: "
        "the request reached a different service and came back as HTML."
    )
    json.loads(body)  # must parse


def test_assistant_core_round_trips():
    """GET then PUT unchanged must succeed — the 'Failed to save' path."""
    status, ctype, body = _get("/api/assistant-core")
    assert status == 200 and "json" in ctype.lower()
    doc = json.loads(body)
    req = urllib.request.Request(
        BASE + "/api/assistant-core", method="PUT",
        data=json.dumps(doc).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            assert r.status == 200
            assert json.loads(r.read()).get("success") is True
    except urllib.error.HTTPError as e:
        pytest.fail(f"PUT /api/assistant-core -> {e.code}: {e.read()[:200]!r}")


def test_required_seeded_files_exist_on_the_data_volume():
    """Files the image ships that the app REQUIRES must reach the data dir.

    resource_subscriptions.json being absent made every chat turn raise
    FileNotFoundError while the UI only said "I ran into an error".
    """
    from app.assistant.utils.path_utils import get_resources_dir
    required = [get_resources_dir() / "context" / "global" / "resource_subscriptions.json"]
    missing = [str(p) for p in required if not p.exists()]
    assert not missing, "required seeded file(s) missing from the data dir: " + ", ".join(missing)
