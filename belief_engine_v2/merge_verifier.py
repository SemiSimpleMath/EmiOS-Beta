"""Native LLM merge verifier (#2, scratch/BELIEF-ENGINE-NEW.md §5).

The deterministic step (embedding NN) only proposes that two phrases MIGHT be the same
concept — it generates a candidate. THIS asks an LLM to DECIDE. Injected into `Registry` as
`verifier(phrase, candidate_label, ctype) -> bool`; only a "yes" merges.

Built lazily so the module imports with no LLM running (the unit tests use a fake verifier and
never touch this). On first call it grabs the app's LLMFactory interface with a cheap model.
This is OUR mechanism — deliberately NOT the KG's.
"""
from __future__ import annotations

from typing import Any

# Tight yes/no contract. `reason` is for logging/inspection, not control flow.
_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "same": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["same", "reason"],
}

_CTYPE_NOUN = {
    "subject": "who or what a belief is about",
    "predicate": "the relationship or claim being made",
    "object": "what the claim is about",
    "qualifier": "a qualifying detail",
}

_SYS = (
    "You decide whether two phrasings express the SAME belief/preference in one person's belief "
    "store, so that duplicates collapse to one concept. Answer TRUE when they assert the same "
    "underlying claim about the same subject — same cadence, day, time, deadline, recipient, and "
    "scope — even if the wording, emphasis, length, or incidental extra clauses differ. Answer "
    "FALSE when any load-bearing detail differs: different cadence (weekly vs monthly), different "
    "day or time (10:00 vs 17:00), different deadline hardness, different subject/recipient, or a "
    "genuinely different claim. Ignore phrasing, ordering, and extra detail; focus only on whether "
    "the core fact is identical. Reply in JSON: {\"same\": bool, \"reason\": short}."
)


class LLMMergeVerifier:
    """Callable verifier backed by the app LLM stack. `.calls` counts decisions made."""

    def __init__(self, *, provider: str = "openai", engine: str = "gpt-5.4-mini",
                 temperature: float = 0.0) -> None:
        self.provider = provider
        self.engine = engine
        self.temperature = temperature
        self.calls = 0
        self._iface = None

    def _interface(self):
        if self._iface is None:
            from app.services.llm_factory import LLMFactory
            self._iface = LLMFactory.get_llm_interface(llm_provider=self.provider, engine=self.engine)
        return self._iface

    def __call__(self, phrase: str, candidate_label: str, ctype: str) -> bool:
        self.calls += 1
        noun = _CTYPE_NOUN.get(ctype, "concept")
        user = (
            f"Concept type: {ctype} ({noun}).\n"
            f"Phrase A: {phrase!r}\n"
            f"Phrase B: {candidate_label!r}\n"
            "Do A and B refer to the same canonical concept for this person? Answer in JSON."
        )
        messages = [
            {"role": "system", "content": _SYS},
            {"role": "user", "content": user},
        ]
        event = self._interface().structured_output_json(
            messages, response_format=_SCHEMA, engine=self.engine, temperature=self.temperature,
        )
        return _extract_same(event)


def _extract_same(event: Any) -> bool:
    """Read the boolean decision; fail loudly on an unrecognized shape (no silent default)."""
    if isinstance(event, dict):
        if "same" in event:
            return bool(event["same"])
    else:
        same = getattr(event, "same", None)
        if same is not None:
            return bool(same)
    raise RuntimeError(f"merge verifier: cannot read 'same' from response {type(event)!r}: {event!r}")
