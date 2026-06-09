"""Tests for the universal pod read gate (pod_utils.read_pod_gated), the canonical id builder,
and the deterministic /pod expand command resolution."""
from __future__ import annotations

import pytest

from app.assistant.pod_store import pod_utils
from app.assistant.pod_store.authority import PodAuthorityError
from app.assistant.pod_store.contracts import Pod
from app.assistant.pod_store.pod_uri import POD_URI_RE
from app.assistant.utils.pydantic_classes import ScopeContext, ScopeApprovalPolicy, ScopePodPolicy
from app.assistant.room_session_manager.services import pod_command


def _scope(authority, allowed, room_id="slack:C1"):
    return ScopeContext(
        scope_id="s", owner_id="user", actor_id="t", surface="ui", room_id=room_id,
        approval=ScopeApprovalPolicy(authority_level=authority),
        pods=ScopePodPolicy(allowed_scopes=allowed),
    )


def _pod(scope_id="slack:C1", min_authority=None):
    return Pod(
        pod_id="datapod:research_finding:aaaaaa", kind="research_finding",
        one_liner="Bob finding", body="Full Bob body.", scope_id=scope_id,
        metadata={"source_urls": ["https://x.com"]}, min_authority=min_authority,
    )


# ── canonical id ──────────────────────────────────────────────────
def test_canonical_id_is_valid_and_deterministic():
    a = pod_utils.canonical_pod_id("research_finding", "run1", "unitA")
    b = pod_utils.canonical_pod_id("research_finding", "run1", "unitA")
    assert a == b
    assert POD_URI_RE.fullmatch(a)
    assert a != pod_utils.canonical_pod_id("research_finding", "run1", "unitB")


# ── read_pod_gated: scope wall then authority wall ────────────────
class TestReadGate:
    def _patch_get(self, monkeypatch, pod):
        monkeypatch.setattr(pod_utils.PodStore, "get", lambda self, pid: pod)

    def test_in_scope_and_authority_ok(self, monkeypatch):
        self._patch_get(monkeypatch, _pod())  # scope slack:C1, min None -> 50
        out = pod_utils.read_pod_gated("datapod:research_finding:aaaaaa", _scope(50, ["self"]))
        assert out["body"] == "Full Bob body."
        assert out["source_urls"] == ["https://x.com"]

    def test_authority_below_floor_raises(self, monkeypatch):
        self._patch_get(monkeypatch, _pod())
        with pytest.raises(PodAuthorityError):
            pod_utils.read_pod_gated("datapod:research_finding:aaaaaa", _scope(30, ["self"]))

    def test_out_of_scope_is_not_found(self, monkeypatch):
        self._patch_get(monkeypatch, _pod(scope_id="slack:OTHER"))
        with pytest.raises(pod_utils.PodNotFound):
            pod_utils.read_pod_gated("datapod:research_finding:aaaaaa", _scope(99, ["self"], room_id="slack:C1"))

    def test_missing_is_not_found(self, monkeypatch):
        monkeypatch.setattr(pod_utils.PodStore, "get", lambda self, pid: None)
        with pytest.raises(pod_utils.PodNotFound):
            pod_utils.read_pod_gated("datapod:research_finding:zzzzzz", _scope(99, ["all"]))

    def test_all_scope_reads_cross_scope(self, monkeypatch):
        self._patch_get(monkeypatch, _pod(scope_id="slack:OTHER"))
        out = pod_utils.read_pod_gated("datapod:research_finding:aaaaaa", _scope(99, ["all"]))
        assert out["one_liner"] == "Bob finding"

    def test_per_pod_override_gates_higher(self, monkeypatch):
        self._patch_get(monkeypatch, _pod(min_authority=70))  # override above default 50
        with pytest.raises(PodAuthorityError):
            pod_utils.read_pod_gated("datapod:research_finding:aaaaaa", _scope(50, ["self"]))


# ── /pod expand resolution ────────────────────────────────────────
class TestPodExpand:
    def test_help_on_bare(self):
        r = pod_command.handle_pod_command(cmd_payload="", room_id="r", surface="ui", context_id="m")
        assert "Usage" in r.early_result["reply_text"]
        assert r.continue_pipeline is False

    def test_one_match_posts_body(self, monkeypatch):
        monkeypatch.setattr(pod_command, "_recent_pod_ids", lambda *a, **k: ["datapod:research_finding:d24abc"])
        monkeypatch.setattr(pod_command, "_build_room_scope", lambda room_id, surface: _scope(99, ["all"]))
        monkeypatch.setattr(pod_command.pod_utils, "read_pod_gated",
                            lambda pid, scope: {"one_liner": "Bob", "body": "Body!", "source_urls": ["https://s"]})
        r = pod_command.handle_pod_command(cmd_payload="expand d24", room_id="r", surface="ui", context_id="m")
        txt = r.early_result["reply_text"]
        assert "Body!" in txt and "Bob" in txt and "https://s" in txt

    def test_many_disambiguates(self, monkeypatch):
        monkeypatch.setattr(pod_command, "_recent_pod_ids", lambda *a, **k:
                            ["datapod:research_finding:d24aaa", "datapod:research_finding:d24bbb"])
        monkeypatch.setattr(pod_command, "_one_liner", lambda pid: "")
        r = pod_command.handle_pod_command(cmd_payload="expand d24", room_id="r", surface="ui", context_id="m")
        assert "several findings" in r.early_result["reply_text"]

    def test_none_matches(self, monkeypatch):
        monkeypatch.setattr(pod_command, "_recent_pod_ids", lambda *a, **k: ["datapod:research_finding:abc123"])
        r = pod_command.handle_pod_command(cmd_payload="expand zzz", room_id="r", surface="ui", context_id="m")
        assert "No recent pod matches" in r.early_result["reply_text"]

    def test_authority_denied(self, monkeypatch):
        monkeypatch.setattr(pod_command, "_recent_pod_ids", lambda *a, **k: ["datapod:research_finding:d24abc"])
        monkeypatch.setattr(pod_command, "_build_room_scope", lambda room_id, surface: _scope(30, ["self"]))

        def _deny(pid, scope):
            raise PodAuthorityError(pod_id=pid, projection=None, required=50, actual=30)

        monkeypatch.setattr(pod_command.pod_utils, "read_pod_gated", _deny)
        r = pod_command.handle_pod_command(cmd_payload="expand d24", room_id="r", surface="ui", context_id="m")
        assert "don't have access" in r.early_result["reply_text"]
