"""Agent-runtime A3 hygiene (2026-07-09).

A3a: the action contract was validated only when the agent set action_required.
An agent that emitted an action WITHOUT requiring one had it routed by
FlowController unvalidated. Now an action is validated whenever it's present.

A3b: FinalAnswerNormalizer.normalize serialized the whole result dict as the
answer when no recognized answer field was present. Now it yields an empty
answer (the other keys still surface via data_list) — arbitrary internal fields
must not become the reply (cf. RSM2).
"""
from __future__ import annotations

import pytest

from app.assistant.agent_runtime.services.action_contract_service import ActionContractService
from app.assistant.agent_runtime.services.action_validator import ActionValidator
from app.assistant.agent_runtime.services.final_answer_normalizer import FinalAnswerNormalizer


class FakePolicy:
    def __init__(self, allowed):
        self._allowed = set(allowed)

    def get_allowed_actions(self):
        return set(self._allowed)

    def is_tool_action(self, name):
        return name in self._allowed


def _svc(action_required, allowed):
    return ActionContractService(
        "a", {"action_required": action_required}, FakePolicy(allowed), ActionValidator()
    )


class TestA3aActionValidation:

    def test_non_required_agent_with_no_action_is_left_alone(self):
        _svc(False, {"read_file"}).enforce({"final_answer_answer": "hi"})  # no raise

    def test_non_required_agent_emitting_disallowed_action_now_raises(self):
        with pytest.raises(ValueError):
            _svc(False, {"read_file"}).enforce({"action": "send_email", "action_input": {"to": "x"}})

    def test_non_required_agent_emitting_allowed_action_passes(self):
        _svc(False, {"send_email"}).enforce({"action": "send_email", "action_input": {"to": "x"}})

    def test_required_agent_still_validates(self):
        with pytest.raises(ValueError):
            _svc(True, {"read_file"}).enforce({"action": "send_email", "action_input": {"to": "x"}})

    def test_planner_step_complete_sentinel_passes(self):
        # Regression (verification 2026-07-11): A3 made every EMITTED action validate,
        # which broke the Planner's control sentinels — spec_step_feeder instructs the
        # planner to emit action="step_complete", which is not a tool/node and not in
        # the policy's allowed set, so every step completion raised. Sentinels pass
        # (they carry no action_input and are consumed by the agent class itself).
        _svc(False, {"read_file"}).enforce({"action": "step_complete"})   # no raise
        _svc(False, {"read_file"}).enforce({"action": "none"})            # no raise
        _svc(True, {"read_file"}).enforce({"action": "step_complete"})    # required agent too

    def test_sentinel_exemption_is_a_closed_set(self):
        # A disallowed real tool still raises — the sentinel pass opens nothing else.
        with pytest.raises(ValueError):
            _svc(False, {"read_file"}).enforce({"action": "step_completely_bogus", "action_input": "{}"})


class TestA3bFinalAnswerNormalize:

    def test_dict_without_recognized_answer_yields_empty_answer(self):
        out = FinalAnswerNormalizer.normalize({"internal_state": "secret", "debug": {"x": 1}})
        assert out["final_answer_answer"] == ""  # not the serialized whole dict
        keys = {d["key"] for d in out["final_answer_data_list"]}
        assert "internal_state" in keys  # still visible as a detail, not the headline answer

    def test_final_answer_answer_is_preserved(self):
        assert FinalAnswerNormalizer.normalize({"final_answer_answer": "hello"})["final_answer_answer"] == "hello"

    def test_recognized_answer_alias_used(self):
        assert FinalAnswerNormalizer.normalize({"answer": "hi there"})["final_answer_answer"] == "hi there"

    def test_non_dict_result_is_still_stringified(self):
        assert FinalAnswerNormalizer.normalize("just a string")["final_answer_answer"] == "just a string"
