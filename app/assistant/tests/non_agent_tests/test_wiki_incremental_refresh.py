"""Incremental wiki refresh (2026-07-25).

Guards the refresh-when-a-page-already-exists economics:
  - the bullet-index sidecar stores {key: text} (removed facts can be named
    in revision prompts); legacy key-only lists still load for diffing;
  - a dirty section with a small delta is REVISED from its cached prose +
    only the added/removed bullet texts, not rewritten from the full slice;
  - window excerpts come from the added bullets' own source windows;
  - build_one_page (the refresh_wiki_page tool path) routes through the
    incremental machinery when a baseline exists, and reports "unchanged"
    instead of paying for a full-page rewrite.

LLM agents are stubbed; this exercises the deterministic plumbing.
"""
from __future__ import annotations

import os

os.environ.setdefault("USE_TEST_DB", "true")
os.environ.setdefault("TEST_DB_NAME", "test_wiki_incremental_refresh")

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional

import pytest

import app.assistant.tests.test_setup  # noqa: F401

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.kg_projection import (
    Bullet,
    load_bullet_index,
    save_bullet_index,
    save_tags,
)
from app.assistant.kg_projection.sections import SectionSpec
from app.assistant.wiki_generator import growth, page_writer
from app.assistant.wiki_generator.page_writer import (
    SECTION_REVISION_MAX_DELTA_RATIO,
    choose_section_refresh_mode,
    regenerate_affected_sections,
)


# ----------------------------------------------------------------------
# Bullet-index sidecar format
# ----------------------------------------------------------------------


def test_bullet_index_round_trip_mapping(tmp_path):
    save_bullet_index(tmp_path, "Alice", {"k1": "- fact one", "k2": "- fact two"})
    assert load_bullet_index(tmp_path, "Alice") == {
        "k1": "- fact one", "k2": "- fact two",
    }


def test_bullet_index_legacy_list_loads_as_none_texts(tmp_path):
    legacy = tmp_path / "bullet_index"
    legacy.mkdir()
    (legacy / "Alice.json").write_text(json.dumps(["k1", "k2"]), encoding="utf-8")
    assert load_bullet_index(tmp_path, "Alice") == {"k1": None, "k2": None}


def test_bullet_index_missing_or_corrupt_is_empty(tmp_path):
    assert load_bullet_index(tmp_path, "Alice") == {}
    bad = tmp_path / "bullet_index"
    bad.mkdir()
    (bad / "Alice.json").write_text("{not json", encoding="utf-8")
    assert load_bullet_index(tmp_path, "Alice") == {}


# ----------------------------------------------------------------------
# Revise-vs-rewrite decision
# ----------------------------------------------------------------------


def test_mode_small_delta_revises():
    assert choose_section_refresh_mode(
        cached_prose="## Family\n\nprose", slice_size=11,
        added_count=1, removed_texts=[],
    ) == "revise"


def test_mode_removal_with_known_text_revises():
    assert choose_section_refresh_mode(
        cached_prose="## Family\n\nprose", slice_size=10,
        added_count=0, removed_texts=["- gone fact"],
    ) == "revise"


def test_mode_no_cached_prose_rewrites():
    assert choose_section_refresh_mode(
        cached_prose="  ", slice_size=10, added_count=1, removed_texts=[],
    ) == "rewrite"


def test_mode_unknown_removed_text_rewrites():
    # Legacy key-only sidecar → removed text is None → nothing to tell the
    # reviser to delete.
    assert choose_section_refresh_mode(
        cached_prose="prose", slice_size=10, added_count=0, removed_texts=[None],
    ) == "rewrite"


def test_mode_large_delta_rewrites():
    # slice=5, delta=3 > 5 * 0.3 → re-ground in the full slice.
    assert SECTION_REVISION_MAX_DELTA_RATIO == pytest.approx(0.3)
    assert choose_section_refresh_mode(
        cached_prose="prose", slice_size=5, added_count=3, removed_texts=[],
    ) == "rewrite"


# ----------------------------------------------------------------------
# Shared fixtures for the section-refresh harness
# ----------------------------------------------------------------------

ENTITY = "Alice"
ROUGH = "---\nname: Alice\nkg_node_id: node-1\n---\n\n# Alice\n"


def _bullet(text: str, windows: Optional[List[str]] = None) -> Bullet:
    return Bullet(text=text, kind="relationship", source_window_ids=windows or [])


def _fake_neighborhood():
    return SimpleNamespace(
        entity=SimpleNamespace(id="node-1", label=ENTITY, node_type="Entity"),
    )


