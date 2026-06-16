"""LLM merge verifier (#2, scratch/BELIEF-ENGINE-NEW.md §5).

The deterministic step (embedding NN) only PROPOSES that two phrases might be the same
concept; this asks an LLM to DECIDE. Injected into `Registry` as
`verifier(phrase, candidate_label, ctype) -> bool`; only a "yes" merges.

The decision is made by the `belief_engine::merge_verifier` AGENT, so it runs through the
normal agent runtime — provider-agnostic model routing, structured output, and prompts under
app/assistant/agents/ — instead of a hand-rolled LLM call. The agent is built lazily on first
use (unit tests inject a fake verifier and never create it).
"""
from __future__ import annotations

_AGENT = "belief_engine::merge_verifier"

# Human-readable gloss per concept type, passed into the agent's prompt.
_CTYPE_NOUN = {
    "subject": "who or what a belief is about",
    "predicate": "the relationship or claim being made",
    "object": "what the claim is about",
    "qualifier": "a qualifying detail",
}


class LLMMergeVerifier:
    """Callable verifier backed by the belief_engine::merge_verifier agent.

    Signature `(phrase, candidate_label, ctype) -> bool`, matching the Registry's injected
    verifier contract. `.calls` counts decisions made. The model lives in the agent's config
    (not a constructor argument) — one source of truth, and the agent runtime routes it to
    whichever provider is configured/available.
    """

    def __init__(self) -> None:
        self.calls = 0
        self._agent = None

    def _get_agent(self):
        if self._agent is None:
            from app.assistant.ServiceLocator.service_locator import DI
            self._agent = DI.agent_factory.create_agent(_AGENT)
            if self._agent is None:
                raise RuntimeError(f"{_AGENT} agent unavailable")
        return self._agent

    def __call__(self, phrase: str, candidate_label: str, ctype: str) -> bool:
        from app.assistant.utils.pydantic_classes import Message
        self.calls += 1
        result = self._get_agent().action_handler(Message(agent_input={
            "phrase_a": phrase,
            "phrase_b": candidate_label,
            "ctype": ctype,
            "ctype_noun": _CTYPE_NOUN.get(ctype, "concept"),
        }))
        data = getattr(result, "data", None) or {}
        same = data.get("same") if isinstance(data, dict) else getattr(data, "same", None)
        if same is None:
            raise RuntimeError(f"{_AGENT}: cannot read 'same' from agent output: {data!r}")
        return bool(same)
