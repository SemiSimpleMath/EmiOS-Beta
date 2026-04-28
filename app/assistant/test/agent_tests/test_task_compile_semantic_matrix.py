from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ScenarioContract:
    scenario_id: str
    title: str
    min_action_steps: int = 1
    min_wait_steps: int = 0
    min_decision_steps: int = 0
    require_terminal_end: bool = True
    forbid_loop_back_edge: bool = False
    require_loop_back_edge: bool = False
    required_event_prefixes: tuple[str, ...] = ()
    required_action_executors: tuple[str, ...] = ()
    required_condition_substrings: tuple[str, ...] = ()


SCENARIO_MATRIX: list[ScenarioContract] = [
    ScenarioContract(
        scenario_id="S1",
        title="Linear one-shot task",
        min_action_steps=1,
        min_wait_steps=0,
        min_decision_steps=0,
        forbid_loop_back_edge=True,
    ),
    ScenarioContract(
        scenario_id="S2",
        title="Multi-manager linear task",
        min_action_steps=2,
        min_wait_steps=0,
        min_decision_steps=0,
        required_action_executors=("personal_admin_manager", "web_manager"),
        forbid_loop_back_edge=True,
    ),
    ScenarioContract(
        scenario_id="S3",
        title="Timed start plus event wake",
        min_action_steps=1,
        min_wait_steps=2,
        min_decision_steps=0,
        required_event_prefixes=("clock.local.", "signal_router.watch."),
    ),
    ScenarioContract(
        scenario_id="S4",
        title="Event or timeout branch",
        min_action_steps=1,
        min_wait_steps=1,
        min_decision_steps=1,
    ),
    ScenarioContract(
        scenario_id="S5",
        title="Loop with priority routing and hard stop",
        min_action_steps=2,
        min_wait_steps=1,
        min_decision_steps=2,
        require_loop_back_edge=True,
    ),
    ScenarioContract(
        scenario_id="S6",
        title="Phase shift by time window",
        min_action_steps=2,
        min_wait_steps=2,
        min_decision_steps=2,
        required_event_prefixes=("signal_router.watch.",),
    ),
    ScenarioContract(
        scenario_id="S7",
        title="Compound trigger OR",
        min_action_steps=1,
        min_wait_steps=1,
        min_decision_steps=1,
        required_condition_substrings=("event_", " or "),
    ),
    ScenarioContract(
        scenario_id="S8",
        title="Compound trigger AND",
        min_action_steps=1,
        min_wait_steps=1,
        min_decision_steps=1,
        required_condition_substrings=(" and ",),
    ),
    ScenarioContract(
        scenario_id="S9",
        title="Mixed boolean grouping",
        min_action_steps=1,
        min_wait_steps=1,
        min_decision_steps=2,
        required_condition_substrings=("(", ")", " or "),
    ),
    ScenarioContract(
        scenario_id="S10",
        title="Missing identifier requires lookup before side effects",
        min_action_steps=2,
        min_wait_steps=0,
        min_decision_steps=1,
    ),
]


@dataclass(frozen=True)
class ExpectedAtomicEvent:
    id: str
    event_type: str
    canonical_event_name: str


@dataclass(frozen=True)
class ExpectedAtomicAction:
    id: str
    executor: str


@dataclass(frozen=True)
class ScenarioBlueprint:
    scenario_id: str
    expected_events: tuple[ExpectedAtomicEvent, ...] = field(default_factory=tuple)
    expected_actions: tuple[ExpectedAtomicAction, ...] = field(default_factory=tuple)


S6_BLUEPRINT = ScenarioBlueprint(
    scenario_id="S6",
    expected_events=(
        ExpectedAtomicEvent("event_1", "watch_event", "signal_router.watch.katy_mention"),
        ExpectedAtomicEvent("event_2", "time_event", "clock.local.18_00"),
        ExpectedAtomicEvent("event_3", "watch_event", "signal_router.watch.jukka_mention"),
        ExpectedAtomicEvent("event_4", "time_event", "clock.local.24_00"),
    ),
    expected_actions=(
        ExpectedAtomicAction("action_1", "personal_admin_manager"),
        ExpectedAtomicAction("action_2", "personal_admin_manager"),
    ),
)