class _StubAgent:
    """Records every agent_input it sees; replies from a canned queue."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: List[dict] = []

    def action_handler(self, msg):
        self.calls.append(dict(msg.agent_input or {}))
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return SimpleNamespace(data=reply)


@pytest.fixture()
def harness(tmp_path, monkeypatch):
    """Vault + stubbed agents/side-effects around regenerate_affected_sections.

    Returns a dict the test mutates before calling ``run()``.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / f"{ENTITY}.md").write_text(ROUGH, encoding="utf-8")

    tagger = _StubAgent([
        {"results": [{"number": i, "sections": ["family"]} for i in range(1, 21)]},
    ])
    critic = _StubAgent([{"include": True}])
    writer = _StubAgent([{"page_markdown": "## Family\n\nREVISED PROSE. {node:n2}"}])

    def _create_agent(name):
        return {
            "wiki_section_tagger": tagger,
            "wiki_inclusion_critic": critic,
            "wiki_writer": writer,
        }.get(name)

    monkeypatch.setattr(DI.agent_factory, "create_agent", _create_agent)

    monkeypatch.setattr(
        page_writer, "_load_wiki_sections_resource",
        lambda: [SectionSpec(key="family", title="Family", description="family")],
    )

    excerpt_calls: List[List[str]] = []

    def _fake_excerpts(window_ids, max_per_msg=200):
        excerpt_calls.append(list(window_ids))
        return "EXCERPTS" if window_ids else ""

    monkeypatch.setattr(page_writer, "build_window_excerpts", _fake_excerpts)
    monkeypatch.setattr(page_writer, "apply_references", lambda md: md)
    monkeypatch.setattr(page_writer, "_sync_lead_to_node_description", lambda *a, **k: None)
    import app.assistant.wiki_generator.lead_writer as lead_writer
    import app.assistant.wiki_generator.profile_image as profile_image
    monkeypatch.setattr(lead_writer, "generate_lead", lambda **k: "")
    monkeypatch.setattr(profile_image, "materialize_profile_image_for_vault", lambda *a, **k: None)

    h = {
        "vault": vault,
        "tagger": tagger,
        "critic": critic,
        "writer": writer,
        "excerpt_calls": excerpt_calls,
        "bullets": [],  # current KG bullets, set per test
    }
    monkeypatch.setattr(page_writer, "render_bullets", lambda neighborhood: list(h["bullets"]))

    def run():
        return regenerate_affected_sections(
            entity_label=ENTITY,
            vault_path=vault,
            changed_node_ids=["changed-node"],
            neighborhood=_fake_neighborhood(),
        )

    h["run"] = run
    return h


def _seed_baseline(vault: Path, old_bullets: List[Bullet], section_prose: str) -> None:
    """Cached prose + tag sidecar + (new-format) bullet index for old bullets."""
    out_dir = vault / "section_outputs"
    out_dir.mkdir(exist_ok=True)
    (out_dir / f"{ENTITY}.json").write_text(
        json.dumps({"family": section_prose}), encoding="utf-8",
    )
    save_tags(vault, ENTITY, {b.key: ["family"] for b in old_bullets})
    save_bullet_index(vault, ENTITY, {b.key: b.text for b in old_bullets})


# ----------------------------------------------------------------------
# Section-refresh behavior
# ----------------------------------------------------------------------

CACHED_PROSE = "## Family\n\nAlice is married to Bob. {node:n1}"


def test_small_addition_revises_cached_prose_with_targeted_excerpts(harness):
    old = [_bullet(f"- old fact {i}") for i in range(10)]
    new = _bullet("- Alice adopted a dog (2026-07-20)", windows=["w-dog"])
    _seed_baseline(harness["vault"], old, CACHED_PROSE)
    harness["bullets"] = old + [new]

    result = harness["run"]()

    assert result is not None
    assert len(harness["writer"].calls) == 1
    call = harness["writer"].calls[0]
    # Revision inputs, not the full slice:
    assert call["current_section_text"] == CACHED_PROSE
    assert call["added_facts"] == new.text
    assert call["removed_facts"] == ""
    assert call["rough_page"] == ""
    # Excerpts grounded in the ADDED bullet's window, not an entity-wide sample.
    assert harness["excerpt_calls"] == [["w-dog"]]
    # Section output replaced by the revision.
    outputs = json.loads(
        (harness["vault"] / "section_outputs" / f"{ENTITY}.json").read_text(encoding="utf-8")
    )
    assert outputs["family"].startswith("## Family\n\nREVISED PROSE.")
    # Index checkpoint now maps every current bullet key → text.
    idx = load_bullet_index(harness["vault"], ENTITY)
    assert idx == {b.key: b.text for b in harness["bullets"]}


