import app.assistant.tests.test_setup  # noqa: F401

import pytest

from app.assistant.control_nodes.task_compile_post_node import TaskCompilePostNode


class _BlackboardStub:
    def __init__(self, compile_seed):
        self._state = {"compile_seed": compile_seed}

    def get_state_value(self, key, default=None):
        return self._state.get(key, default)


def test_materialize_data_bindings_allows_preloaded_consumed_fact_without_producer():
    source_task = """# Task

Already provided facts and artifacts (readily consumable, do not re-materialize):
- fact_1: Mark recipient email is `mark@example.com`.

Steps:
1) Do thing.
"""
    compile_seed = {"source_task": source_task}
    node = TaskCompilePostNode(
        name="task_compile_post_node",
        blackboard=_BlackboardStub(compile_seed=compile_seed),
        agent_registry=None,
        tool_registry=None,
    )
    draft = {
        "steps": [
            {
                "id": "s1",
                "kind": "action",
                "consumes_data_ids": ["fact_1"],
                "produces_data_ids": ["artifact_1"],
            },
            {
                "id": "s2",
                "kind": "action",
                "consumes_data_ids": ["artifact_1", "fact_1"],
                "produces_data_ids": [],
            },
        ]
    }

    out = node._materialize_data_bindings(draft)
    bindings = out.get("data_bindings")

    assert isinstance(bindings, list)
    data_ids = [item.get("data_id") for item in bindings if isinstance(item, dict)]
    assert "artifact_1" in data_ids
    assert "fact_1" not in data_ids


def test_preprovided_artifact_include_compiles_to_resource_ref(tmp_path):
    # The coercer validates the include file exists, so use a real temp file
    # (the old fixture pointed at a gitignored personal file under tasks/ —
    # the test only passed on machines that had it).
    include_file = tmp_path / "contact.bio.md"
    include_file.write_text("bio context", encoding="utf-8")
    include_path = include_file.as_posix()
    source_task = f"""# Task

Already provided facts and artifacts (readily consumable, do not re-materialize):
- artifact_1: Contact bio context from include `{include_path}`.

Steps:
1) Do thing.
"""
    compile_seed = {"source_task": source_task}
    node = TaskCompilePostNode(
        name="task_compile_post_node",
        blackboard=_BlackboardStub(compile_seed=compile_seed),
        agent_registry=None,
        tool_registry=None,
    )

    preloaded = node._extract_preloaded_task_state(compile_seed=compile_seed, draft={})
    artifacts = preloaded.get("artifacts")
    assert isinstance(artifacts, dict)
    artifact_1 = artifacts.get("artifact_1")
    assert isinstance(artifact_1, dict)
    ref = artifact_1.get("__resource_ref__")
    assert isinstance(ref, dict)
    assert ref.get("kind") == "repo_file"
    assert ref.get("path") == include_path


def test_materialize_data_bindings_allows_multi_step_deterministic_fact_updates():
    source_task = """# Task

Already provided facts and artifacts (readily consumable, do not re-materialize):
- fact_1: Sender mailbox account id is `google_emi`.

Steps:
1) Do thing.
"""
    compile_seed = {"source_task": source_task}
    node = TaskCompilePostNode(
        name="task_compile_post_node",
        blackboard=_BlackboardStub(compile_seed=compile_seed),
        agent_registry=None,
        tool_registry=None,
    )
    draft = {
        "steps": [
            {
                "id": "s1_register",
                "kind": "action",
                "consumes_data_ids": ["fact_1"],
                "produces_data_ids": ["fact_3"],
                "task_state_update": {"facts": {"fact_3": False}},
            },
            {
                "id": "s2_wait",
                "kind": "wait_gate",
                "consumes_data_ids": ["fact_3"],
                "produces_data_ids": [],
                "event_fact_bindings": {
                    "signal_router.watch.email_jouko_reply": {"fact_3": True},
                    "clock.local.08_00": {"fact_3": False},
                },
            },
            {
                "id": "s3_send",
                "kind": "action",
                "consumes_data_ids": ["fact_1", "fact_3"],
                "produces_data_ids": ["fact_3"],
                "task_state_update": {"facts": {"fact_3": False}},
            },
        ]
    }

    out = node._materialize_data_bindings(draft)
    bindings = out.get("data_bindings")

    assert isinstance(bindings, list)
    data_ids = [item.get("data_id") for item in bindings if isinstance(item, dict)]
    assert "fact_3" not in data_ids