def _step_edges(step: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key in ("next_step", "on_true", "on_false"):
        value = step.get(key)
        if isinstance(value, str) and value.strip():
            out.append(value.strip())
    return out


def validate_compiled_task(contract: ScenarioContract, compiled_task: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    if str(compiled_task.get("schema_version") or "") != "task_ir_v1":
        errors.append("schema_version must be task_ir_v1")
        return errors

    steps = compiled_task.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append("steps must be a non-empty list")
        return errors

    step_ids: list[str] = []
    step_map: dict[str, dict[str, Any]] = {}
    for step in steps:
        if not isinstance(step, dict):
            errors.append("all steps must be objects")
            continue
        step_id = str(step.get("id") or "").strip()
        if not step_id:
            errors.append("step missing id")
            continue
        if step_id in step_map:
            errors.append(f"duplicate step id: {step_id}")
            continue
        step_ids.append(step_id)
        step_map[step_id] = step

    entry_step_id = str(compiled_task.get("entry_step_id") or "").strip()
    if not entry_step_id:
        errors.append("entry_step_id is required")
    elif entry_step_id not in step_map:
        errors.append(f"entry_step_id not found in steps: {entry_step_id}")

    action_steps = [s for s in step_map.values() if str(s.get("kind") or "") == "action"]
    wait_steps = [s for s in step_map.values() if str(s.get("kind") or "") == "wait_for_event"]
    decision_steps = [s for s in step_map.values() if str(s.get("kind") or "") == "decision"]
    end_steps = [s for s in step_map.values() if str(s.get("kind") or "") == "end"]

    if len(action_steps) < contract.min_action_steps:
        errors.append(f"requires at least {contract.min_action_steps} action step(s)")
    if len(wait_steps) < contract.min_wait_steps:
        errors.append(f"requires at least {contract.min_wait_steps} wait_for_event step(s)")
    if len(decision_steps) < contract.min_decision_steps:
        errors.append(f"requires at least {contract.min_decision_steps} decision step(s)")
    if contract.require_terminal_end and not end_steps:
        errors.append("requires at least one end step")

    if contract.required_action_executors:
        executors = {
            str(step.get("executor") or "").strip()
            for step in action_steps
            if isinstance(step.get("executor"), str)
        }
        for required in contract.required_action_executors:
            if required not in executors:
                errors.append(f"missing required action executor: {required}")

    if contract.required_event_prefixes:
        event_names = [
            str(step.get("event_name") or "").strip()
            for step in wait_steps
            if isinstance(step.get("event_name"), str) and str(step.get("event_name")).strip()
        ]
        for prefix in contract.required_event_prefixes:
            if not any(name.startswith(prefix) for name in event_names):
                errors.append(f"missing wait_for_event with prefix: {prefix}")

    if contract.required_condition_substrings:
        conditions = [
            str(step.get("condition") or "")
            for step in decision_steps
            if isinstance(step.get("condition"), str)
        ]
        for marker in contract.required_condition_substrings:
            if not any(marker in cond for cond in conditions):
                errors.append(f"missing decision condition marker: {marker}")

    index_by_id = {step_id: idx for idx, step_id in enumerate(step_ids)}
    has_loop_back_edge = False
    for step_id, step in step_map.items():
        src_idx = index_by_id.get(step_id, -1)
        for dst in _step_edges(step):
            if dst not in step_map:
                errors.append(f"edge points to unknown step: {step_id} -> {dst}")
                continue
            dst_idx = index_by_id[dst]
            if dst_idx <= src_idx:
                has_loop_back_edge = True

    if contract.forbid_loop_back_edge and has_loop_back_edge:
        errors.append("loop back-edge found but forbidden for scenario")
    if contract.require_loop_back_edge and not has_loop_back_edge:
        errors.append("requires at least one loop back-edge")

    return errors


def validate_phase_i_atoms_exact(blueprint: ScenarioBlueprint, atoms: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(atoms, dict):
        return ["phase_i_atoms must be a dict"]

    events = atoms.get("events")
    actions = atoms.get("actions")
    if not isinstance(events, list):
        errors.append("phase_i_atoms.events must be a list")
        return errors
    if not isinstance(actions, list):
        errors.append("phase_i_atoms.actions must be a list")
        return errors

    event_map = {
        str(item.get("id") or "").strip(): item
        for item in events
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    action_map = {
        str(item.get("id") or "").strip(): item
        for item in actions
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }

    for expected_event in blueprint.expected_events:
        found = event_map.get(expected_event.id)
        if not isinstance(found, dict):
            errors.append(f"missing atomic event: {expected_event.id}")
            continue
        if str(found.get("event_type") or "") != expected_event.event_type:
            errors.append(f"event {expected_event.id} has wrong event_type")
        if str(found.get("canonical_event_name") or "") != expected_event.canonical_event_name:
            errors.append(f"event {expected_event.id} has wrong canonical_event_name")

    for expected_action in blueprint.expected_actions:
        found = action_map.get(expected_action.id)
        if not isinstance(found, dict):
            errors.append(f"missing atomic action: {expected_action.id}")
            continue
        if str(found.get("executor") or "") != expected_action.executor:
            errors.append(f"action {expected_action.id} has wrong executor")

    return errors


def validate_phase_ii_logic_tree_bindings(*, logic_tree: dict[str, Any], atoms: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(logic_tree, dict):
        return ["logic_tree must be a dict"]
    bindings = logic_tree.get("bindings")
    if not isinstance(bindings, list) or not bindings:
        return ["logic_tree.bindings must be a non-empty list"]

    atom_ids: set[str] = set()
    if isinstance(atoms, dict):
        events = atoms.get("events", [])
        conditions = atoms.get("conditions", [])
        actions = atoms.get("actions", [])
        if isinstance(events, list):
            atom_ids.update(
                str(item.get("id") or "").strip()
                for item in events
                if isinstance(item, dict) and isinstance(item.get("id"), str) and str(item.get("id")).strip()
            )
        if isinstance(conditions, list):
            atom_ids.update(
                str(item.get("id") or "").strip()
                for item in conditions
                if isinstance(item, dict) and isinstance(item.get("id"), str) and str(item.get("id")).strip()
            )
        if isinstance(actions, list):
            atom_ids.update(
                str(item.get("id") or "").strip()
                for item in actions
                if isinstance(item, dict) and isinstance(item.get("id"), str) and str(item.get("id")).strip()
            )

    for binding in bindings:
        if not isinstance(binding, dict):
            errors.append("logic_tree binding must be object")
            continue
        atomic_id = str(binding.get("atomic_id") or "").strip()
        if not atomic_id:
            errors.append("logic_tree binding missing atomic_id")
            continue
        if atomic_id not in atom_ids:
            errors.append(f"logic_tree binding references unknown atomic_id: {atomic_id}")
    return errors


def _compiled_task_or_and_example() -> dict[str, Any]:
    return {
        "schema_version": "task_ir_v1",
        "task_id": "semantic_example",
        "created_at_utc": "2026-02-26T00:00:00+00:00",
        "compiler_name": "task_compile_manager",
        "compiler_version": "v1",
        "source_task": "wait for X or Y",
        "source_information": "",
        "source_hash": "abc123abc123abcd",
        "entry_step_id": "s1",
        "steps": [
            {
                "id": "s1",
                "kind": "wait_for_event",
                "title": "Wait canonical trigger",
                "event_name": "signal_router.watch.any_x_or_y",
                "next_step": "s2",
            },
            {
                "id": "s2",
                "kind": "decision",
                "title": "Branch by matched trigger",
                "condition": "event_1 == True or event_2 == True",
                "on_true": "s3",
                "on_false": "s4",
            },
            {
                "id": "s3",
                "kind": "action",
                "title": "Handle trigger",
                "executor": "personal_admin_manager",
                "instruction": "Send notification.",
                "next_step": "s4",
            },
            {"id": "s4", "kind": "end", "title": "Done"},
        ],
    }


def test_semantic_matrix_is_well_formed():
    ids = [item.scenario_id for item in SCENARIO_MATRIX]
    assert len(ids) == 10
    assert len(set(ids)) == len(ids)
    assert all(item.title for item in SCENARIO_MATRIX)


def test_validator_accepts_valid_compound_or_example():
    contract = next(item for item in SCENARIO_MATRIX if item.scenario_id == "S7")
    errors = validate_compiled_task(contract=contract, compiled_task=_compiled_task_or_and_example())
    assert errors == []


def test_validator_rejects_missing_required_wait_prefix():
    contract = next(item for item in SCENARIO_MATRIX if item.scenario_id == "S3")
    invalid = {
        "schema_version": "task_ir_v1",
        "task_id": "bad_s3",
        "created_at_utc": "2026-02-26T00:00:00+00:00",
        "compiler_name": "task_compile_manager",
        "compiler_version": "v1",
        "source_task": "",
        "source_information": "",
        "source_hash": "abc123abc123abcd",
        "entry_step_id": "s1",
        "steps": [
            {
                "id": "s1",
                "kind": "wait_for_event",
                "title": "Wait for watch only",
                "event_name": "signal_router.watch.email_from_katy",
                "next_step": "s2",
            },
            {
                "id": "s2",
                "kind": "action",
                "title": "Handle result",
                "executor": "personal_admin_manager",
                "instruction": "Send an update.",
                "next_step": "s3",
            },
            {"id": "s3", "kind": "end", "title": "Done"},
        ],
    }
    errors = validate_compiled_task(contract=contract, compiled_task=invalid)
    assert any("clock.local." in err for err in errors)


def test_phase_i_atoms_can_be_validated_exactly_for_s6_blueprint():
    atoms = {
        "events": [
            {
                "id": "event_1",
                "event_type": "watch_event",
                "canonical_event_name": "signal_router.watch.katy_mention",
            },
            {
                "id": "event_2",
                "event_type": "time_event",
                "canonical_event_name": "clock.local.18_00",
            },
            {
                "id": "event_3",
                "event_type": "watch_event",
                "canonical_event_name": "signal_router.watch.jukka_mention",
            },
            {
                "id": "event_4",
                "event_type": "time_event",
                "canonical_event_name": "clock.local.24_00",
            },
        ],
        "actions": [
            {"id": "action_1", "executor": "personal_admin_manager"},
            {"id": "action_2", "executor": "personal_admin_manager"},
        ],
    }
    errors = validate_phase_i_atoms_exact(S6_BLUEPRINT, atoms)
    assert errors == []


def test_phase_ii_logic_tree_binding_references_known_atoms():
    atoms = {
        "events": [{"id": "event_1"}, {"id": "event_2"}],
        "actions": [{"id": "action_1"}],
    }
    logic_tree = {
        "entry_step_id": "s1",
        "edges": [
            {"from_step_id": "s1", "to_step_id": "s2", "reason": "next_step"},
        ],
        "bindings": [
            {"step_id": "s1", "atomic_type": "event", "atomic_id": "event_1"},
            {"step_id": "s2", "atomic_type": "action", "atomic_id": "action_1"},
        ],
    }
    errors = validate_phase_ii_logic_tree_bindings(logic_tree=logic_tree, atoms=atoms)
    assert errors == []