def test_removal_with_known_text_revises_and_names_the_fact(harness):
    old = [_bullet(f"- old fact {i}") for i in range(10)]
    removed = old[3]
    _seed_baseline(harness["vault"], old, CACHED_PROSE)
    harness["bullets"] = [b for b in old if b.key != removed.key]

    result = harness["run"]()

    assert result is not None
    call = harness["writer"].calls[0]
    assert call["current_section_text"] == CACHED_PROSE
    assert call["removed_facts"] == removed.text
    assert call["added_facts"] == ""
    assert call["rough_page"] == ""
    # Removal-dirty sections bypass the inclusion critic.
    assert harness["critic"].calls == []


def test_legacy_index_removal_falls_back_to_full_slice_rewrite(harness):
    old = [_bullet(f"- old fact {i}") for i in range(10)]
    removed = old[3]
    _seed_baseline(harness["vault"], old, CACHED_PROSE)
    # Overwrite the index with the legacy key-only list → removed text unknown.
    idx_path = harness["vault"] / "bullet_index" / f"{ENTITY}.json"
    idx_path.write_text(json.dumps([b.key for b in old]), encoding="utf-8")
    harness["bullets"] = [b for b in old if b.key != removed.key]

    harness["run"]()

    call = harness["writer"].calls[0]
    assert call["current_section_text"] == ""
    assert call["rough_page"].startswith("## Family")
    for b in harness["bullets"]:
        assert b.text in call["rough_page"]


def test_large_delta_rewrites_from_full_slice(harness):
    old = [_bullet(f"- old fact {i}") for i in range(2)]
    new = [_bullet(f"- new fact {i}", windows=[f"w{i}"]) for i in range(3)]
    _seed_baseline(harness["vault"], old, CACHED_PROSE)
    harness["bullets"] = old + new

    harness["run"]()

    call = harness["writer"].calls[0]
    assert call["current_section_text"] == ""
    assert call["rough_page"].startswith("## Family")
    # Even in rewrite mode, excerpts target the added bullets' windows.
    assert harness["excerpt_calls"] == [["w0", "w1", "w2"]]


def test_no_changes_needed_sentinel_keeps_cached_prose(harness):
    old = [_bullet(f"- old fact {i}") for i in range(10)]
    new = _bullet("- minor new fact")
    _seed_baseline(harness["vault"], old, CACHED_PROSE)
    harness["bullets"] = old + [new]
    harness["writer"].replies = [{"page_markdown": "(no changes needed)"}]

    result = harness["run"]()

    assert result is not None  # page still restitched and written
    outputs = json.loads(
        (harness["vault"] / "section_outputs" / f"{ENTITY}.json").read_text(encoding="utf-8")
    )
    assert outputs["family"] == CACHED_PROSE
    # The checkpoint still advances so the same bullet isn't re-detected as
    # dirty forever.
    assert new.key in load_bullet_index(harness["vault"], ENTITY)


def test_critic_gate_blocks_section_and_returns_none(harness):
    old = [_bullet(f"- old fact {i}") for i in range(10)]
    new = _bullet("- trivia not worth the wiki")
    _seed_baseline(harness["vault"], old, CACHED_PROSE)
    harness["bullets"] = old + [new]
    harness["critic"].replies = [{"include": False}]

    result = harness["run"]()

    assert result is None
    assert harness["writer"].calls == []


def test_unchanged_bullet_text_is_diff_clean(harness):
    old = [_bullet(f"- old fact {i}") for i in range(10)]
    _seed_baseline(harness["vault"], old, CACHED_PROSE)
    harness["bullets"] = list(old)

    result = harness["run"]()

    assert result is None
    assert harness["writer"].calls == []
    assert harness["critic"].calls == []


# ----------------------------------------------------------------------
# build_one_page routing (the refresh_wiki_page tool path)
# ----------------------------------------------------------------------

PROSE_FM = (
    "---\nname: Alice\nkg_node_id: node-1\ngenerated_at: 2026-07-01T00:00:00Z\n---\n\n"
    "# Alice\n\nbody\n"
)


def _seed_prose_baseline(vault: Path) -> None:
    prose = vault / "prose"
    prose.mkdir(parents=True, exist_ok=True)
    (prose / f"{ENTITY}.md").write_text(PROSE_FM, encoding="utf-8")
    out_dir = vault / "section_outputs"
    out_dir.mkdir(exist_ok=True)
    (out_dir / f"{ENTITY}.json").write_text(
        json.dumps({"family": CACHED_PROSE}), encoding="utf-8",
    )


