"""The people, places and things a matter names, from the knowledge graph.

Pins: any Entity node is found by its label or alias as whole words, case-insensitive, with a
possessive, including one without a card (the Karjalohja miss); a single-word name only where the
text capitalizes it unless the text is all lowercase (so "water" is not the entity Water); the
longest name at a position wins,
so "South Lake Middle School" does not also match "Lake"; the owner's own nodes are left out; what
joins two entities is the Event/State nodes linked to both and the entities reached from both
through one; each entity found carries its ties to the household's people (the sentences of its links
through a node that also links to one of them); facts by meaning are the closest link sentences, read
back from the graph, deduplicated. An in-memory graph and a fake edge index; invented data only.
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
EDGES = [("e1", "n-trip", "n-bro", "Olli went on the summer trip.", 5),
         ("e2", "n-trip", "n-kar", "The summer trip was at Karjalohja.", 6),
         ("e3", "n-trip", "n-dad", "Aarne went on the summer trip.", 5),
         ("e4", "n-own", "n-kar", "The cottage at Karjalohja is owned.", 7),
         ("e5", "n-own", "n-bro", "Olli owns the cottage.", 7),
         ("e6", "n-own", "n-owner", "Sam co-owns the cottage.", 4),
         ("e7", "n-trip", "n-owner", "Sam went on the summer trip.", 3)]


@pytest.fixture(autouse=True)
def graph(monkeypatch):
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE kg_node_metadata (id, label, node_type, aliases, description, importance, "
               "start_date, end_date, pagerank_score)")
    db.execute("CREATE TABLE kg_edge_metadata (id, source_id, target_id, sentence, importance)")
    db.executemany("INSERT INTO kg_node_metadata VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?)",
                   [(*n, 0.02 if n[1] in ("Olli", "Leena", "Sam") else 0.001) for n in NODES])
    db.executemany("INSERT INTO kg_edge_metadata VALUES (?, ?, ?, ?, ?)", EDGES)
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


def test_an_entity_found_carries_its_ties_to_the_household():
    [kar] = kg_links.find_entities(["The cottage at Karjalohja needs a new roof."])
    assert kar["ties"] == ["The cottage at Karjalohja is owned.", "The summer trip was at Karjalohja."]
    [school] = kg_links.find_entities(["South Lake Middle School sent a note."])
    assert school["ties"] == [], "no link reaches the household"


def test_facts_by_meaning_are_the_closest_link_sentences(monkeypatch):
    class Index:
        def search_similar_edges(self, vector, k):
            return {1: [("e4", 0.61), ("e2", 0.55)], 2: [("e2", 0.70), ("e9-gone", 0.9)]}[vector[0]]
    monkeypatch.setattr("app.assistant.kg.chroma.chroma_embedding_manager.get_chroma_manager", lambda: Index())
    monkeypatch.setattr("app.assistant.embeddings.embedder.embed_texts", lambda texts: [[1], [2]][:len(texts)])
    facts = kg_links.facts_by_meaning(["The roof at the cottage", "A trip in summer", ""])
    assert facts == [{"sentence": "The summer trip was at Karjalohja.", "score": 0.7},
                     {"sentence": "The cottage at Karjalohja is owned.", "score": 0.61}], (
        "best score per sentence; an id the graph no longer has is dropped")
    assert kg_links.facts_by_meaning(["", " "]) == []
