"""Tests for the display-name registry.

Forward (manager → display) was already exercised by the narrator's tests.
These cover the reverse path used by the @mention resolver, plus
case-insensitivity and unknown-name handling.
"""
from __future__ import annotations

from app.assistant.chat_narrator.display_names import (
    DISPLAY_NAMES,
    all_named_managers,
    display_name_for,
    manager_for_display_name,
)


class TestForwardLookup:

    def test_known_managers(self):
        assert display_name_for("web_manager") == "Quimby"
        assert display_name_for("emi_team_manager") == "Em"

    def test_unknown_falls_back_to_humanized(self):
        assert display_name_for("brand_new_manager") == "Brand New"
        assert display_name_for("foo") == "Foo"

    def test_empty_returns_em(self):
        assert display_name_for("") == "Em"
        assert display_name_for(None) == "Em"  # type: ignore[arg-type]


class TestReverseLookup:

    def test_exact_known_name(self):
        assert manager_for_display_name("Quimby") == "web_manager"
        assert manager_for_display_name("Em") == "emi_team_manager"
        assert manager_for_display_name("Phyllis") == "personal_admin_manager"

    def test_case_insensitive(self):
        assert manager_for_display_name("quimby") == "web_manager"
        assert manager_for_display_name("QUIMBY") == "web_manager"
        assert manager_for_display_name("QuImBy") == "web_manager"

    def test_unknown_returns_none(self):
        assert manager_for_display_name("Steve") is None
        assert manager_for_display_name("notaworker") is None

    def test_empty_returns_none(self):
        assert manager_for_display_name("") is None
        assert manager_for_display_name(None) is None  # type: ignore[arg-type]

    def test_whitespace_trimmed(self):
        assert manager_for_display_name("  Quimby  ") == "web_manager"


class TestRegistryHelper:

    def test_all_named_managers_returns_copy(self):
        result = all_named_managers()
        assert result == DISPLAY_NAMES
        # Mutating the returned dict shouldn't affect the canonical registry.
        result["fake"] = "Faker"
        assert "fake" not in DISPLAY_NAMES