@pytest.fixture()
def routing(tmp_path, monkeypatch):
    """Stub the heavy pieces build_one_page orchestrates; record calls."""
    import app.assistant.kg_projection as kg_projection
    import app.assistant.wiki_generator.consistency_critic as consistency_critic
    import app.assistant.wiki_generator.wiki_writer as wiki_writer

    vault = tmp_path / "vault"
    vault.mkdir()
    calls: Dict[str, list] = {
        "find_changed": [], "neighborhood": [], "rough": [],
        "affected": [], "full": [], "critic": [],
    }
    state = {"changed": ["n9"], "affected_result": vault / "prose" / f"{ENTITY}.md"}
    fake_nb = _fake_neighborhood()

    monkeypatch.setattr(
        kg_projection, "find_changed_neighborhood_nodes",
        lambda **kw: calls["find_changed"].append(kw) or list(state["changed"]),
    )
    monkeypatch.setattr(
        kg_projection, "get_entity_neighborhood",
        lambda label=None, node_id=None: calls["neighborhood"].append(
            {"label": label, "node_id": node_id}) or fake_nb,
    )
    monkeypatch.setattr(
        wiki_writer, "regenerate_entity_page",
        lambda **kw: calls["rough"].append(kw) or (vault / f"{ENTITY}.md"),
    )
    monkeypatch.setattr(
        page_writer, "regenerate_affected_sections",
        lambda **kw: calls["affected"].append(kw) or state["affected_result"],
    )
    monkeypatch.setattr(
        page_writer, "generate_prose_page_tagged",
        lambda **kw: calls["full"].append(kw) or (vault / "prose" / f"{ENTITY}.md"),
    )
    monkeypatch.setattr(
        consistency_critic, "run_consistency_critic",
        lambda **kw: calls["critic"].append(kw) or {"findings_count": 0},
    )
    return {"vault": vault, "calls": calls, "state": state, "neighborhood": fake_nb}


def test_build_one_page_no_baseline_runs_full_generation(routing):
    result = growth.build_one_page(ENTITY, routing["vault"], run_critic=False)

    assert result["status"] == "ok"
    assert "mode" not in result
    assert len(routing["calls"]["full"]) == 1
    assert routing["calls"]["affected"] == []
    assert routing["calls"]["find_changed"] == []
    # One neighborhood load threaded into both rough + prose generation.
    assert routing["calls"]["neighborhood"] == [{"label": ENTITY, "node_id": None}]
    assert routing["calls"]["rough"][0]["neighborhood"] is routing["neighborhood"]
    assert routing["calls"]["full"][0]["neighborhood"] is routing["neighborhood"]


def test_build_one_page_baseline_unchanged_skips_all_llm_work(routing):
    _seed_prose_baseline(routing["vault"])
    routing["state"]["changed"] = []

    result = growth.build_one_page(ENTITY, routing["vault"], run_critic=True)

    assert result["status"] == "unchanged"
    assert result["changed"] == 0
    assert routing["calls"]["neighborhood"] == []
    assert routing["calls"]["rough"] == []
    assert routing["calls"]["affected"] == []
    assert routing["calls"]["full"] == []
    assert routing["calls"]["critic"] == []


def test_build_one_page_baseline_with_changes_goes_incremental(routing):
    _seed_prose_baseline(routing["vault"])

    result = growth.build_one_page(ENTITY, routing["vault"], run_critic=False)

    assert result["status"] == "ok"
    assert result["mode"] == "incremental"
    assert result["changed"] == 1
    assert routing["calls"]["full"] == []
    assert len(routing["calls"]["affected"]) == 1
    affected_kw = routing["calls"]["affected"][0]
    assert affected_kw["changed_node_ids"] == ["n9"]
    assert affected_kw["neighborhood"] is routing["neighborhood"]
    assert routing["calls"]["rough"][0]["neighborhood"] is routing["neighborhood"]
    # since_ts comes from the page's generated_at frontmatter.
    assert routing["calls"]["find_changed"][0]["since_ts"].year == 2026


def test_build_one_page_incremental_noop_reports_unchanged(routing):
    _seed_prose_baseline(routing["vault"])
    routing["state"]["affected_result"] = None

    result = growth.build_one_page(ENTITY, routing["vault"], run_critic=True)

    assert result["status"] == "unchanged"
    assert result["changed"] == 1
    assert routing["calls"]["critic"] == []
