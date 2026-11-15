"""One correction round for an agent's answer (agent_runtime/correction.py; 2026-11-15).

On 2026-11-15 the routine writer answered B558 `until: "midnight"`; the correction re-rendered the same prompt with the
problem as a string, the model never saw its answer, wrote "midnight" again and the brain run failed. Pins: the retry
continues the conversation (the original messages, the model's answer as the assistant turn, the problems); problems
of keyed list items are answered item by item against a form holding only that list and merged by key; anything else
is answered whole; the merged answer is checked whole; still refused raises AgentOutputInvalid; a form a provider
enforces itself (it raises) is corrected with the refused values named; a failure that is not the form's is not
retried; an agent with neither form nor validator is called once. Plus the rules of the two agents moved onto it, and
the registry loading validate.py. Fake model calls; no manager, no database.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.agent_registry.agent_registry import AgentRegistry
from app.assistant.agent_runtime.correction import AgentOutputInvalid, Problem, run
from app.assistant.utils.path_utils import get_app_root

AGENTS = get_app_root() / "assistant" / "agents"
WRITER = AgentRegistry()._load_validator(AGENTS / "dayflow_routine_writer" / "validate.py")
PLACEMENTS = AgentRegistry()._load_validator(AGENTS / "subconscious" / "day_placements" / "validate.py")
from app.assistant.agents.dayflow_routine_writer.agent_form import AgentForm as WriterForm  # noqa: E402
from app.assistant.agents.ticket_builder.composer.agent_form import AgentForm as ComposerForm  # noqa: E402

PROMPT = [{"role": "system", "content": "place beliefs"}, {"role": "user", "content": "today"}]
INPUT = {"belief_keys": ["B556", "B558"], "related_keys": ["B72"]}


def _judgment(key, applies, times=()):
    return {"belief_key": key, "why": "w", "applies_today": applies, "outcome": "Done" if applies else "",
            "times": [{"time": t, "until": u} for t, u in times]}


GOOD = {"judgments": [_judgment("B556", False), _judgment("B558", True, [("11:00 PM", "12:00 AM")])]}
MIDNIGHT = {"judgments": [_judgment("B556", False), _judgment("B558", True, [("11:00 PM", "midnight")])]}


class Model:
    """A fake model: answers in turn (an exception is raised), and keeps each call's messages and form."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, messages, response_format):
        self.calls.append((messages, response_format))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _agent(form=WriterForm, validator=WRITER):
    return SimpleNamespace(name="tester", config={"structured_output": form, "validator": validator})


def test_a_good_answer_is_called_once_and_returned():
    model = Model(GOOD)
    assert run(_agent(), PROMPT, WriterForm, INPUT, model) == GOOD and len(model.calls) == 1


def test_midnight_is_corrected_in_conversation_item_by_item():
    fixed = {"judgments": [_judgment("B558", True, [("11:00 PM", "12:00 AM")])]}
    model = Model(MIDNIGHT, fixed)
    assert run(_agent(), PROMPT, WriterForm, INPUT, model) == GOOD
    messages, form = model.calls[1]
    assert messages[:2] == PROMPT
    assert messages[2] == {"role": "assistant", "content": json.dumps(MIDNIGHT, ensure_ascii=False, indent=2)}
    turn = messages[3]
    assert turn["role"] == "user"
    assert 'judgments B558, times[0].until: you wrote "midnight"' in turn["content"]
    assert "midnight is written '12:00 AM'" in turn["content"]
    assert "Answer with only the judgments items named above" in turn["content"]
    assert list(form.model_fields) == ["judgments"] and form is not WriterForm


def test_a_structural_problem_is_answered_whole():
    missing = {"judgments": [_judgment("B556", False)]}
    model = Model(missing, GOOD)
    assert run(_agent(), PROMPT, WriterForm, INPUT, model) == GOOD
    messages, form = model.calls[1]
    assert form is WriterForm
    assert "judgments B558. no judgment; judge every approved belief" in messages[3]["content"]
    assert "Answer again, with the whole answer corrected." in messages[3]["content"]


def test_the_merged_answer_is_checked_whole_and_still_refused_raises():
    model = Model(MIDNIGHT, {"judgments": [_judgment("B558", True, [("11:00 PM", "midnight")])]})
    with pytest.raises(AgentOutputInvalid, match="still invalid after one correction") as raised:
        run(_agent(), PROMPT, WriterForm, INPUT, model)
    assert [p.value for p in raised.value.problems] == ["midnight"] and len(model.calls) == 2


def _refused_label() -> RuntimeError:
    bad = {"reasoning": "r", "ticket_kind": "decision", "suggestion_type": "wellness", "title": "t", "message": "m",
           "response_choices": [{"label": "Yes", "meaning": "yes", "scope": "s"},
                                {"label": "Help me follow up", "meaning": "no", "scope": "s"}]}
    try:
        ComposerForm.model_validate(bad)
    except ValidationError as e:
        try:
            raise RuntimeError(f"LLM structured_output failed: {e}") from e
        except RuntimeError as r:
            return r
    raise AssertionError("the composer's form accepted a label over 3 words")


