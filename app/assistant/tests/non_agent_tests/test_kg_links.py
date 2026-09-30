"""The people, places and things a matter names, from the knowledge graph.

Pins: any Entity node is found by its label or alias as whole words, case-insensitive, with a
possessive, including one without a card (the Karjalohja miss); a single-word name only where the
text capitalizes it unless the text is all lowercase (so "water" is not the entity Water); the
longest name at a position wins,
so "South Lake Middle School" does not also match "Lake"; the owner's own nodes are left out; what
joins two entities is the Event/State nodes linked to both and the entities reached from both
through one. An in-memory graph; invented data only.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from app.assistant.subconscious import kg_links

NODES = [("n-owner", "Sam", "Entity", '["user", "Sam"]', "The owner.", 10),
         ("n-bro", "Olli", "Entity", "[]", "The owner's brother.", 10),
         ("n-kar", "Karjalohja", "Entity", "[]", "A place the family travels to.", 8),
         ("n-dad", "Aarne", "Entity", "[]", "The owner's father.", 9),
         ("n-sls", "South Lake Middle School", "Entity", "[]", "A school.", 7),
         ("n-lake", "Lake", "Entity", "[]", "A lake.", 1),
         ("n-wife", "Leena", "Entity", '["my wife"]', "The owner's wife.", 10),
         ("n-water", "Water", "Entity", "[]", "Water.", 7),
         ("n-cake", "Princess Cake", "Entity", "[]", "A cake.", 3),
         ("n-trip", "Summer Trip", "Event", "[]", "The brother and the father at Karjalohja.", 6),
         ("n-own", "Ownership", "State", "[]", "Owns the cottage at Karjalohja.", 5)]
EDGES = [("n-trip", "n-bro"), ("n-trip", "n-kar"), ("n-trip", "n-dad"), ("n-own", "n-kar"),
         ("n-own", "n-bro"), ("n-own", "n-owner"), ("n-trip", "n-owner")]


@pytest.fixture(autouse=True)
def graph(monkeypatch):
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE kg_node_metadata (id, label, node_type, aliases, description, importance, "
               "start_date, end_date, pagerank_score)")
    db.execute("CREATE TABLE kg_edge_metadata (source_id, target_id)")
    db.executemany("INSERT INTO kg_node_metadata VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?)",
                   [(*n, 0.02 if n[1] in ("Olli", "Leena", "Sam") else 0.001) for n in NODES])
    db.executemany("INSERT INTO kg_edge_metadata VALUES (?, ?)", EDGES)
    monkeypatch.setattr(kg_links, "_raw", lambda sql, params=(): db.execute(sql, tuple(params)).fetchall())
    monkeypatch.setattr("app.assistant.kg_core.user_identity.get_primary_user_name", lambda: "Sam")


def _labels(found):
    return [e["label"] for e in found]


def test_entities_are_found_by_name_whatever_the_case_and_possessive():
    found = kg_links.find_entities(["Email from olli: I contacted the attorney about Karjalohja's cottage."])
    assert sorted(_labels(found)) == ["Karjalohja", "Olli"]
    assert _labels(kg_links.find_entities(["my wife said hi"])) == ["Leena"]


def test_the_longest_name_wins_and_the_owner_is_left_out():
    found = kg_links.find_entities(["Sam got a note from South Lake Middle School."])
    assert _labels(found) == ["South Lake Middle School"]


def test_what_joins_two_entities():
    joined = kg_links.shared(["n-bro", "n-kar"])
    assert sorted(_labels(joined["links"])) == ["Ownership", "Summer Trip"]
    assert _labels(joined["entities"]) == ["Aarne"], "the father was on the trip that links them both"
    joined = kg_links.shared(["n-bro", "n-dad"])
    assert _labels(joined["links"]) == ["Summer Trip"] and _labels(joined["entities"]) == ["Karjalohja"]
    assert kg_links.shared(["n-bro"]) == {"links": [], "entities": []}


def test_case_decides_a_single_word_name_unless_the_text_is_all_lowercase():
    assert _labels(kg_links.find_entities(["He drank water with Leena and ordered a princess cake."])) == [
        "Leena", "Princess Cake"]
    assert _labels(kg_links.find_entities(["only leena drinks water"])) == ["Leena", "Water"]
    assert _labels(kg_links.find_entities(["Talked to leena about the Water bill."])) == ["Leena", "Water"], (
        "a central person matches in lowercase; others need the capital")
