"""Tests for the display-name registry.

Registry is populated at boot from manager configs (display_name field).
Forward (manager → display) used by the narrator; reverse (display → manager)
used by the @mention resolver.
"""
from __future__ import annotations

import pytest

from app.assistant.chat_narrator.display_names import (
    all_named_managers,
    display_name_for,
    initialize_display_name_registry,
    manager_for_display_name,
)


@pytest.fixture(autouse=True)
def _seeded_registry():
    initialize_display_name_registry({
        "web_manager": "Quimby",
        "emi_team_manager": "Em",
        "personal_admin_manager": "Phyllis",
    })
    yield
    initialize_display_name_registry({})


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


class TestRegistryHelpers:

    def test_all_named_managers_returns_copy(self):
        result = all_named_managers()
        assert result == {
            "web_manager": "Quimby",
            "emi_team_manager": "Em",
            "personal_admin_manager": "Phyllis",
        }
        # Mutating the returned dict shouldn't affect the canonical registry.
        result["fake"] = "Faker"
        assert manager_for_display_name("Faker") is None

    def test_initialize_replaces_contents(self):
        initialize_display_name_registry({"only_one": "Solo"})
        # Old entries are gone.
        assert manager_for_display_name("Quimby") is None
        # New entry is present.
        assert manager_for_display_name("Solo") == "only_one"
        assert display_name_for("only_one") == "Solo"

    def test_initialize_with_empty_dict_clears(self):
        initialize_display_name_registry({})
        assert manager_for_display_name("Quimby") is None
        assert all_named_managers() == {}

    def test_skips_invalid_entries(self):
        initialize_display_name_registry({
            "good": "Goodie",
            "": "BadKey",
            "bad_value": "",
            "another": "AnotherGood",
        })
        assert manager_for_display_name("Goodie") == "good"
        assert manager_for_display_name("AnotherGood") == "another"
        assert manager_for_display_name("BadKey") is None
        assert all_named_managers() == {"good": "Goodie", "another": "AnotherGood"}