def test_a_form_the_provider_enforces_is_corrected_with_the_refused_values_named():
    """The ticket composer, 2026-10-13: a choice labelled "Help me follow up" (over 3 words) lost the whole ask."""
    good = {"reasoning": "r", "ticket_kind": "decision", "suggestion_type": "wellness", "title": "t", "message": "m",
            "response_choices": [{"label": "Yes", "meaning": "yes", "scope": "s"},
                                 {"label": "Follow up", "meaning": "no", "scope": "s"}]}
    model = Model(_refused_label(), good)
    assert run(_agent(ComposerForm, None), PROMPT, ComposerForm, {}, model) == good
    messages, form = model.calls[1]
    assert form is ComposerForm and [m["role"] for m in messages] == ["system", "user", "user"]
    assert "response_choices, 1.label" in messages[2]["content"] and "Help me follow up" in messages[2]["content"]
    with pytest.raises(AgentOutputInvalid):
        run(_agent(ComposerForm, None), PROMPT, ComposerForm, {}, Model(_refused_label(), _refused_label()))


def test_a_failure_that_is_not_the_form_s_is_not_retried():
    model = Model(RuntimeError("connection reset"))
    with pytest.raises(RuntimeError, match="connection reset"):
        run(_agent(), PROMPT, WriterForm, INPUT, model)
    assert len(model.calls) == 1


def test_an_agent_with_neither_form_nor_validator_is_called_once_unchecked():
    model = Model("plain text")
    assert run(_agent(None, None), PROMPT, None, None, model) == "plain text" and len(model.calls) == 1


# ── the rules moved onto it ─────────────────────────────────────────────────────────────────────────────

def test_the_writer_refuses_unknown_and_related_keys_and_a_missing_judgment():
    answer = {"judgments": [_judgment("belief:B556", False), _judgment("B72", False), _judgment("B9", False)]}
    found = {(p.value or p.item, p.why) for p in WRITER.validate(answer, INPUT)}
    assert ("belief:B556", "write the key alone, as 'B556'") in found
    assert ("B72", "it is read beside the approved beliefs, not an entry: leave it out") in found
    assert ("B9", "it is not one of the beliefs given") in found
    assert ("B558", "no judgment; judge every approved belief") in found


def test_the_writer_needs_an_outcome_a_time_and_a_clock_time_until():
    answer = {"judgments": [_judgment("B556", False),
                            {**_judgment("B558", True, [("evening", "")]), "outcome": ""}]}
    found = [(p.at, p.value) for p in WRITER.validate(answer, INPUT)]
    assert found == [("outcome", None), ("times[0].time", "evening")]
    assert WRITER.validate(GOOD, INPUT) == []


def _lines(*labels):
    return [{"label": x} for x in labels]


def test_the_placements_rules():
    payload = {"lines": _lines("L1", "L2"), "concerns": _lines("P1")}
    bad = {"placements": [{"lines": ["L1", "L1"], "continues": "P4", "outcome": "o", "time": "8:00 AM",
                           "until": "midnight"}]}
    found = {(p.at, p.value, p.why) for p in PLACEMENTS.validate(bad, payload)}
    assert ("[0].continues", "P4", "it is not one of today's concerns") in found
    assert ("[0].until", "midnight",
            "the time it matters until is not a clock time such as '9:00 PM' or '7:30 AM'; "
            "midnight is written '12:00 AM'") in found
    assert ("", "L1", "put it in exactly one placement") in found
    assert ("", "L2", "it is in no placement; every line is in one") in found
    good = {"placements": [{"lines": ["L1"], "continues": "P1", "outcome": "o", "time": "8:00 AM", "until": "9:00 AM"},
                           {"lines": ["L2"], "continues": "", "outcome": "o", "time": "all day", "until": ""}]}
    assert PLACEMENTS.validate(good, payload) == []


# ── the registry ────────────────────────────────────────────────────────────────────────────────────────

def test_the_registry_loads_validate_py_and_refuses_one_without_validate(tmp_path):
    assert AgentRegistry()._load_validator(tmp_path / "validate.py") is None
    (tmp_path / "validate.py").write_text("ITEM_KEYS = {}\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="defines no validate"):
        AgentRegistry()._load_validator(tmp_path / "validate.py")
    assert callable(WRITER.validate) and WRITER.ITEM_KEYS == {"judgments": "belief_key"}
    # Managers deep-copy the agent registry (MultiAgentManagerFactory); a loaded module could not be copied.
    import copy
    assert copy.deepcopy(WRITER).validate is WRITER.validate
    assert isinstance(Problem(why="x"), Problem)
