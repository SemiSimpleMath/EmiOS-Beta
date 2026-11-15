"""One correction round for an agent's answer (docs/design/agent_output_correction_2026-11-15.md).

An agent's answer is checked against its form (the Pydantic `AgentForm` from `agent_form.py`) and, when its folder has
a `validate.py`, against the agent's own rules: `validate(answer, agent_input) -> list[Problem]`. A refused answer gets
one correction round that continues the conversation: the original messages, the model's answer as the assistant turn
(when there is one: a form a provider enforces itself refuses by raising, and then only the refused values are
known), and the problems as the next user turn (shared/correction.j2). When every problem is a field of a list item
named by the validator's ITEM_KEYS, the model answers only those items, against a form holding only that list, and
each returned item takes the place of the item with the same key; otherwise it answers the whole form again and that
answer replaces the old one. The result is checked whole once more; still refused, it raises AgentOutputInvalid.

Until 2026-11-15 each caller re-rendered the same prompt with a `correction` string, and the model never saw the
answer it was told to correct: the routine writer wrote `until: "midnight"` twice and the brain run failed. Three
correction mechanisms are replaced by this one: those callers, `llm_client._revalidate_or_reask` and TicketComposer's.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from pydantic import BaseModel, ValidationError, create_model

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class Problem:
    """What is wrong with an answer. `field` is the top-level field ("" for the answer as a whole), `item` the key of
    the list item it is in ("" when none), `at` where inside it ("times[0].until"), `value` what the model wrote, and
    `why` what is wrong, with the correct form when there is one. A `structural` problem (an item missing, repeated or
    not one of those given; anything the form refuses) is corrected by answering the whole form again."""
    why: str
    field: str = ""
    item: str = ""
    at: str = ""
    value: Any = None
    structural: bool = False


class AgentOutputInvalid(ValueError):
    def __init__(self, agent_name: str, problems: List[Problem]):
        self.agent_name = agent_name
        self.problems = problems
        super().__init__(f"[{agent_name}] answer still invalid after one correction: "
                         + "; ".join(line(p) for p in problems))


def line(p: Problem) -> str:
    where = " ".join(x for x in (p.field, p.item, p.at) if x)
    return f"{where}: {p.why}" if where else p.why


def _from_validation_error(e: ValidationError) -> List[Problem]:
    return [Problem(why=err["msg"], field=str(err["loc"][0]) if err["loc"] else "",
                    at=".".join(str(x) for x in err["loc"][1:]), value=err.get("input"), structural=True)
            for err in e.errors()]


def _refusal(exc: BaseException) -> Optional[ValidationError]:
    """The form's refusal in the chain of a failed call; None when the call failed for another reason."""
    seen: Optional[BaseException] = exc
    while seen is not None:
        if isinstance(seen, ValidationError):
            return seen
        seen = seen.__cause__ or seen.__context__
    return None


def _check(form: Optional[type], validator, answer: Any, agent_input: Dict[str, Any]) -> List[Problem]:
    """The form's problems first; the agent's own rules only for an answer that fits its form."""
    if not isinstance(answer, dict):
        return [Problem(why=f"the answer must be one JSON object, not {type(answer).__name__}", structural=True)]
    if form is not None:
        try:
            form.model_validate(answer)
        except ValidationError as e:
            return _from_validation_error(e)
    return list(validator.validate(answer, agent_input)) if validator is not None else []


def _attempt(call, messages, response_format) -> Tuple[Any, List[Problem]]:
    """(the answer, the form's refusal of it when the provider raised one)."""
    try:
        answer = call(messages, response_format)
        return (answer.model_dump() if isinstance(answer, BaseModel) else answer), []
    except Exception as exc:
        refused = _refusal(exc)
        if refused is None:
            raise
        return None, _from_validation_error(refused)


def _partial_field(validator, form: Optional[type], answer: Any, problems: List[Problem]) -> Optional[str]:
    """The one list field every problem is an item field of, when the answer can be corrected item by item."""
    keys = getattr(validator, "ITEM_KEYS", {}) if validator is not None else {}
    fields = {p.field for p in problems}
    if (isinstance(answer, dict) and form is not None and len(fields) == 1 and next(iter(fields)) in keys
            and all(p.item and not p.structural for p in problems)):
        return next(iter(fields))
    return None


def _limited_form(form: type, field: str) -> type:
    info = form.model_fields[field]
    return create_model(f"{form.__name__}Correction", **{field: (info.annotation, info)})


def _merge(answer: Dict[str, Any], field: str, key: str, fixed: Dict[str, Any]) -> Dict[str, Any]:
    """The answer with each returned item in place of the item with the same key; an item with a key the answer does
    not have is added, and the whole-answer check refuses it if it does not belong."""
    items = list(answer.get(field) or [])
    where = {str(it.get(key)): i for i, it in enumerate(items) if isinstance(it, dict)}
    for it in fixed.get(field) or []:
        k = str((it or {}).get(key))
        if k in where:
            items[where[k]] = it
        else:
            items.append(it)
    return {**answer, field: items}


def _turn(problems: List[Problem], field: Optional[str]) -> str:
    from app.assistant.agent_runtime.services.prompt_builder import _jinja_env
    return _jinja_env.get_template("shared/correction.j2").render(
        problems=[{**p.__dict__, "value": None if p.value is None else json.dumps(p.value, ensure_ascii=False)}
                  for p in problems], field=field)


def run(agent, messages: List[Dict[str, Any]], response_format: Any, agent_input: Optional[Dict[str, Any]],
        call: Callable[[List[Dict[str, Any]], Any], Any]) -> Any:
    """The answer to `messages`, checked and corrected once. `call(messages, response_format)` is the model call.
    Agents with neither a Pydantic form nor a validator are called once, unchecked."""
    form = response_format if isinstance(response_format, type) and issubclass(response_format, BaseModel) else None
    validator = agent.config.get("validator")
    if form is None and validator is None:
        return call(messages, response_format)
    agent_input = agent_input if isinstance(agent_input, dict) else {}

    answer, problems = _attempt(call, messages, response_format)
    if not problems:
        problems = _check(form, validator, answer, agent_input)
    if not problems:
        return answer
    logger.warning("[%s] answer refused, one correction: %s", agent.name, [line(p) for p in problems])

    field = _partial_field(validator, form, answer, problems)
    retry = list(messages)
    if answer is not None:
        retry.append({"role": "assistant", "content": json.dumps(answer, ensure_ascii=False, indent=2)
                      if isinstance(answer, dict) else str(answer)})
    retry.append({"role": "user", "content": _turn(problems, field)})

    if field is None:
        new, still = _attempt(call, retry, response_format)
    else:
        fixed, still = _attempt(call, retry, _limited_form(form, field))
        if not still and not isinstance(fixed, dict):
            still = [Problem(why=f"the correction must be one JSON object, not {type(fixed).__name__}",
                             structural=True)]
        new = _merge(answer, field, validator.ITEM_KEYS[field], fixed) if not still else None
    if not still:
        still = _check(form, validator, new, agent_input)
    if still:
        raise AgentOutputInvalid(agent.name, still)
    return new
