"""Unit tests for the skill registry — parser, validator, registry.

Pure tests, no DI bootstrap. Each test builds a temp ``skills/`` tree
and exercises one piece.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.skill_registry.models import Skill, SkillHeader, ValidationResult
from app.skill_registry.parser import parse_skill_md
from app.skill_registry.skill_registry import SkillRegistry


# ─── helpers ──────────────────────────────────────────────────────


def _write_skill(root: Path, name: str, frontmatter: dict, body: str = "Body content.") -> Path:
    """Create skills/<name>/SKILL.md in ``root`` with the given frontmatter."""
    import yaml as _yaml
    sd = root / name
    sd.mkdir(parents=True, exist_ok=True)
    fm_text = _yaml.safe_dump(frontmatter, sort_keys=False).strip()
    text = f"---\n{fm_text}\n---\n{body}\n"
    p = sd / "SKILL.md"
    p.write_text(text, encoding="utf-8")
    return p


# ─── parser / validator: happy path ───────────────────────────────


def test_parse_minimal_valid_skill(tmp_path):
    p = _write_skill(tmp_path, "slack-formatting", {
        "name": "slack-formatting",
        "description": "Slack uses its own mrkdwn dialect. Use when responding on Slack.",
    }, body="# Body\n\nDo this not that.")
    skill, result = parse_skill_md(p)
    assert result.ok is True
    assert result.errors == []
    assert skill is not None
    assert skill.name == "slack-formatting"
    assert "mrkdwn" in skill.description
    assert "Do this" in skill.body


def test_parse_with_optional_fields(tmp_path):
    p = _write_skill(tmp_path, "pdf-processing", {
        "name": "pdf-processing",
        "description": "Extract text from PDFs.",
        "license": "Apache-2.0",
        "compatibility": "Designed for Claude Code.",
        "metadata": {"author": "jukka", "version": "1.0"},
        "allowed-tools": "Bash(jq:*) Read",
    })
    skill, result = parse_skill_md(p)
    assert result.ok
    assert skill.license == "Apache-2.0"
    assert skill.compatibility.startswith("Designed for")
    assert skill.metadata == {"author": "jukka", "version": "1.0"}
    assert skill.allowed_tools == "Bash(jq:*) Read"


# ─── parser / validator: errors ────────────────────────────────────


def test_missing_name_fails(tmp_path):
    p = _write_skill(tmp_path, "noname", {"description": "x"})
    skill, result = parse_skill_md(p)
    assert skill is None
    assert any("'name' is required" in e for e in result.errors)


def test_missing_description_fails(tmp_path):
    p = _write_skill(tmp_path, "nodesc", {"name": "nodesc"})
    skill, result = parse_skill_md(p)
    assert skill is None
    assert any("'description' is required" in e for e in result.errors)


def test_name_must_match_dir(tmp_path):
    p = _write_skill(tmp_path, "actual-dir", {
        "name": "different-name",
        "description": "x",
    })
    skill, result = parse_skill_md(p)
    assert skill is None
    assert any("must match parent directory name" in e for e in result.errors)


def test_name_uppercase_rejected(tmp_path):
    p = _write_skill(tmp_path, "BAD-NAME", {
        "name": "BAD-NAME",
        "description": "x",
    })
    skill, result = parse_skill_md(p)
    assert skill is None
    assert any("must match [a-z0-9-]" in e for e in result.errors)


def test_name_consecutive_hyphen_rejected(tmp_path):
    p = _write_skill(tmp_path, "bad--name", {
        "name": "bad--name",
        "description": "x",
    })
    skill, result = parse_skill_md(p)
    assert skill is None
    assert any("consecutive hyphens" in e for e in result.errors)


def test_name_leading_hyphen_rejected(tmp_path):
    p = _write_skill(tmp_path, "-bad", {
        "name": "-bad",
        "description": "x",
    })
    skill, result = parse_skill_md(p)
    assert skill is None
    # leading hyphen is caught by the regex check
    assert any("must match [a-z0-9-]" in e for e in result.errors)


def test_name_too_long_rejected(tmp_path):
    long_name = "a" * 65
    p = _write_skill(tmp_path, long_name, {
        "name": long_name,
        "description": "x",
    })
    skill, result = parse_skill_md(p)
    assert skill is None
    assert any("exceeds 64 chars" in e for e in result.errors)


def test_description_too_long_rejected(tmp_path):
    p = _write_skill(tmp_path, "longdesc", {
        "name": "longdesc",
        "description": "x" * 1025,
    })
    skill, result = parse_skill_md(p)
    assert skill is None
    assert any("exceeds 1024 chars" in e for e in result.errors)


def test_metadata_must_be_mapping(tmp_path):
    p = _write_skill(tmp_path, "badmeta", {
        "name": "badmeta",
        "description": "x",
        "metadata": ["not", "a", "map"],
    })
    skill, result = parse_skill_md(p)
    assert skill is None
    assert any("'metadata' must be a mapping" in e for e in result.errors)


def test_empty_body_warns_but_loads(tmp_path):
    p = _write_skill(tmp_path, "emptybody", {
        "name": "emptybody",
        "description": "Test empty body.",
    }, body="")
    skill, result = parse_skill_md(p)
    assert skill is not None
    assert result.ok
    assert any("body is empty" in w for w in result.warnings)


# ─── registry ──────────────────────────────────────────────────────


def test_registry_loads_valid_skills(tmp_path):
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "alpha", {"name": "alpha", "description": "First skill."})
    _write_skill(skills_dir, "beta", {"name": "beta", "description": "Second skill."})

    reg = SkillRegistry(base_dir=tmp_path)
    headers = reg.headers()
    names = sorted(h.name for h in headers)
    assert names == ["alpha", "beta"]
    assert reg.get("alpha").description == "First skill."


def test_registry_skips_malformed_skill(tmp_path):
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "good", {"name": "good", "description": "ok"})
    _write_skill(skills_dir, "bad", {"name": "wrong-name", "description": "bad"})  # name/dir mismatch

    reg = SkillRegistry(base_dir=tmp_path)
    names = [h.name for h in reg.headers()]
    assert "good" in names
    assert "bad" not in names
    assert reg.get("bad") is None


def test_registry_skips_dir_without_skill_md(tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "empty-dir").mkdir()  # no SKILL.md
    _write_skill(skills_dir, "real", {"name": "real", "description": "ok"})

    reg = SkillRegistry(base_dir=tmp_path)
    assert [h.name for h in reg.headers()] == ["real"]


def test_registry_skips_hidden_and_underscore_dirs(tmp_path):
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, ".hidden", {"name": ".hidden", "description": "x"})
    _write_skill(skills_dir, "_template", {"name": "_template", "description": "x"})
    _write_skill(skills_dir, "real", {"name": "real", "description": "ok"})

    reg = SkillRegistry(base_dir=tmp_path)
    assert [h.name for h in reg.headers()] == ["real"]


def test_registry_handles_missing_skills_dir(tmp_path):
    # No skills/ subdir at all — registry should be empty, not crash.
    reg = SkillRegistry(base_dir=tmp_path)
    assert reg.headers() == []


def test_registry_get_returns_none_for_unknown_name(tmp_path):
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "alpha", {"name": "alpha", "description": "x"})
    reg = SkillRegistry(base_dir=tmp_path)
    assert reg.get("nonexistent") is None


def test_registry_discover_matches_by_keyword(tmp_path):
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "slack-formatting", {
        "name": "slack-formatting",
        "description": "Slack uses its own mrkdwn dialect for bold and italic.",
    })
    _write_skill(skills_dir, "doordash-ordering", {
        "name": "doordash-ordering",
        "description": "How to navigate DoorDash modals when ordering food.",
    })
    _write_skill(skills_dir, "pdf-processing", {
        "name": "pdf-processing",
        "description": "Extract text and tables from PDF documents.",
    })

    reg = SkillRegistry(base_dir=tmp_path)
    hits = reg.discover("how do I send something on slack")
    assert hits, "expected at least one match for 'slack'"
    assert hits[0].name == "slack-formatting"

    hits = reg.discover("PDF tables please")
    assert hits[0].name == "pdf-processing"


def test_registry_discover_returns_empty_on_no_match(tmp_path):
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "alpha", {"name": "alpha", "description": "Something obscure."})
    reg = SkillRegistry(base_dir=tmp_path)
    assert reg.discover("zzzzz totally unrelated query") == []


def test_registry_discover_empty_query_returns_empty(tmp_path):
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "alpha", {"name": "alpha", "description": "x"})
    reg = SkillRegistry(base_dir=tmp_path)
    assert reg.discover("") == []
