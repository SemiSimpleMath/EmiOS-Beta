from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any
import yaml

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root, resolve_repo_path

logger = get_logger(__name__)


class TaskCompilePostNode(ControlNode):
    """
    Deterministic post-compile normalizer.

    Merges adjacent action steps that target the same executor so the runtime
    does not leave and re-enter the same manager unnecessarily.
    """

    def action_handler(self, message):
        _ = message
        self.blackboard.update_state_value("next_agent", None)
        cfg = self._cfg()
        previous_agents = self._required_previous_agents(cfg)
        next_agent = self._required_str(cfg, "next_agent")

        last_agent = self.blackboard.get_state_value("last_agent", None)
        if not isinstance(last_agent, str) or last_agent not in previous_agents:
            raise ValueError(
                f"[{self.name}] expected last_agent in {previous_agents} before post-processing, got: {last_agent!r}"
            )

        compiled_task = self.blackboard.get_state_value("compiled_task", None)
        if compiled_task is None:
            raise ValueError(f"[{self.name}] Missing required 'compiled_task' in state.")
        if not isinstance(compiled_task, dict):
            raise TypeError(f"[{self.name}] compiled_task must be dict, got {type(compiled_task)}.")

        merged_draft = self._merge_adjacent_executor_actions(compiled_task)
        merged_draft = self._inject_wait_watch_registrations(merged_draft)
        merged_draft = self._materialize_data_bindings(merged_draft)
        merged_draft = self._normalize_wait_gate_release_conditions(merged_draft)
        finalized_task = self._materialize_compiled_task(draft=merged_draft)
        # Option B: emit a work-object TEMPLATE directly — task_ir_v1 is NEVER persisted. The
        # normalization above produced a clean, validated step graph in memory; build_template maps it to
        # work-object nodes/edges/contracts. A kind/pattern the runner can't yet run (an unknown kind, a
        # richer loop) makes build_template raise here — the compile fails loud, never emitting unrunnable
        # output. Consumers (run_task, the taskrunner routine) detect driver=task_runner and drive it.
        from app.assistant.task_runtime.wo_builder import build_template
        work_object_template = build_template(finalized_task)
        self.blackboard.update_state_value("compiled_task", work_object_template)
        compiled_file_path = self._persist_compiled_task(merged_task=work_object_template, cfg=cfg)
        self.blackboard.update_state_value("compiled_task_file", compiled_file_path)
        self.blackboard.update_state_value("last_agent", self.name)
        self.blackboard.update_state_value("next_agent", next_agent)

    def _persist_compiled_task(self, *, merged_task: dict[str, Any], cfg: dict[str, Any]) -> str:
        auto_write = cfg.get("auto_write_compiled_task", True)
        if not isinstance(auto_write, bool):
            raise TypeError(f"[{self.name}] auto_write_compiled_task must be bool, got {type(auto_write)}.")
        if not auto_write:
            raise ValueError(f"[{self.name}] auto_write_compiled_task=false is not supported.")

        repo_root = get_repo_root()
        compile_seed = self._require_compile_seed()

        task_id_raw = str(compile_seed.get("task_id") or merged_task.get("task_id") or "").strip()
        if not task_id_raw:
            raise ValueError(f"[{self.name}] compiled_task.task_id is required for auto-write.")
        task_id_safe = re.sub(r"[^A-Za-z0-9._-]+", "_", task_id_raw).strip("._-")
        if not task_id_safe:
            raise ValueError(f"[{self.name}] compiled_task.task_id produced empty safe filename.")
        output_file_path = self._resolve_compiled_output_file_path(
            repo_root=repo_root,
            task_id_safe=task_id_safe,
            compile_seed=compile_seed,
        )
        output_file_path.parent.mkdir(parents=True, exist_ok=True)
        output_file_path.write_text(
            json.dumps(merged_task, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        # Registry is now filesystem-driven — no write needed here.
        # load_task_registry() scans tasks/*/task_spec.md dynamically.

        rel_output = output_file_path.relative_to(repo_root).as_posix()
        logger.info("[%s] Wrote compiled task output: %s", self.name, rel_output)
        return rel_output

    def _resolve_compiled_output_file_path(
        self,
        *,
        repo_root: Path,
        task_id_safe: str,
        compile_seed: dict[str, Any],
    ) -> Path:
        task_file = str(compile_seed.get("task_file") or "").strip()
        if task_file:
            task_spec_path = resolve_repo_path(task_file)
            return (task_spec_path.parent / f"{task_id_safe}.json").resolve()
        default_dir = (repo_root / "tasks" / task_id_safe).resolve()
        return (default_dir / f"{task_id_safe}.json").resolve()

    def _upsert_task_registry(
        self,
        *,
        repo_root: Path,
        task_id: str,
        compile_seed: dict[str, Any],
    ) -> None:
        registry_path = (repo_root / "tasks" / "registry.yaml").resolve()
        if not registry_path.exists():
            raise FileNotFoundError(f"[{self.name}] task registry file not found: {registry_path}")
        raw = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"[{self.name}] tasks/registry.yaml must be a YAML object.")
        tasks = raw.get("tasks")
        if tasks is None:
            tasks = []
        if not isinstance(tasks, list):
            raise ValueError(f"[{self.name}] tasks/registry.yaml key 'tasks' must be a list.")

        task_file = str(compile_seed.get("task_file") or "").strip()
        if task_file:
            task_file_rel = task_file
        else:
            task_file_rel = f"tasks/{task_id}/task_spec.md"

        task_name = task_id.replace("_", " ").strip()
        task_description = str(compile_seed.get("task_description") or "").strip()
        if not task_description:
            task_description = f"Compiled task for '{task_id}'."

        aliases = [task_id, task_name]
        aliases = [a for a in aliases if isinstance(a, str) and a.strip()]
        dedup_aliases: list[str] = []
        for alias in aliases:
            if alias not in dedup_aliases:
                dedup_aliases.append(alias)

        entry = {
            "id": task_id,
            "name": task_name,
            "task_file": task_file_rel,
            "description": task_description,
            "aliases": dedup_aliases,
        }

        found_index = None
        for idx, item in enumerate(tasks):
            if not isinstance(item, dict):
                continue
            if str(item.get("id") or "").strip() == task_id:
                found_index = idx
                break
        if found_index is None:
            tasks.append(entry)
            logger.info("[%s] Added task to registry: %s", self.name, task_id)
        else:
            tasks[found_index] = entry
            logger.info("[%s] Updated task in registry: %s", self.name, task_id)

        raw["tasks"] = tasks
        registry_path.write_text(
            yaml.safe_dump(raw, sort_keys=False, allow_unicode=False),
            encoding="utf-8",
        )

    def _require_compile_seed(self) -> dict[str, Any]:
        compile_seed = self.blackboard.get_state_value("compile_seed", None)
        if not isinstance(compile_seed, dict):
            raise TypeError(f"[{self.name}] compile_seed must be dict, got {type(compile_seed)}.")
        return compile_seed

    def _merge_adjacent_executor_actions(self, compiled_task: dict) -> dict:
        merged_task = dict(compiled_task)
        steps = merged_task.get("steps")
        if not isinstance(steps, list):
            raise TypeError(f"[{self.name}] compiled_task.steps must be list.")
        normalized_steps: list[dict[str, Any]] = []
        for step in steps:
            if not isinstance(step, dict):
                raise TypeError(f"[{self.name}] compiled_task.steps entries must be dict.")
            normalized_steps.append(dict(step))

        merged_id_map: dict[str, str] = {}
        i = 0
        out_steps: list[dict[str, Any]] = []
        while i < len(normalized_steps):
            current = dict(normalized_steps[i])
            if i + 1 >= len(normalized_steps):
                out_steps.append(current)
                i += 1
                continue

            nxt = dict(normalized_steps[i + 1])
            can_merge = (
                current.get("kind") == "action"
                and nxt.get("kind") == "action"
                and isinstance(current.get("executor"), str)
                and isinstance(nxt.get("executor"), str)
                and current.get("executor") == nxt.get("executor")
                and isinstance(current.get("next_step"), str)
                and isinstance(nxt.get("id"), str)
                and current.get("next_step") == nxt.get("id")
            )

            if not can_merge:
                out_steps.append(current)
                i += 1
                continue

            title_a = str(current.get("title") or "").strip()
            title_b = str(nxt.get("title") or "").strip()
            instr_a = str(current.get("instruction") or "").strip()
            instr_b = str(nxt.get("instruction") or "").strip()

            current["title"] = f"{title_a} + {title_b}".strip(" +")
            if instr_a and instr_b:
                current["instruction"] = f"{instr_a}\nThen: {instr_b}"
            elif instr_b:
                current["instruction"] = instr_b
            current["consumes_data_ids"] = self._merge_str_list(
                current.get("consumes_data_ids"),
                nxt.get("consumes_data_ids"),
            )
            current["produces_data_ids"] = self._merge_str_list(
                current.get("produces_data_ids"),
                nxt.get("produces_data_ids"),
            )
            # Internal dependencies between merged same-manager steps should not
            # survive as external step-level consumes.
            current["consumes_data_ids"] = [
                data_id
                for data_id in current["consumes_data_ids"]
                if data_id not in current["produces_data_ids"]
            ]
            current["next_step"] = nxt.get("next_step", "")
            current_id = str(current.get("id") or "").strip()
            merged_away_id = str(nxt.get("id") or "").strip()
            if current_id and merged_away_id and current_id != merged_away_id:
                merged_id_map[merged_away_id] = current_id
            out_steps.append(current)
            i += 2

        def _resolve_step_ref(step_id: str) -> str:
            value = str(step_id or "").strip()
            if not value:
                return ""
            seen: set[str] = set()
            while value in merged_id_map:
                if value in seen:
                    raise ValueError(f"[{self.name}] detected merge-id cycle at step id '{value}'.")
                seen.add(value)
                value = str(merged_id_map[value]).strip()
                if not value:
                    raise ValueError(f"[{self.name}] merge-id map resolved to empty step id.")
            return value

        for step in out_steps:
            for edge_key in ("next_step", "on_true", "on_false"):
                raw_edge = step.get(edge_key)
                if not isinstance(raw_edge, str) or not raw_edge.strip():
                    continue
                step[edge_key] = _resolve_step_ref(raw_edge)

        entry_step_id = str(merged_task.get("entry_step_id") or "").strip()
        if entry_step_id:
            merged_task["entry_step_id"] = _resolve_step_ref(entry_step_id)
        merged_task["steps"] = self._normalize_steps_by_kind(out_steps)
        return merged_task

    @staticmethod
    def _extract_email_sender_from_query(semantic_query: str) -> str | None:
        """
        Deterministically extract an email address from a semantic query string.
        Returns the first email-like token found (e.g. 'user@example.com'), or None.
        Used to populate sender_equals in email_semantic_match predicates so the
        garbage filter can cheaply drop mismatched senders without an LLM call.
        """
        match = re.search(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", semantic_query)
        return match.group(0).strip().lower() if match else None

    def _inject_wait_watch_registrations(self, compiled_task: dict[str, Any]) -> dict[str, Any]:
        phase_i_atoms = self.blackboard.get_state_value("phase_i_atoms", None)
        logic_tree = self.blackboard.get_state_value("logic_tree", None)
        if not isinstance(phase_i_atoms, dict):
            raise TypeError(f"[{self.name}] phase_i_atoms must be dict, got {type(phase_i_atoms)}.")
        if not isinstance(logic_tree, dict):
            raise TypeError(f"[{self.name}] logic_tree must be dict, got {type(logic_tree)}.")

        task_id = self._compile_seed_task_id()
        event_atoms = self._event_atoms_by_id(phase_i_atoms=phase_i_atoms)
        wait_event_bindings = self._wait_event_bindings(logic_tree=logic_tree)

        out = dict(compiled_task)
        steps = out.get("steps")
        if not isinstance(steps, list):
            raise TypeError(f"[{self.name}] compiled_task.steps must be list.")

        normalized_steps: list[dict[str, Any]] = []
        for raw_step in steps:
            if not isinstance(raw_step, dict):
                raise TypeError(f"[{self.name}] compiled_task step must be dict, got {type(raw_step)}.")
            step = dict(raw_step)
            kind = str(step.get("kind") or "").strip()
            if kind not in {"wait_for_event", "wait_gate"}:
                normalized_steps.append(step)
                continue

            step_id = str(step.get("id") or "").strip()
            if not step_id:
                raise ValueError(f"[{self.name}] wait step missing id.")

            event_atom_ids = wait_event_bindings.get(step_id, [])
            if kind == "wait_for_event":
                event_name = str(step.get("event_name") or "").strip()
                if not event_name:
                    raise ValueError(f"[{self.name}] wait_for_event step missing event_name.")
                if len(event_atom_ids) != 1:
                    raise ValueError(
                        f"[{self.name}] wait_for_event step '{step_id}' requires exactly one event binding, got {len(event_atom_ids)}."
                    )
                event_atom_id = event_atom_ids[0]
                event_atom = event_atoms.get(event_atom_id)
                if not isinstance(event_atom, dict):
                    raise ValueError(f"[{self.name}] missing event atom '{event_atom_id}' for wait step '{step_id}'.")
                atom_event_type = str(event_atom.get("event_type") or "").strip()
                atom_event_name = str(event_atom.get("canonical_event_name") or "").strip()
                if atom_event_name and atom_event_name != event_name:
                    raise ValueError(
                        f"[{self.name}] wait_for_event '{step_id}' event_name '{event_name}' mismatches atom canonical_event_name '{atom_event_name}'."
                    )
                if atom_event_type != "watch_event" or not event_name.startswith("signal_router.watch."):
                    normalized_steps.append(step)
                    continue
                atom_description = str(event_atom.get("description") or "").strip()
                if not atom_description:
                    raise ValueError(
                        f"[{self.name}] watch_event atom '{event_atom_id}' requires non-empty description for deterministic registration."
                    )
                watch_type = "email_semantic_match" if event_name.startswith("signal_router.watch.email_") else "semantic_match"
                email_predicate: dict[str, Any] = {"semantic_query": atom_description}
                if watch_type == "email_semantic_match":
                    sender = self._extract_email_sender_from_query(atom_description)
                    if sender:
                        email_predicate["sender_equals"] = sender
                step["watch_registration"] = {
                    "watch_key": f"task::{task_id}::{step_id}",
                    "event_name": event_name,
                    "watch_type": watch_type,
                    "predicate": email_predicate,
                    "dedupe_window_seconds": 300,
                    "metadata": {
                        "source": "task_compile_post_node",
                        "atomic_event_id": event_atom_id,
                        "task_id": task_id,
                    },
                }
            else:
                subscriptions = step.get("subscriptions")
                if not isinstance(subscriptions, list) or not subscriptions:
                    raise ValueError(f"[{self.name}] wait_gate step '{step_id}' requires non-empty subscriptions list.")
                sub_set = {str(s or "").strip() for s in subscriptions if str(s or "").strip()}
                if not sub_set:
                    raise ValueError(f"[{self.name}] wait_gate step '{step_id}' subscriptions must contain non-empty strings.")
                regs: list[dict[str, Any]] = []
                for event_atom_id in event_atom_ids:
                    event_atom = event_atoms.get(event_atom_id)
                    if not isinstance(event_atom, dict):
                        raise ValueError(f"[{self.name}] missing event atom '{event_atom_id}' for wait_gate '{step_id}'.")
                    atom_event_type = str(event_atom.get("event_type") or "").strip()
                    atom_event_name = str(event_atom.get("canonical_event_name") or "").strip()
                    if atom_event_name and atom_event_name not in sub_set:
                        continue
                    if atom_event_type != "watch_event":
                        continue
                    if not atom_event_name.startswith("signal_router.watch."):
                        continue
                    atom_description = str(event_atom.get("description") or "").strip()
                    if not atom_description:
                        raise ValueError(
                            f"[{self.name}] watch_event atom '{event_atom_id}' requires non-empty description for deterministic registration."
                        )
                    watch_type = (
                        "email_semantic_match" if atom_event_name.startswith("signal_router.watch.email_") else "semantic_match"
                    )
                    atom_predicate: dict[str, Any] = {"semantic_query": atom_description}
                    if watch_type == "email_semantic_match":
                        sender = self._extract_email_sender_from_query(atom_description)
                        if sender:
                            atom_predicate["sender_equals"] = sender
                    regs.append(
                        {
                            "watch_key": f"task::{task_id}::{step_id}::{event_atom_id}",
                            "event_name": atom_event_name,
                            "watch_type": watch_type,
                            "predicate": atom_predicate,
                            "dedupe_window_seconds": 300,
                            "metadata": {
                                "source": "task_compile_post_node",
                                "atomic_event_id": event_atom_id,
                                "task_id": task_id,
                            },
                        }
                    )
                if regs:
                    step["watch_registrations"] = regs
            normalized_steps.append(step)

        out["steps"] = normalized_steps
        return out

    def _event_atoms_by_id(self, *, phase_i_atoms: dict[str, Any]) -> dict[str, dict[str, Any]]:
        events = phase_i_atoms.get("events")
        if not isinstance(events, list):
            raise ValueError(f"[{self.name}] phase_i_atoms.events must be list.")
        out: dict[str, dict[str, Any]] = {}
        for item in events:
            if not isinstance(item, dict):
                raise TypeError(f"[{self.name}] phase_i_atoms.events entries must be dict.")
            event_id = str(item.get("id") or "").strip()
            if not event_id:
                raise ValueError(f"[{self.name}] phase_i_atoms.events entries require non-empty id.")
            if event_id in out:
                raise ValueError(f"[{self.name}] duplicate phase_i event id: {event_id}")
            out[event_id] = item
        return out

    def _wait_event_bindings(self, *, logic_tree: dict[str, Any]) -> dict[str, list[str]]:
        bindings = logic_tree.get("bindings")
        if not isinstance(bindings, list):
            raise ValueError(f"[{self.name}] logic_tree.bindings must be list.")
        out: dict[str, list[str]] = {}
        for binding in bindings:
            if not isinstance(binding, dict):
                raise TypeError(f"[{self.name}] logic_tree.bindings entries must be dict.")
            atomic_type = str(binding.get("atomic_type") or "").strip()
            if atomic_type != "event":
                continue
            step_id = str(binding.get("step_id") or "").strip()
            atomic_id = str(binding.get("atomic_id") or "").strip()
            if not step_id or not atomic_id:
                raise ValueError(f"[{self.name}] event binding requires non-empty step_id and atomic_id.")
            if step_id not in out:
                out[step_id] = []
            if atomic_id not in out[step_id]:
                out[step_id].append(atomic_id)
        return out

    def _compile_seed_task_id(self) -> str:
        compile_seed = self.blackboard.get_state_value("compile_seed", None)
        if not isinstance(compile_seed, dict):
            raise TypeError(f"[{self.name}] compile_seed must be dict, got {type(compile_seed)}.")
        task_id = str(compile_seed.get("task_id") or "").strip()
        if not task_id:
            raise ValueError(f"[{self.name}] compile_seed.task_id must be non-empty.")
        return task_id

    def _materialize_compiled_task(self, *, draft: dict[str, Any]) -> dict[str, Any]:
        compile_seed = self.blackboard.get_state_value("compile_seed", None)
        if not isinstance(compile_seed, dict):
            raise TypeError(f"[{self.name}] compile_seed must be dict, got {type(compile_seed)}.")

        required_seed_keys = (
            "task_id",
            "created_at_utc",
            "compiler_name",
            "compiler_version",
            "source_hash",
            "source_task",
            "source_information",
        )
        for key in required_seed_keys:
            value = compile_seed.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"[{self.name}] compile_seed['{key}'] must be a non-empty string.")

        entry_step_id = draft.get("entry_step_id")
        if not isinstance(entry_step_id, str) or not entry_step_id.strip():
            raise ValueError(f"[{self.name}] compiled_task.entry_step_id must be a non-empty string.")
        steps = draft.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError(f"[{self.name}] compiled_task.steps must be a non-empty list.")
        data_bindings = draft.get("data_bindings")
        if data_bindings is None:
            data_bindings = []
        if not isinstance(data_bindings, list):
            raise ValueError(f"[{self.name}] compiled_task.data_bindings must be list when provided.")

        preloaded_task_state = self._extract_preloaded_task_state(compile_seed=compile_seed, draft=draft)
        task_llm_params = self._extract_task_llm_params(compile_seed=compile_seed)

        out = {
            "schema_version": "task_ir_v1",
            "task_id": str(compile_seed["task_id"]).strip(),
            "created_at_utc": str(compile_seed["created_at_utc"]).strip(),
            "compiler_name": str(compile_seed["compiler_name"]).strip(),
            "compiler_version": str(compile_seed["compiler_version"]).strip(),
            "source_task": str(compile_seed["source_task"]).strip(),
            "source_information": str(compile_seed.get("source_information") or "").strip(),
            "source_hash": str(compile_seed["source_hash"]).strip(),
            "entry_step_id": entry_step_id.strip(),
            "steps": steps,
            "data_bindings": data_bindings,
            "preloaded_task_state": preloaded_task_state,
        }
        if isinstance(task_llm_params, dict) and task_llm_params:
            out["llm_params"] = task_llm_params
        return out

    def _materialize_data_bindings(self, compiled_task: dict[str, Any]) -> dict[str, Any]:
        out = dict(compiled_task)
        steps = out.get("steps")
        if not isinstance(steps, list):
            raise ValueError(f"[{self.name}] compiled_task.steps must be list for data binding materialization.")
        preloaded_ids = self._preloaded_data_ids_from_compile_seed()
        steps = self._prune_dead_produced_data_ids(steps)
        out["steps"] = steps
        step_order: dict[str, int] = {}
        producer_by_id: dict[str, str] = {}
        consumers_by_id: dict[str, list[str]] = {}
        kind_by_id: dict[str, str] = {}
        deterministic_fact_ids: set[str] = set()
        multi_producer_fact_ids: set[str] = set()

        for idx, raw in enumerate(steps):
            if not isinstance(raw, dict):
                raise TypeError(f"[{self.name}] step must be dict, got {type(raw)}.")
            step_id = str(raw.get("id") or "").strip()
            if not step_id:
                raise ValueError(f"[{self.name}] step id is required for data binding materialization.")
            if step_id in step_order:
                raise ValueError(f"[{self.name}] duplicate step id during data binding materialization: {step_id}")
            step_order[step_id] = idx

            consumes = self._normalize_data_ids(raw.get("consumes_data_ids"), field_name="consumes_data_ids")
            produces = self._normalize_data_ids(raw.get("produces_data_ids"), field_name="produces_data_ids")
            deterministic_step_facts = self._deterministic_fact_ids_for_step(raw)

            for data_id in produces:
                if data_id in deterministic_step_facts and data_id.startswith("fact_"):
                    # Deterministic task-state writes are not manager materialization bindings.
                    deterministic_fact_ids.add(data_id)
                    kind_by_id[data_id] = self._data_kind_for_id(data_id)
                    continue
                if data_id in producer_by_id:
                    if data_id.startswith("fact_"):
                        # Facts can be mutable task-state variables across a loop.
                        # Treat multi-writer facts as stateful and skip binding materialization.
                        multi_producer_fact_ids.add(data_id)
                        continue
                    raise ValueError(
                        f"[{self.name}] data id '{data_id}' has multiple producers: "
                        f"{producer_by_id[data_id]} and {step_id}"
                    )
                producer_by_id[data_id] = step_id
                kind_by_id[data_id] = self._data_kind_for_id(data_id)

            for data_id in consumes:
                if data_id not in consumers_by_id:
                    consumers_by_id[data_id] = []
                consumers_by_id[data_id].append(step_id)
                if data_id not in kind_by_id:
                    kind_by_id[data_id] = self._data_kind_for_id(data_id)

        bindings: list[dict[str, Any]] = []
        for data_id, consumers in consumers_by_id.items():
            if data_id in deterministic_fact_ids:
                continue
            if data_id in multi_producer_fact_ids:
                logger.info(
                    "[%s] Skipping data binding for mutable multi-producer fact '%s'.",
                    self.name,
                    data_id,
                )
                continue
            producer_step_id = producer_by_id.get(data_id)
            if not producer_step_id:
                if data_id in preloaded_ids:
                    # Preloaded task facts/artifacts are valid consumed inputs without runtime producers.
                    continue
                # Orphan: consumed but never produced and not preloaded.
                # Strip it from all consumer steps to prevent runtime failures.
                logger.warning(
                    "[%s] Stripping orphan data id '%s' — consumed but never produced "
                    "and not in preloaded_task_state. Consumer steps: %s",
                    self.name, data_id, consumers,
                )
                for step in steps:
                    step_consumes = step.get("consumes_data_ids")
                    if isinstance(step_consumes, list) and data_id in step_consumes:
                        step_consumes.remove(data_id)
                continue
            producer_idx = step_order[producer_step_id]
            filtered_consumers: list[str] = []
            for consumer_step_id in consumers:
                if consumer_step_id == producer_step_id:
                    # Intra-step consume/produce is internal to one action step.
                    continue
                consumer_idx = step_order[consumer_step_id]
                if producer_idx >= consumer_idx:
                    raise ValueError(
                        f"[{self.name}] data id '{data_id}' is consumed by step '{consumer_step_id}' "
                        f"before it is produced by '{producer_step_id}'."
                    )
                filtered_consumers.append(consumer_step_id)
            if not filtered_consumers:
                continue
            bindings.append(
                {
                    "data_id": data_id,
                    "data_kind": kind_by_id[data_id],
                    "state_path": self._state_path_for_data_id(data_id),
                    "producer_step_id": producer_step_id,
                    "consumer_step_ids": filtered_consumers,
                }
            )

        # Keep deterministic order by producer step then data id.
        bindings.sort(key=lambda item: (step_order[str(item["producer_step_id"])], str(item["data_id"])))
        out["data_bindings"] = bindings
        return out

    def _deterministic_fact_ids_for_step(self, step: dict[str, Any]) -> set[str]:
        if not isinstance(step, dict):
            raise TypeError(f"[{self.name}] step must be dict for deterministic fact extraction.")
        out: set[str] = set()
        step_id = str(step.get("id") or "").strip()

        state_update = step.get("task_state_update")
        if state_update is not None:
            if not isinstance(state_update, dict):
                raise ValueError(f"[{self.name}] step '{step_id}' task_state_update must be object when provided.")
            facts_obj = state_update.get("facts")
            if facts_obj is not None:
                if not isinstance(facts_obj, dict):
                    raise ValueError(f"[{self.name}] step '{step_id}' task_state_update.facts must be object.")
                for fact_key_raw in facts_obj.keys():
                    fact_key = str(fact_key_raw or "").strip()
                    if not fact_key:
                        raise ValueError(f"[{self.name}] step '{step_id}' task_state_update fact key is empty.")
                    if not fact_key.startswith("fact_"):
                        raise ValueError(
                            f"[{self.name}] step '{step_id}' task_state_update fact key '{fact_key}' must start with 'fact_'."
                        )
                    out.add(fact_key)

        event_bindings = step.get("event_fact_bindings")
        if event_bindings is not None:
            if not isinstance(event_bindings, dict):
                raise ValueError(f"[{self.name}] step '{step_id}' event_fact_bindings must be object when provided.")
            for event_key_raw, event_map in event_bindings.items():
                event_key = str(event_key_raw or "").strip()
                if not event_key:
                    raise ValueError(f"[{self.name}] step '{step_id}' event_fact_bindings event key is empty.")
                if not isinstance(event_map, dict):
                    raise ValueError(
                        f"[{self.name}] step '{step_id}' event_fact_bindings['{event_key}'] must be object."
                    )
                for fact_key_raw in event_map.keys():
                    fact_key = str(fact_key_raw or "").strip()
                    if not fact_key:
                        raise ValueError(
                            f"[{self.name}] step '{step_id}' event_fact_bindings['{event_key}'] fact key is empty."
                        )
                    if not fact_key.startswith("fact_"):
                        raise ValueError(
                            f"[{self.name}] step '{step_id}' event_fact_bindings['{event_key}'] key '{fact_key}' must start with 'fact_'."
                        )
                    out.add(fact_key)
        return out

    def _prune_dead_produced_data_ids(self, steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        preloaded_ids = self._preloaded_data_ids_from_compile_seed()
        consumed_ids: set[str] = set()
        consumer_kinds_by_id: dict[str, set[str]] = {}
        normalized_steps: list[dict[str, Any]] = []

        for raw in steps:
            if not isinstance(raw, dict):
                raise TypeError(f"[{self.name}] step must be dict, got {type(raw)}.")
            step = dict(raw)
            step_kind = str(step.get("kind") or "").strip()
            consumes = self._normalize_data_ids(step.get("consumes_data_ids"), field_name="consumes_data_ids")
            for data_id in consumes:
                consumed_ids.add(data_id)
                if data_id not in consumer_kinds_by_id:
                    consumer_kinds_by_id[data_id] = set()
                if step_kind:
                    consumer_kinds_by_id[data_id].add(step_kind)
            step["consumes_data_ids"] = consumes
            step["produces_data_ids"] = self._normalize_data_ids(
                step.get("produces_data_ids"),
                field_name="produces_data_ids",
            )
            normalized_steps.append(step)

        for step in normalized_steps:
            produces = step.get("produces_data_ids")
            if not isinstance(produces, list):
                raise TypeError(f"[{self.name}] produces_data_ids must be list[str] during pruning.")
            if not produces:
                continue
            step_kind = str(step.get("kind") or "").strip()
            kept: list[str] = []
            removed: list[str] = []
            for data_id in produces:
                if data_id not in consumed_ids:
                    has_explicit_output_contract = (
                        isinstance(step.get("output_schema"), dict)
                        or step.get("gate_type") is not None
                    )
                    if has_explicit_output_contract:
                        kept.append(data_id)
                        continue
                    removed.append(data_id)
                    continue
                if step_kind == "action" and data_id.startswith("fact_"):
                    consumer_kinds = consumer_kinds_by_id.get(data_id, set())
                    # Manager action facts are only meaningful for downstream gates.
                    if not any(kind in {"decision", "wait_for_event", "wait_gate"} for kind in consumer_kinds):
                        removed.append(data_id)
                        continue
                kept.append(data_id)
            if len(kept) != len(produces):
                logger.info(
                    "[%s] Pruned dead produces_data_ids for step=%s removed=%s",
                    self.name,
                    str(step.get("id") or "").strip(),
                    removed,
                )
            step["produces_data_ids"] = kept

        produced_ids: set[str] = set()
        for step in normalized_steps:
            produces = step.get("produces_data_ids")
            if not isinstance(produces, list):
                raise TypeError(f"[{self.name}] produces_data_ids must be list[str] after pruning.")
            for data_id in produces:
                produced_ids.add(data_id)

        for step in normalized_steps:
            consumes = step.get("consumes_data_ids")
            if not isinstance(consumes, list):
                raise TypeError(f"[{self.name}] consumes_data_ids must be list[str] after pruning.")
            # Preserve declared consumes_data_ids even when producer is external.
            # External inputs are resolved through task context/preloaded state, not in-graph producers.
            step["consumes_data_ids"] = consumes

        return normalized_steps

    def _extract_preloaded_task_state(self, *, compile_seed: dict[str, Any], draft: dict[str, Any]) -> dict[str, Any]:
        source_task = str(compile_seed.get("source_task") or "")
        declarations = self._parse_preprovided_declarations(source_task=source_task)

        # Also pick up any formal `inputs:` declared in the spec frontmatter.
        # Markdown-style declarations take precedence; frontmatter fills gaps.
        frontmatter_decls = self._frontmatter_input_declarations(compile_seed=compile_seed)
        for data_id, description in frontmatter_decls.items():
            declarations.setdefault(data_id, description)

        if not declarations:
            declarations = self._infer_external_input_declarations(compile_seed=compile_seed, draft=draft)
            if declarations:
                logger.info(
                    "[%s] Inferred preloaded declarations for external inputs: %s",
                    self.name,
                    sorted(declarations.keys()),
                )
        if not declarations:
            return {"facts": {}, "artifacts": {}, "flags": {}}

        facts: dict[str, Any] = {}
        artifacts: dict[str, Any] = {}
        for data_id, description in declarations.items():
            if data_id.startswith("fact_"):
                facts[data_id] = self._coerce_preprovided_fact_value(description=description)
                continue
            if data_id.startswith("artifact_"):
                artifacts[data_id] = self._coerce_preprovided_artifact_value(description=description)
                continue
            raise ValueError(f"[{self.name}] Unsupported preprovided data id '{data_id}'.")
        return {"facts": facts, "artifacts": artifacts, "flags": {}}

    def _frontmatter_input_declarations(self, *, compile_seed: dict[str, Any]) -> dict[str, str]:
        """Read frontmatter.inputs from the task_file and return {data_id: description}.

        Returns empty dict if task_file is missing or has no inputs. Skips entries
        without a fact_*/artifact_* id (purely descriptive inputs are a spec
        convention, not runtime seeds).
        """
        task_file = str(compile_seed.get("task_file") or "").strip()
        if not task_file:
            return {}
        spec_path = resolve_repo_path(task_file)
        if not spec_path.exists():
            return {}
        raw = spec_path.read_text(encoding="utf-8")
        lines = raw.splitlines()
        if not lines or lines[0].strip() != "---":
            return {}
        end = None
        for idx in range(1, len(lines)):
            if lines[idx].strip() == "---":
                end = idx
                break
        if end is None:
            return {}
        frontmatter = yaml.safe_load("\n".join(lines[1:end])) or {}
        if not isinstance(frontmatter, dict):
            return {}
        return self._declarations_from_frontmatter_dict(frontmatter)

    @staticmethod
    def _declarations_from_frontmatter_dict(frontmatter: dict[str, Any]) -> dict[str, str]:
        """Extract {fact_*/artifact_*: description} from a parsed frontmatter dict."""
        inputs = frontmatter.get("inputs")
        if not isinstance(inputs, list):
            return {}
        out: dict[str, str] = {}
        for entry in inputs:
            if not isinstance(entry, dict):
                continue
            data_id = str(entry.get("id") or "").strip()
            if not (data_id.startswith("fact_") or data_id.startswith("artifact_")):
                continue
            description = str(entry.get("description") or "").strip() or data_id
            out[data_id] = description
        return out

    @staticmethod
    def _seed_preloaded_from_frontmatter(*, compiled_task: dict[str, Any], frontmatter: dict[str, Any]) -> None:
        """Seed ``compiled_task.preloaded_task_state`` from spec frontmatter in place.

        Test-friendly helper that takes both inputs as plain dicts so it can be
        exercised without instantiating the node or reading disk. It is the
        authoritative path for the runtime pipeline — ``_frontmatter_input_declarations``
        funnels into this same logic via ``_declarations_from_frontmatter_dict``.
        """
        declarations = TaskCompilePostNode._declarations_from_frontmatter_dict(frontmatter)
        if not declarations:
            return
        state = compiled_task.setdefault("preloaded_task_state", {})
        facts = state.setdefault("facts", {})
        artifacts = state.setdefault("artifacts", {})
        state.setdefault("flags", {})
        for data_id, description in declarations.items():
            if data_id.startswith("fact_"):
                facts.setdefault(data_id, description)
            elif data_id.startswith("artifact_"):
                artifacts.setdefault(data_id, f"include `{description}`")

    def _infer_external_input_declarations(self, *, compile_seed: dict[str, Any], draft: dict[str, Any]) -> dict[str, str]:
        steps = draft.get("steps")
        if not isinstance(steps, list):
            raise ValueError(f"[{self.name}] compiled_task.steps must be list for preloaded input inference.")

        consumed_ids: set[str] = set()
        produced_ids: set[str] = set()
        for raw_step in steps:
            if not isinstance(raw_step, dict):
                raise ValueError(f"[{self.name}] compiled_task step must be object for preloaded input inference.")
            consumes = self._normalize_data_ids(raw_step.get("consumes_data_ids"), field_name="consumes_data_ids")
            produces = self._normalize_data_ids(raw_step.get("produces_data_ids"), field_name="produces_data_ids")
            consumed_ids.update(consumes)
            produced_ids.update(produces)

        external_ids = [data_id for data_id in sorted(consumed_ids) if data_id not in produced_ids]
        if not external_ids:
            return {}

        source_task = str(compile_seed.get("source_task") or "")
        known_input_lines = self._parse_known_inputs_lines(source_task=source_task)
        includes = self._task_spec_includes_from_compile_seed(compile_seed=compile_seed)

        fact_ids = sorted([data_id for data_id in external_ids if data_id.startswith("fact_")], key=self._data_id_sort_key)
        artifact_ids = sorted(
            [data_id for data_id in external_ids if data_id.startswith("artifact_")],
            key=self._data_id_sort_key,
        )
        unsupported_ids = [data_id for data_id in external_ids if not (data_id.startswith("fact_") or data_id.startswith("artifact_"))]
        if unsupported_ids:
            raise ValueError(
                f"[{self.name}] unsupported external consumed data ids for preload inference: {unsupported_ids}"
            )

        fact_values = self._extract_fact_values_from_known_inputs(known_input_lines=known_input_lines)
        artifact_values = self._extract_artifact_values(known_input_lines=known_input_lines, includes=includes)

        if fact_ids and len(fact_values) < len(fact_ids):
            # External facts with no spec declaration are implicit context (e.g. known email
            # addresses, account ids). Log and skip rather than hard-failing — only compile-ladder
            # specs formally pre-declare inputs and user-created specs often don't.
            logger.info(
                "[%s] External fact inputs %s have no spec declaration; treating as implicit context.",
                self.name,
                fact_ids,
            )
            fact_ids = fact_ids[:len(fact_values)]
        if artifact_ids and len(artifact_values) < len(artifact_ids):
            logger.info(
                "[%s] External artifact inputs %s have no spec declaration; treating as implicit context.",
                self.name,
                artifact_ids,
            )
            artifact_ids = artifact_ids[:len(artifact_values)]

        out: dict[str, str] = {}
        for idx, data_id in enumerate(fact_ids):
            out[data_id] = f"`{fact_values[idx]}`"
        for idx, data_id in enumerate(artifact_ids):
            out[data_id] = f"include `{artifact_values[idx]}`"
        return out

    def _extract_task_llm_params(self, *, compile_seed: dict[str, Any]) -> dict[str, Any] | None:
        raw = compile_seed.get("task_llm_params")
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise ValueError(f"[{self.name}] compile_seed.task_llm_params must be an object when provided.")
        out: dict[str, Any] = {}
        engine = raw.get("engine")
        if engine is not None:
            if not isinstance(engine, str) or not engine.strip():
                raise ValueError(f"[{self.name}] compile_seed.task_llm_params.engine must be non-empty string.")
            out["engine"] = engine.strip()
        temperature = raw.get("temperature")
        if temperature is not None:
            if not isinstance(temperature, (int, float)):
                raise ValueError(f"[{self.name}] compile_seed.task_llm_params.temperature must be numeric.")
            out["temperature"] = float(temperature)
        timeout = raw.get("timeout")
        if timeout is not None:
            if not isinstance(timeout, (int, float)):
                raise ValueError(f"[{self.name}] compile_seed.task_llm_params.timeout must be numeric.")
            out["timeout"] = float(timeout)
        return out if out else None

    def _preloaded_data_ids_from_compile_seed(self) -> set[str]:
        compile_seed = self.blackboard.get_state_value("compile_seed", None)
        if not isinstance(compile_seed, dict):
            raise TypeError(f"[{self.name}] compile_seed must be dict, got {type(compile_seed)}.")
        source_task = str(compile_seed.get("source_task") or "")
        declarations = self._parse_preprovided_declarations(source_task=source_task)
        ids = set(declarations.keys())
        # Also honor formal frontmatter `inputs:` declarations so the orphan-strip
        # agrees with _extract_preloaded_task_state on what counts as preloaded.
        ids.update(self._frontmatter_input_declarations(compile_seed=compile_seed).keys())
        return ids

    def _parse_preprovided_declarations(self, *, source_task: str) -> dict[str, str]:
        lines = str(source_task or "").splitlines()
        in_section = False
        out: dict[str, str] = {}
        bullet_pattern = re.compile(r"^\s*-\s*(fact_\d+|artifact_\d+)\s*:\s*(.+?)\s*$")
        section_headers = {"already provided facts and artifacts", "known inputs", "known inputs:"}
        for raw_line in lines:
            line = str(raw_line or "")
            normalized = line.strip().lower()
            # Strip leading markdown heading markers (##, #) before comparing to section headers
            normalized_heading = normalized.lstrip("#").strip()
            if not in_section:
                if any(normalized_heading.startswith(h) for h in section_headers):
                    in_section = True
                continue
            if normalized.startswith("steps:") or normalized.startswith("## ") or normalized == "steps":
                break
            if normalized == "":
                continue
            match = bullet_pattern.match(line)
            if not match:
                # Free-text bullet (e.g. "- Recipient email: foo@bar.com") — not a formal fact_N declaration.
                # Phase I is responsible for treating these as pre-provided context.
                logger.debug("[%s] Skipping free-text known-input line (no fact_N id): %r", self.name, line)
                continue
            data_id = match.group(1).strip()
            description = match.group(2).strip()
            if data_id in out:
                raise ValueError(f"[{self.name}] Duplicate preprovided declaration for '{data_id}'.")
            out[data_id] = description
        return out

    def _parse_known_inputs_lines(self, *, source_task: str) -> list[str]:
        lines = str(source_task or "").splitlines()
        in_section = False
        out: list[str] = []
        section_headers = {"known inputs:", "known inputs", "already provided facts and artifacts"}
        for raw_line in lines:
            line = str(raw_line or "")
            normalized = line.strip().lower()
            normalized_heading = normalized.lstrip("#").strip()
            if not in_section:
                if any(normalized_heading.startswith(h) for h in section_headers):
                    in_section = True
                continue
            if normalized.startswith("steps:") or normalized.startswith("## ") or normalized == "steps":
                break
            if not normalized:
                continue
            if line.lstrip().startswith("-"):
                out.append(line.strip())
        return out

    def _task_spec_includes_from_compile_seed(self, *, compile_seed: dict[str, Any]) -> list[str]:
        task_file = str(compile_seed.get("task_file") or "").strip()
        if not task_file:
            return []
        spec_path = resolve_repo_path(task_file)
        if not spec_path.exists():
            raise FileNotFoundError(f"[{self.name}] task_file not found for include inference: {task_file}")
        raw = spec_path.read_text(encoding="utf-8")
        lines = raw.splitlines()
        if not lines or lines[0].strip() != "---":
            return []
        end = None
        for idx in range(1, len(lines)):
            if lines[idx].strip() == "---":
                end = idx
                break
        if end is None:
            raise ValueError(f"[{self.name}] task_file frontmatter is not properly closed: {task_file}")
        frontmatter = yaml.safe_load("\n".join(lines[1:end])) or {}
        if not isinstance(frontmatter, dict):
            raise ValueError(f"[{self.name}] task_file frontmatter must be object: {task_file}")
        includes = frontmatter.get("includes")
        if includes is None:
            return []
        if not isinstance(includes, list):
            raise ValueError(f"[{self.name}] frontmatter.includes must be list when provided: {task_file}")
        out: list[str] = []
        for item in includes:
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"[{self.name}] frontmatter.includes entries must be non-empty strings: {task_file}")
            out.append(item.strip())
        return out

    @staticmethod
    def _extract_fact_values_from_known_inputs(*, known_input_lines: list[str]) -> list[str]:
        out: list[str] = []
        for line in known_input_lines:
            lowered = line.lower()
            if "included context files" in lowered:
                continue
            match = re.findall(r"`([^`]+)`", line)
            if not match:
                continue
            for value in match:
                candidate = str(value or "").strip()
                if candidate:
                    out.append(candidate)
        dedup: list[str] = []
        seen: set[str] = set()
        for value in out:
            if value in seen:
                continue
            seen.add(value)
            dedup.append(value)
        return dedup

    @staticmethod
    def _extract_artifact_values(*, known_input_lines: list[str], includes: list[str]) -> list[str]:
        out: list[str] = []
        for value in includes:
            candidate = str(value or "").strip()
            if candidate:
                out.append(candidate)
        for line in known_input_lines:
            if "included context files" not in line.lower():
                continue
            for value in re.findall(r"`([^`]+)`", line):
                candidate = str(value or "").strip()
                if candidate:
                    out.append(candidate)
        dedup: list[str] = []
        seen: set[str] = set()
        for value in out:
            if value in seen:
                continue
            seen.add(value)
            dedup.append(value)
        return dedup

    @staticmethod
    def _data_id_sort_key(data_id: str) -> tuple[int, str]:
        match = re.search(r"_(\d+)$", str(data_id or ""))
        if match:
            return (int(match.group(1)), str(data_id))
        return (10_000_000, str(data_id))

    @staticmethod
    def _first_backtick_value(text: str) -> str:
        match = re.search(r"`([^`]+)`", str(text or ""))
        if not match:
            return ""
        return str(match.group(1) or "").strip()

    def _coerce_preprovided_fact_value(self, *, description: str) -> Any:
        inline = self._first_backtick_value(description)
        if inline:
            return inline
        value = str(description or "").strip()
        if not value:
            raise ValueError(f"[{self.name}] Preprovided fact description is empty.")
        return value

    def _coerce_preprovided_artifact_value(self, *, description: str) -> Any:
        include_match = re.search(r"include\s+`([^`]+)`", str(description or ""), flags=re.IGNORECASE)
        if include_match:
            include_path = str(include_match.group(1) or "").strip()
            if not include_path:
                raise ValueError(f"[{self.name}] Preprovided artifact include path is empty.")
            include_file = resolve_repo_path(include_path)
            if not include_file.exists():
                raise FileNotFoundError(f"[{self.name}] Preprovided artifact include file not found: {include_path}")
            return {
                "__resource_ref__": {
                    "kind": "repo_file",
                    "path": include_path,
                }
            }
        inline = self._first_backtick_value(description)
        if inline:
            return inline
        value = str(description or "").strip()
        if not value:
            raise ValueError(f"[{self.name}] Preprovided artifact description is empty.")
        return value

    def _normalize_steps_by_kind(self, steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for step in steps:
            if not isinstance(step, dict):
                raise TypeError(f"[{self.name}] normalized step must be dict, got {type(step)}.")
            kind = step.get("kind")
            if not isinstance(kind, str) or not kind.strip():
                raise ValueError(f"[{self.name}] step.kind is required for normalization.")
            kind = kind.strip()

            cleaned = dict(step)
            if kind == "action":
                self._drop_keys(
                    cleaned,
                    [
                        "event_name",
                        "subscriptions",
                        "release_condition",
                        "watch_registration",
                        "watch_registrations",
                        "clear_condition",
                        "condition",
                        "on_true",
                        "on_false",
                    ],
                )
                consumes = self._normalize_data_ids(cleaned.get("consumes_data_ids"), field_name="consumes_data_ids")
                produces = self._normalize_data_ids(cleaned.get("produces_data_ids"), field_name="produces_data_ids")
                artifact_outputs = [data_id for data_id in produces if data_id.startswith("artifact_")]
                if len(artifact_outputs) > 1:
                    # Keep only the first artifact; extras are over-declarations by the LLM.
                    # Dead-produces pruning will remove it too if nothing consumes it.
                    logger.warning(
                        "[%s] action step '%s' declared %d artifact_* ids; truncating to first: %s",
                        self.name,
                        str(cleaned.get("id") or "").strip(),
                        len(artifact_outputs),
                        artifact_outputs,
                    )
                    keep = artifact_outputs[0]
                    produces = [d for d in produces if not d.startswith("artifact_") or d == keep]
                cleaned["consumes_data_ids"] = consumes
                cleaned["produces_data_ids"] = produces
            elif kind == "tool_sequence":
                self._drop_keys(
                    cleaned,
                    [
                        "executor",
                        "instruction",
                        "event_name",
                        "subscriptions",
                        "release_condition",
                        "watch_registration",
                        "watch_registrations",
                        "clear_condition",
                        "condition",
                        "on_true",
                        "on_false",
                    ],
                )
                consumes = self._normalize_data_ids(cleaned.get("consumes_data_ids"), field_name="consumes_data_ids")
                produces = self._normalize_data_ids(cleaned.get("produces_data_ids"), field_name="produces_data_ids")
                cleaned["consumes_data_ids"] = consumes
                cleaned["produces_data_ids"] = produces
                # Validate tools list
                tools = cleaned.get("tools")
                if not isinstance(tools, list) or not tools:
                    raise ValueError(f"[{self.name}] tool_sequence step '{cleaned.get('id')}' requires non-empty 'tools' list.")
                for t_idx, t in enumerate(tools):
                    if not isinstance(t, dict) or not str(t.get("tool") or "").strip():
                        raise ValueError(f"[{self.name}] tool_sequence step '{cleaned.get('id')}' tools[{t_idx}] missing 'tool' name.")
            elif kind == "wait_for_event":
                self._drop_keys(
                    cleaned,
                    [
                        "executor",
                        "instruction",
                        "subscriptions",
                        "release_condition",
                        "watch_registrations",
                        "condition",
                        "on_true",
                        "on_false",
                    ],
                )
                self._normalize_wait_clear_condition(cleaned)
            elif kind == "wait_gate":
                self._drop_keys(
                    cleaned,
                    [
                        "executor",
                        "instruction",
                        "event_name",
                        "watch_registration",
                        "clear_condition",
                        "condition",
                        "on_true",
                        "on_false",
                    ],
                )
                subscriptions = cleaned.get("subscriptions")
                if not isinstance(subscriptions, list):
                    raise ValueError(f"[{self.name}] wait_gate.subscriptions must be list[str].")
                cleaned["subscriptions"] = self._normalize_data_ids(
                    subscriptions,
                    field_name="subscriptions",
                )
                if not cleaned["subscriptions"]:
                    raise ValueError(f"[{self.name}] wait_gate.subscriptions must be non-empty.")
                release_condition = self._normalize_condition_expression(str(cleaned.get("release_condition") or "").strip())
                release_condition = self._rewrite_fact_event_observed_flags(release_condition)
                release_condition = self._rewrite_event_id_boolean_predicates(release_condition)
                release_condition = self._rewrite_event_observed_atom_ids(release_condition)
                release_condition = self._rewrite_wait_event_condition_to_observed_facts(
                    expression=release_condition,
                    subscriptions=cleaned["subscriptions"],
                )
                if not release_condition:
                    raise ValueError(f"[{self.name}] wait_gate.release_condition is required.")
                try:
                    ast.parse(release_condition, mode="eval")
                except SyntaxError as e:
                    raise ValueError(f"[{self.name}] wait_gate.release_condition must be parseable.") from e
                self._validate_condition_expression_scope(release_condition)
                cleaned["release_condition"] = release_condition
            elif kind == "decision":
                self._drop_keys(
                    cleaned,
                    [
                        "executor",
                        "instruction",
                        "event_name",
                        "subscriptions",
                        "release_condition",
                        "watch_registration",
                        "watch_registrations",
                        "clear_condition",
                        "next_step",
                    ],
                )
                raw_condition = str(cleaned.get("condition") or "").strip()
                if not raw_condition:
                    raise ValueError(f"[{self.name}] decision.condition is required.")
                normalized_condition = self._normalize_condition_expression(raw_condition)
                normalized_condition = self._rewrite_fact_event_observed_flags(normalized_condition)
                normalized_condition = self._rewrite_event_id_boolean_predicates(normalized_condition)
                normalized_condition = self._rewrite_event_observed_atom_ids(normalized_condition)
                normalized_condition = self._normalize_event_observed_presence_predicates(normalized_condition)
                try:
                    ast.parse(normalized_condition, mode="eval")
                except SyntaxError as e:
                    raise ValueError(f"[{self.name}] decision.condition must be parseable.") from e
                self._validate_condition_expression_scope(normalized_condition)
                cleaned["condition"] = normalized_condition
            elif kind == "end":
                self._drop_keys(
                    cleaned,
                    [
                        "executor",
                        "instruction",
                        "event_name",
                        "subscriptions",
                        "release_condition",
                        "watch_registration",
                        "watch_registrations",
                        "clear_condition",
                        "condition",
                        "on_true",
                        "on_false",
                        "next_step",
                        "consumes_data_ids",
                        "produces_data_ids",
                    ],
                )
            else:
                raise ValueError(f"[{self.name}] unsupported step.kind during normalization: {kind!r}")

            normalized.append(cleaned)
        return normalized

    def _normalize_wait_clear_condition(self, step: dict[str, Any]) -> None:
        raw = step.get("clear_condition", "")
        if raw is None:
            step["clear_condition"] = ""
            return
        if not isinstance(raw, str):
            raise TypeError(f"[{self.name}] wait_for_event.clear_condition must be str when provided.")
        value = self._normalize_condition_expression(raw.strip())
        value = self._rewrite_fact_event_observed_flags(value)
        value = self._rewrite_event_id_boolean_predicates(value)
        value = self._rewrite_event_observed_atom_ids(value)
        value = self._normalize_event_observed_presence_predicates(value)
        if not value:
            step["clear_condition"] = ""
            return
        try:
            ast.parse(value, mode="eval")
        except SyntaxError:
            # Deterministic canonicalization: prose clear conditions are invalid at runtime,
            # so fall back to pure wait semantics (wake event delivery clears the gate).
            step["clear_condition"] = ""
            return
        self._validate_condition_expression_scope(value)
        step["clear_condition"] = value

    @staticmethod
    def _normalize_condition_expression(expression: str) -> str:
        value = str(expression or "").strip()
        if not value:
            return ""
        # Canonicalize boolean operators to Python expression syntax.
        value = re.sub(r"\bAND\b", "and", value, flags=re.IGNORECASE)
        value = re.sub(r"\bOR\b", "or", value, flags=re.IGNORECASE)
        value = re.sub(r"\bNOT\b", "not", value, flags=re.IGNORECASE)
        # Canonicalize legacy/local aliases to runtime-supported task_state facts.
        value = re.sub(r"\blocal_time\.hh_mm\b", "task_state.facts.time_local_hhmm", value)
        value = re.sub(r"\blocal_time\b", "task_state.facts.time_local_hhmm", value)
        # Rewrite bare fact_* and artifact_* names to their full task_state paths so
        # the runtime condition evaluator can resolve them.
        # Only rewrites standalone names — not those already inside task_state.facts.* paths.
        value = re.sub(r"(?<!\.)(\bfact_\w+\b)", r"task_state.facts.\1", value)
        value = re.sub(r"(?<!\.)(\bartifact_\w+\b)", r"task_state.artifacts.\1", value)
        return value

    @staticmethod
    def _rewrite_wait_event_condition_to_observed_facts(*, expression: str, subscriptions: list[str]) -> str:
        value = str(expression or "").strip()
        if not value:
            return ""
        if not isinstance(subscriptions, list):
            raise TypeError("wait_gate subscriptions must be list[str] for condition normalization.")
        allowed_events = {str(item or "").strip() for item in subscriptions if str(item or "").strip()}
        if not allowed_events:
            raise ValueError("wait_gate subscriptions must contain at least one non-empty event name.")

        def _replace_event_match(match: re.Match[str]) -> str:
            event_name = str(match.group(1) or "").strip()
            if event_name not in allowed_events:
                raise ValueError(
                    "wait_gate release_condition references event_name "
                    f"'{event_name}' that is not present in subscriptions {sorted(allowed_events)}."
                )
            return f'"{event_name}" in task_state.facts.events_observed'

        patterns = ()
        for pattern in patterns:
            value = re.sub(pattern, _replace_event_match, value)
        return value

    def _rewrite_event_id_boolean_predicates(self, expression: str) -> str:
        value = str(expression or "").strip()
        if not value:
            return ""
        event_name_by_atom_id = self._phase_i_event_name_by_atom_id()
        if not event_name_by_atom_id:
            return value

        for atom_id, event_name in event_name_by_atom_id.items():
            atom_pattern = re.escape(atom_id)
            event_literal = f'"{event_name}"'
            value = re.sub(
                rf"\b{atom_pattern}\b\s*==\s*True",
                f"{event_literal} in task_state.facts.events_observed",
                value,
            )
            value = re.sub(
                rf"\b{atom_pattern}\b\s*==\s*False",
                f"{event_literal} not in task_state.facts.events_observed",
                value,
            )
        return value

    def _rewrite_event_observed_atom_ids(self, expression: str) -> str:
        value = str(expression or "").strip()
        if not value:
            return ""
        event_name_by_atom_id = self._phase_i_event_name_by_atom_id()
        if not event_name_by_atom_id:
            return value

        def _replace_dot(match: re.Match[str]) -> str:
            atom_id = str(match.group(1) or "").strip()
            event_name = event_name_by_atom_id.get(atom_id)
            if not event_name:
                return match.group(0)
            return f'task_state.facts.events_observed["{event_name}"]'

        def _replace_subscript(match: re.Match[str]) -> str:
            atom_id = str(match.group(1) or "").strip()
            event_name = event_name_by_atom_id.get(atom_id)
            if not event_name:
                return match.group(0)
            return f'task_state.facts.events_observed["{event_name}"]'

        value = re.sub(r"\btask_state\.facts\.events_observed\.(event_[A-Za-z0-9_]+)\b", _replace_dot, value)
        value = re.sub(
            r"\btask_state\.facts\.events_observed\[['\"](event_[A-Za-z0-9_]+)['\"]\]",
            _replace_subscript,
            value,
        )
        return value

    def _phase_i_event_name_by_atom_id(self) -> dict[str, str]:
        phase_i_atoms = self.blackboard.get_state_value("phase_i_atoms", None)
        if not isinstance(phase_i_atoms, dict):
            return {}
        events = phase_i_atoms.get("events")
        if not isinstance(events, list):
            return {}
        out: dict[str, str] = {}
        for item in events:
            if not isinstance(item, dict):
                continue
            atom_id = str(item.get("id") or "").strip()
            canonical = str(item.get("canonical_event_name") or "").strip()
            if atom_id and canonical:
                out[atom_id] = canonical
        return out

    def _rewrite_fact_event_observed_flags(self, expression: str) -> str:
        value = str(expression or "").strip()
        if not value:
            return ""
        event_name_by_atom_id = self._phase_i_event_name_by_atom_id()
        if not event_name_by_atom_id:
            return value

        def _atom_to_event_name(atom_id: str) -> str | None:
            atom_id_norm = str(atom_id or "").strip()
            if not atom_id_norm:
                return None
            if atom_id_norm.endswith("_observed"):
                atom_id_norm = atom_id_norm[: -len("_observed")]
            return event_name_by_atom_id.get(atom_id_norm)

        def _replace_eq_true(match: re.Match[str]) -> str:
            atom_id = str(match.group(1) or "").strip()
            event_name = _atom_to_event_name(atom_id)
            if not event_name:
                return match.group(0)
            return f'"{event_name}" in task_state.facts.events_observed'

        def _replace_eq_false(match: re.Match[str]) -> str:
            atom_id = str(match.group(1) or "").strip()
            event_name = _atom_to_event_name(atom_id)
            if not event_name:
                return match.group(0)
            return f'"{event_name}" not in task_state.facts.events_observed'

        value = re.sub(r"\btask_state\.facts\.(event_[A-Za-z0-9_]+_observed)\s*==\s*True", _replace_eq_true, value)
        value = re.sub(r"\btask_state\.facts\.(event_[A-Za-z0-9_]+_observed)\s*==\s*False", _replace_eq_false, value)
        return value

    def _normalize_wait_gate_release_conditions(self, compiled_task: dict[str, Any]) -> dict[str, Any]:
        out = dict(compiled_task)
        steps = out.get("steps")
        if not isinstance(steps, list):
            raise TypeError(f"[{self.name}] compiled_task.steps must be list for wait gate normalization.")
        normalized_steps: list[dict[str, Any]] = []
        for raw_step in steps:
            if not isinstance(raw_step, dict):
                raise TypeError(f"[{self.name}] compiled_task step must be dict, got {type(raw_step)}.")
            step = dict(raw_step)
            if str(step.get("kind") or "").strip() != "wait_gate":
                normalized_steps.append(step)
                continue
            subscriptions = step.get("subscriptions")
            if not isinstance(subscriptions, list):
                raise ValueError(f"[{self.name}] wait_gate.subscriptions must be list[str].")
            release_condition = str(step.get("release_condition") or "").strip()
            if not release_condition:
                raise ValueError(f"[{self.name}] wait_gate.release_condition is required.")
            release_condition = self._rewrite_event_id_boolean_predicates(release_condition)
            release_condition = self._normalize_event_observed_presence_predicates(release_condition)
            self._validate_condition_expression_scope(release_condition)
            step["release_condition"] = release_condition
            normalized_steps.append(step)
        out["steps"] = normalized_steps
        return out

    @staticmethod
    def _normalize_event_observed_presence_predicates(expression: str) -> str:
        value = str(expression or "").strip()
        if not value:
            return ""
        patterns = (
            (
                r'task_state\.facts\.events_observed\["([^"]+)"\]\s*==\s*True',
                lambda m: f'"{m.group(1)}" in task_state.facts.events_observed',
            ),
            (
                r"task_state\.facts\.events_observed\['([^']+)'\]\s*==\s*True",
                lambda m: f'"{m.group(1)}" in task_state.facts.events_observed',
            ),
            (
                r'task_state\.facts\.events_observed\["([^"]+)"\]\s*==\s*False',
                lambda m: f'"{m.group(1)}" not in task_state.facts.events_observed',
            ),
            (
                r"task_state\.facts\.events_observed\['([^']+)'\]\s*==\s*False",
                lambda m: f'"{m.group(1)}" not in task_state.facts.events_observed',
            ),
        )
        for pattern, repl in patterns:
            value = re.sub(pattern, repl, value)
        return value

    def _validate_condition_expression_scope(self, expression: str) -> None:
        value = str(expression or "").strip()
        if not value:
            return
        try:
            parsed = ast.parse(value, mode="eval")
        except SyntaxError as e:
            raise ValueError(f"[{self.name}] condition expression must be parseable.") from e
        for node in ast.walk(parsed):
            if isinstance(node, ast.Name):
                name = node.id
                # Allow short-form deterministic ids from the compile contract:
                # fact_*, artifact_*, event_*, condition_*, True, False
                if name in {"True", "False", "None"}:
                    continue
                if (
                    name.startswith("fact_")
                    or name.startswith("artifact_")
                    or name.startswith("event_")
                    or name.startswith("condition_")
                    or name == "task_state"
                ):
                    continue
                raise ValueError(
                    f"[{self.name}] condition expression uses unsupported name '{name}'. "
                    "Use fact_*, artifact_*, event_* ids or task_state.facts.* / task_state.artifacts.* paths."
                )
            if isinstance(node, ast.Attribute):
                dotted = self._attribute_to_dotted_path(node)
                if dotted is None:
                    continue
                if dotted == "task_state":
                    continue
                if dotted in {"task_state.facts", "task_state.artifacts"}:
                    continue
                if dotted.startswith("task_state.facts."):
                    remainder = dotted[len("task_state.facts."):]
                    top_key = str(remainder.split(".", 1)[0] or "").strip()
                    intrinsic_fact_keys = {"events_observed", "time_local_hhmm", "time_utc_iso"}
                    if top_key.startswith("fact_") or top_key in intrinsic_fact_keys:
                        continue
                    raise ValueError(
                        f"[{self.name}] unsupported task_state.facts key '{top_key}' in condition path '{dotted}'. "
                        "Condition facts must be fact_* or intrinsic fact keys."
                    )
                if dotted.startswith("task_state.artifacts."):
                    remainder = dotted[len("task_state.artifacts."):]
                    top_key = str(remainder.split(".", 1)[0] or "").strip()
                    if top_key.startswith("artifact_"):
                        continue
                    raise ValueError(
                        f"[{self.name}] unsupported task_state.artifacts key '{top_key}' in condition path '{dotted}'. "
                        "Condition artifacts must use artifact_* ids."
                    )
                raise ValueError(
                    f"[{self.name}] unsupported condition path '{dotted}'. "
                    "Only task_state.facts.* and task_state.artifacts.* are allowed."
                )

    @staticmethod
    def _attribute_to_dotted_path(node: ast.Attribute) -> str | None:
        parts: list[str] = [str(node.attr)]
        current: Any = node.value
        while isinstance(current, ast.Attribute):
            parts.append(str(current.attr))
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(str(current.id))
            return ".".join(reversed(parts))
        return None

    @staticmethod
    def _drop_keys(target: dict[str, Any], keys: list[str]) -> None:
        for key in keys:
            if key in target:
                del target[key]

    @staticmethod
    def _merge_str_list(left: Any, right: Any) -> list[str]:
        merged: list[str] = []
        seen: set[str] = set()
        for raw in (left, right):
            if raw is None:
                continue
            if not isinstance(raw, list):
                raise TypeError("consumes_data_ids/produces_data_ids must be list[str] when provided.")
            for item in raw:
                if not isinstance(item, str):
                    raise TypeError("consumes_data_ids/produces_data_ids entries must be str.")
                value = item.strip()
                if not value or value in seen:
                    continue
                seen.add(value)
                merged.append(value)
        return merged

    @staticmethod
    def _normalize_data_ids(raw: Any, *, field_name: str) -> list[str]:
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise TypeError(f"{field_name} must be list[str] when provided.")
        out: list[str] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, str):
                raise TypeError(f"{field_name} entries must be str.")
            value = item.strip()
            if not value or value in seen:
                continue
            seen.add(value)
            out.append(value)
        return out

    @staticmethod
    def _data_kind_for_id(data_id: str) -> str:
        if data_id.startswith("fact_"):
            return "fact"
        if data_id.startswith("artifact_"):
            return "artifact"
        if data_id.startswith("event_"):
            return "event"
        if data_id.startswith("condition_"):
            return "condition"
        raise ValueError(f"Unsupported data id prefix for '{data_id}'. Expected fact_*, artifact_*, event_*, or condition_*.")

    @staticmethod
    def _state_path_for_data_id(data_id: str) -> str:
        if data_id.startswith("fact_"):
            return f"task_state.facts.{data_id}"
        if data_id.startswith("artifact_"):
            return f"task_state.artifacts.{data_id}"
        if data_id.startswith("event_"):
            return f"task_state.facts.events_observed.{data_id}"
        if data_id.startswith("condition_"):
            return f"task_state.facts.{data_id}"
        raise ValueError(f"Unsupported data id prefix for '{data_id}'. Expected fact_*, artifact_*, event_*, or condition_*.")

    def _cfg(self) -> dict:
        return self._node_cfg()

    @staticmethod
    def _required_previous_agents(cfg: dict) -> set[str]:
        raw_single = cfg.get("previous_agent")
        raw_multi = cfg.get("previous_agents")
        out: set[str] = set()

        if isinstance(raw_single, str) and raw_single.strip():
            out.add(raw_single.strip())
        elif raw_single is not None and not isinstance(raw_single, str):
            raise TypeError("task compile post node 'previous_agent' must be str when provided.")

        if raw_multi is not None:
            if not isinstance(raw_multi, list):
                raise TypeError("task compile post node 'previous_agents' must be list[str] when provided.")
            for item in raw_multi:
                if not isinstance(item, str):
                    raise TypeError("task compile post node 'previous_agents' entries must be str.")
                value = item.strip()
                if value:
                    out.add(value)

        if not out:
            raise ValueError("task compile post node requires non-empty 'previous_agent' or 'previous_agents'.")
        return out
