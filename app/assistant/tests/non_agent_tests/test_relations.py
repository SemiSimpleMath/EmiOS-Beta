"""Relations between objects (subconscious/relations.py; docs/design/objects_and_projections_2026-10-08.md).

Pins: a relation set is replaced whole, in order, without repeats; only relations in RELATIONS are written, and each
only between the kinds it joins; links read both ways (what an object links to, what links to an object). Scratch
database."""
import pytest

from app.assistant.subconscious import relations
from belief_engine.intake.store import sqlite_file


def test_a_set_is_replaced_whole_and_read_both_ways(tmp_path):
    conn = sqlite_file(tmp_path / "r.db")
    relations.ensure_schema(conn)
    entry = ("list_entry", "cal:2026-03-10:B3")
    with conn(True) as c:
        relations.replace(c, entry, "relies_on", [("work", "w1"), ("message", "m1"), ("work", "w1")], at="t", by="test")
    assert relations.targets(entry, "relies_on", connect=conn) == [("work", "w1"), ("message", "m1")]
    with conn(True) as c:
        relations.replace(c, entry, "relies_on", [("message", "m2")], at="t2", by="test")
    assert relations.targets(entry, "relies_on", connect=conn) == [("message", "m2")]
    assert relations.sources(("message", "m2"), connect=conn) == [(entry, "relies_on")]
    assert relations.sources(("work", "w1"), connect=conn) == [], "the old set is gone"


@pytest.mark.parametrize("frm,rel,to,problem", [
    (("list_entry", "e"), "covers_up", [("work", "w")], "'covers_up' is not a relation"),
    (("work", "w"), "relies_on", [("work", "w2")], "does not start from a 'work'"),
    (("brain_update", "1:c"), "attach", [("message", "m")], "does not point to ['message']"),
    (("list_entry", "e"), "relies_on", [("spaceship", "s")], "does not point to ['spaceship']"),
])
def test_only_listed_relations_between_their_kinds_are_written(tmp_path, frm, rel, to, problem):
    conn = sqlite_file(tmp_path / "r.db")
    relations.ensure_schema(conn)
    with pytest.raises(ValueError, match=problem.replace("[", r"\[").replace("]", r"\]")):
        with conn(True) as c:
            relations.replace(c, frm, rel, to, at="t", by="test")
