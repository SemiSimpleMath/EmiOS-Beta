"""Tests for /api/pods/<id> — the chat pod-viewer content route.

Covers the fail-closed displayable-kind guard (only research_finding is served;
image / secret / unknown pods 404) and route disambiguation against the
existing /api/pods/<id>/image route.
"""
from __future__ import annotations

import pytest
from flask import Flask

import app.routes.pod_api as pod_api
from app.assistant.pod_store.contracts import Pod


def _pod(pod_id: str, kind: str) -> Pod:
    return Pod(
        pod_id=pod_id,
        kind=kind,
        tags=["irvine", "hvac"],
        one_liner="Acme Air Plumbing — IAQ, (555) 010-1234",
        body="Full writeup: licensed, EPA-certified, books IAQ assessments.",
        source_refs=[],
        for_agents=[],
        scope_id=None,
        created_by="web::planner",
        metadata={"unit": "j martin", "run": "irvine_hvac", "source_urls": ["https://example.com/jmartin"]},
    )


_RESEARCH_ID = "datapod:research_finding:3519561be7b1"
_IMAGE_ID = "datapod:image:abcdef123456"
_SECRET_ID = "datapod:secret:0011aabbccdd"


class _FakeStore:
    """Returns canned pods keyed by id; None for anything unknown."""
    _pods = {
        _RESEARCH_ID: _pod(_RESEARCH_ID, "research_finding"),
        _IMAGE_ID: _pod(_IMAGE_ID, "image"),
        _SECRET_ID: _pod(_SECRET_ID, "secret"),
    }

    def get(self, pod_id):
        return self._pods.get(pod_id)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(pod_api, "PodStore", _FakeStore)
    app = Flask(__name__)
    app.register_blueprint(pod_api.pod_api_bp)
    return app.test_client()


def test_research_pod_served(client):
    resp = client.get(f"/api/pods/{_RESEARCH_ID}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["pod_id"] == _RESEARCH_ID
    assert data["kind"] == "research_finding"
    assert "Acme Air" in data["one_liner"]
    assert "Full writeup" in data["body"]
    assert data["source_urls"] == ["https://example.com/jmartin"]


def test_secret_pod_404(client):
    # Secret pods are NOT in DISPLAYABLE_POD_KINDS — must never be served.
    assert client.get(f"/api/pods/{_SECRET_ID}").status_code == 404


def test_image_pod_not_served_as_content(client):
    # Image pods have their own /image route; the content route 404s them.
    assert client.get(f"/api/pods/{_IMAGE_ID}").status_code == 404


def test_malformed_pod_uri_404(client):
    assert client.get("/api/pods/not-a-pod-uri").status_code == 404


def test_route_disambiguation_against_image(client):
    # The content route must not swallow the /image suffix route.
    adapter = client.application.url_map.bind("localhost")
    ep_content, _ = adapter.match(f"/api/pods/{_RESEARCH_ID}")
    ep_image, _ = adapter.match(f"/api/pods/{_IMAGE_ID}/image")
    assert ep_content == "pod_api.get_pod_contents"
    assert ep_image == "pod_api.get_pod_image"