def test_materialize_data_bindings_keeps_single_producer_guard_for_artifacts():
    source_task = """# Task

Already provided facts and artifacts (readily consumable, do not re-materialize):
- fact_1: Sender mailbox account id is `google_emi`.

Steps:
1) Do thing.
"""
    compile_seed = {"source_task": source_task}
    node = TaskCompilePostNode(
        name="task_compile_post_node",
        blackboard=_BlackboardStub(compile_seed=compile_seed),
        agent_registry=None,
        tool_registry=None,
    )
    draft = {
        "steps": [
            {
                "id": "s1",
                "kind": "action",
                "consumes_data_ids": [],
                "produces_data_ids": ["artifact_2"],
            },
            {
                "id": "s2",
                "kind": "action",
                "consumes_data_ids": [],
                "produces_data_ids": ["artifact_2"],
            },
            {
                "id": "s3",
                "kind": "action",
                "consumes_data_ids": ["artifact_2"],
                "produces_data_ids": [],
            },
        ]
    }

    with pytest.raises(ValueError, match="multiple producers"):
        _ = node._materialize_data_bindings(draft)


def test_materialize_data_bindings_allows_multi_producer_fact_state_without_explicit_state_update():
    source_task = """# Task

Already provided facts and artifacts (readily consumable, do not re-materialize):
- fact_1: Sender mailbox account id is `google_emi`.

Steps:
1) Do thing.
"""
    compile_seed = {"source_task": source_task}
    node = TaskCompilePostNode(
        name="task_compile_post_node",
        blackboard=_BlackboardStub(compile_seed=compile_seed),
        agent_registry=None,
        tool_registry=None,
    )
    draft = {
        "steps": [
            {
                "id": "s1",
                "kind": "action",
                "consumes_data_ids": ["fact_1"],
                "produces_data_ids": ["fact_3"],
                "instruction": "Set task_state.facts.fact_3 = False",
            },
            {
                "id": "s2",
                "kind": "decision",
                "consumes_data_ids": ["fact_3"],
                "produces_data_ids": [],
                "condition": "task_state.facts.fact_3 == True",
            },
            {
                "id": "s3",
                "kind": "action",
                "consumes_data_ids": ["fact_3"],
                "produces_data_ids": ["fact_3"],
                "instruction": "Reset task_state.facts.fact_3 = False",
            },
        ]
    }

    out = node._materialize_data_bindings(draft)
    bindings = out.get("data_bindings")

    assert isinstance(bindings, list)
    data_ids = [item.get("data_id") for item in bindings if isinstance(item, dict)]
    assert "fact_3" not in data_ids


def test_normalize_steps_by_kind_truncates_multiple_artifact_outputs_to_first():
    """An action declaring several artifact_* outputs keeps only the FIRST —
    extras are LLM over-declarations, dropped with a warning (the old
    contract raised; truncation keeps compilation moving and dead-produces
    pruning cleans up anything unconsumed)."""
    compile_seed = {"source_task": "# Task\n\nSteps:\n1) Do thing."}
    node = TaskCompilePostNode(
        name="task_compile_post_node",
        blackboard=_BlackboardStub(compile_seed=compile_seed),
        agent_registry=None,
        tool_registry=None,
    )
    steps = [
        {
            "id": "s1",
            "kind": "action",
            "title": "Fetch two blobs",
            "executor": "personal_admin_manager",
            "instruction": "Fetch data",
            "next_step": "s2",
            "consumes_data_ids": [],
            "produces_data_ids": ["artifact_1", "artifact_2"],
        },
        {"id": "s2", "kind": "end", "title": "Done"},
    ]

    normalized = node._normalize_steps_by_kind(steps)
    s1 = next(s for s in normalized if s.get("id") == "s1")
    produces = s1.get("produces_data_ids")
    assert "artifact_1" in produces
    assert "artifact_2" not in produces
