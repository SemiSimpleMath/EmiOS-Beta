from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.assistant.utils.identity_names import get_required_assistant_name
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root

logger = get_logger(__name__)

_TASKS_ROOT = "tasks"


class TaskCreateCompileRunner:
    """
    Synchronous compile runner called from a background thread.

    1. Creates tasks/<task_id>/ folder
    2. Writes the spec markdown to tasks/<task_id>/task_spec.md
    3. Invokes the task_compile_manager synchronously
    4. Emits task_compiled (or task_compile_failed) WebSocket event

    All aux files the task references (bios, guidelines, etc.) should live
    alongside the spec in the same tasks/<task_id>/ folder.
    """

    @classmethod
    def run(cls, *, session_id: str, spec_markdown: str, room_id: str, precompile_hints: str | None = None) -> None:
        logger.info("[TaskCreateCompileRunner] Starting compile for session=%s", session_id)
        try:
            # Try to get the structured TaskSpec dict from the session.
            spec_dict = None
            try:
                from app.assistant.ServiceLocator.service_locator import DI
                from app.assistant.room_session_manager.services.task_creation_session_service import TaskCreationSessionService
                svc = TaskCreationSessionService(blackboard=DI.global_blackboard)
                spec_dict = svc.get_task_spec_object(session_id=session_id)
            except Exception:
                pass
            spec_path = cls._write_spec_to_disk(
                session_id=session_id,
                spec_markdown=spec_markdown,
                spec_dict=spec_dict,
                precompile_hints=precompile_hints,
            )
            compiled_task_id = cls._invoke_compile(spec_path=spec_path, session_id=session_id)
            cls._register_task(spec_path=spec_path, task_id=compiled_task_id, spec_markdown=spec_markdown)
            cls._update_session(session_id=session_id, compiled_task_id=compiled_task_id)
            cls._emit_compiled(room_id=room_id, session_id=session_id, compiled_task_id=compiled_task_id)
            logger.info(
                "[TaskCreateCompileRunner] Compile complete session=%s compiled_task_id=%s",
                session_id,
                compiled_task_id,
            )
        except Exception as e:
            logger.error("[TaskCreateCompileRunner] Compile failed for session=%s: %s", session_id, e)
            logger.debug("[TaskCreateCompileRunner] compile exception", exc_info=True)
            cls._emit_compile_failed(room_id=room_id, session_id=session_id, error=str(e))

    @classmethod
    def _write_spec_to_disk(
        cls,
        *,
        session_id: str,
        spec_markdown: str,
        spec_dict: dict | None = None,
        precompile_hints: str | None = None,
    ) -> Path:
        repo_root = Path(get_repo_root())
        task_id = cls._derive_task_id(session_id=session_id, spec_markdown=spec_markdown)

        # Each task lives in its own folder: tasks/<task_id>/
        task_dir = repo_root / _TASKS_ROOT / task_id
        task_dir.mkdir(parents=True, exist_ok=True)

        spec_path = task_dir / "task_spec.md"
        compiled_path = f"{_TASKS_ROOT}/{task_id}/{task_id}.json"

        # Save the TaskSpec as JSON (structured source of truth).
        if isinstance(spec_dict, dict) and spec_dict:
            import json
            spec_json_path = task_dir / "task_spec.json"
            spec_json_path.write_text(
                json.dumps(spec_dict, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            logger.info("[TaskCreateCompileRunner] Saved task_spec.json: %s", spec_json_path)

        # Pre-compile hints live in a sibling file so the spec stays pristine.
        # Rewritten every compile — never accumulates.
        precompile_path = task_dir / "pre-compile.md"
        if precompile_hints and precompile_hints.strip():
            precompile_path.write_text(precompile_hints.strip() + "\n", encoding="utf-8")
            logger.info("[TaskCreateCompileRunner] Wrote pre-compile.md: %s", precompile_path)
        elif precompile_path.exists():
            precompile_path.unlink()

        # task_spec.md stays clean (spec only). Hints live in the sibling pre-compile.md.
        # The compile metadata node reads both and concatenates them for the LLM input.
        if spec_markdown.lstrip().startswith("---"):
            spec_path.write_text(spec_markdown, encoding="utf-8")
        else:
            created_at = datetime.now(timezone.utc).isoformat()
            frontmatter = (
                "---\n"
                f"schema_version: 1\n"
                f"task_id: {task_id}\n"
                f"manager: emi_team_manager\n"
                f"description: Task created via /task create chat flow.\n"
                "inputs: []\n"
                "outputs:\n"
                f"  - id: compiled_output\n"
                f"    path: {compiled_path}\n"
                "    format: json\n"
                "    overwrite: true\n"
                "idempotency: overwrite_outputs\n"
                "limits:\n"
                "  timeout_seconds: 300\n"
                "  max_cycles: 30\n"
                f"created_at_utc: '{created_at}'\n"
                "---\n"
            )
            spec_path.write_text(frontmatter + spec_markdown, encoding="utf-8")
        logger.info(
            "[TaskCreateCompileRunner] Task folder created: %s  spec: %s",
            task_dir,
            spec_path,
        )
        return spec_path

    @staticmethod
    def _derive_task_id(*, session_id: str, spec_markdown: str) -> str:
        """
        Extract task_id from YAML frontmatter, then H1 title, then ## Title section.
        Falls back to the session_id if no usable title is found.
        """
        import re as _re

        # 1. YAML frontmatter task_id (authoritative).
        if spec_markdown and "---" in spec_markdown:
            parts = spec_markdown.split("---", 2)
            if len(parts) >= 3:
                for line in parts[1].splitlines():
                    line = line.strip()
                    if line.lower().startswith("task_id:"):
                        tid = line.split(":", 1)[1].strip().strip("'\"")
                        if tid:
                            return _re.sub(r"[^A-Za-z0-9]+", "_", tid).strip("_").lower()

        # 2. ## Title section content (task spec format).
        in_title = False
        for line in (spec_markdown or "").splitlines():
            stripped = line.strip()
            if stripped.lower() in ("## title", "## title:"):
                in_title = True
                continue
            if in_title and stripped and not stripped.startswith("#"):
                slug = _re.sub(r"[^A-Za-z0-9]+", "_", stripped).strip("_").lower()
                if slug:
                    return slug
            if in_title and stripped.startswith("#"):
                in_title = False

        # 3. First H1 heading.
        for line in (spec_markdown or "").splitlines():
            stripped = line.strip()
            if stripped.startswith("# ") and not stripped.lower().startswith("# goal"):
                title = stripped[2:].strip()
                if title:
                    slug = _re.sub(r"[^A-Za-z0-9]+", "_", title).strip("_").lower()
                    if slug:
                        return slug

        return session_id

    @classmethod
    def _invoke_compile(cls, *, spec_path: Path, session_id: str) -> str:
        from app.assistant.scope.loader import load_scope_for_source
        from app.assistant.ServiceLocator.service_locator import DI
        from app.assistant.utils.pydantic_classes import Message

        repo_root = Path(get_repo_root())
        relative_spec_path = str(spec_path.relative_to(repo_root))
        # task_id is the parent folder name: tasks/<task_id>/task_spec.md
        task_id = spec_path.parent.name

        import json as _json
        information_meta = _json.dumps({
            "task_id": task_id,
            "task_file": relative_spec_path,
            "task_description": f"Task created via chat flow: {task_id.replace('_', ' ')}",
        })

        scope_context = load_scope_for_source(
            kind="subsystem",
            source_id="task",
            actor_id="task_create_compile_runner",
            identity_overrides={
                "owner_id": "system",
                "surface": "internal",
                "scope_id": f"compile::{session_id}",
            },
        )

        logger.info(
            "[TaskCreateCompileRunner] Creating task_compile_manager for session=%s spec=%s",
            session_id,
            relative_spec_path,
        )
        manager = DI.multi_agent_manager_factory.create_manager("task_compile_manager")
        manager_message = Message(
            event_topic="task_request",
            sender="task_create_compile_runner",
            receiver=None,
            content=f"Compile task spec at {relative_spec_path}",
            task=f"Compile the task spec at: {relative_spec_path}",
            information=information_meta,
            request_id=None,
            data={"task_file": relative_spec_path},
            scope_context=scope_context,
        )
        logger.info(
            "[TaskCreateCompileRunner] Invoking task_compile_manager session=%s",
            session_id,
        )
        result = DI.manager_invoker.invoke(manager, manager_message)
        logger.info(
            "[TaskCreateCompileRunner] task_compile_manager returned session=%s result_type=%s",
            session_id,
            type(result).__name__,
        )

        compiled_task_id = cls._extract_compiled_task_id(result=result, session_id=session_id)
        return compiled_task_id

    @classmethod
    def post_process_ir(cls, *, ir: dict, ir_path: "Path") -> None:
        """
        Lightweight post-processing pass for editor-saved IRs.

        Skips the LLM-phase-dependent watch registration injection (requires
        phase_i_atoms/logic_tree which aren't available outside a compile run).
        Only re-materializes data_bindings which are purely graph-structural.
        """
        from datetime import datetime, timezone

        logger.info("[TaskCreateCompileRunner.post_process_ir] Running for %s", ir_path)
        try:
            steps = ir.get("steps")
            if not isinstance(steps, list):
                logger.warning("[post_process_ir] IR has no steps list, skipping")
                return

            data_bindings = _build_data_bindings(steps)
            ir["data_bindings"] = data_bindings
            ir["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
            ir_path.write_text(
                __import__("json").dumps(ir, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            logger.info("[post_process_ir] Re-materialized %d data bindings for %s", len(data_bindings), ir_path)
        except Exception as e:
            logger.error("[post_process_ir] Failed: %s", e)
            logger.debug("[post_process_ir] exception", exc_info=True)
            raise

    @staticmethod
    def _extract_compiled_task_id(*, result: Any, session_id: str) -> str:
        if result is None:
            return session_id
        import json as _json

        # First try: check compile_seed on the blackboard via data_list entries
        content = str(getattr(result, "content", "") or "")
        if content.strip():
            try:
                data = _json.loads(content)
                # direct top-level keys (legacy path)
                task_id = (
                    data.get("task_id")
                    or data.get("compiled_task_id")
                    or (data.get("compile_seed") or {}).get("task_id")
                )
                if isinstance(task_id, str) and task_id.strip():
                    return task_id.strip()

                # final_answer_data_list path: look for compiled_task entry
                data_list = data.get("final_answer_data_list")
                if isinstance(data_list, list):
                    for entry in data_list:
                        if not isinstance(entry, dict):
                            continue
                        if entry.get("key") == "compiled_task" or entry.get("data_type") == "compiled_task":
                            raw_value = entry.get("value")
                            if isinstance(raw_value, str) and raw_value.strip():
                                try:
                                    compiled = _json.loads(raw_value)
                                    task_id = compiled.get("task_id")
                                    if isinstance(task_id, str) and task_id.strip():
                                        return task_id.strip()
                                except (_json.JSONDecodeError, AttributeError):
                                    pass
                            elif isinstance(raw_value, dict):
                                task_id = raw_value.get("task_id")
                                if isinstance(task_id, str) and task_id.strip():
                                    return task_id.strip()
            except (_json.JSONDecodeError, AttributeError):
                pass

        return session_id

    @classmethod
    def _register_task(cls, *, spec_path: Path, task_id: str, spec_markdown: str) -> None:
        """Add or update the task entry in tasks/registry.yaml so it's findable by name."""
        import re as _re

        try:
            repo_root = Path(get_repo_root())
            registry_path = repo_root / _TASKS_ROOT / "registry.yaml"

            # Derive human-readable name from H1 heading.
            name = ""
            for line in (spec_markdown or "").splitlines():
                stripped = line.strip()
                if stripped.startswith("# "):
                    name = stripped[2:].strip()
                    break
            if not name:
                name = task_id.replace("_", " ")

            relative_spec = str(spec_path.relative_to(repo_root)).replace("\\", "/")
            description = f"Task created via chat flow: {name.lower()}"

            # Build aliases: task_id with underscores, name with spaces, "run <name>".
            aliases = list({
                task_id,
                task_id.replace("_", " "),
                name.lower(),
                f"run {name.lower()}",
            })
            aliases.sort()

            # Load existing registry or create new.
            import yaml
            if registry_path.is_file():
                raw = registry_path.read_text(encoding="utf-8")
                data = yaml.safe_load(raw) or {}
            else:
                data = {}
            tasks_list = data.get("tasks") or []
            if not isinstance(tasks_list, list):
                tasks_list = []

            # Update existing entry or append new one.
            found = False
            for entry in tasks_list:
                if not isinstance(entry, dict):
                    continue
                if entry.get("id") == task_id:
                    entry["name"] = name
                    entry["task_file"] = relative_spec
                    entry["description"] = description
                    entry["aliases"] = aliases
                    found = True
                    break

            if not found:
                tasks_list.append({
                    "id": task_id,
                    "name": name,
                    "task_file": relative_spec,
                    "description": description,
                    "aliases": aliases,
                })

            data["tasks"] = tasks_list
            registry_path.write_text(
                yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            logger.info(
                "[TaskCreateCompileRunner] Registered task '%s' (%s) in registry.yaml",
                task_id, name,
            )
        except Exception as e:
            logger.error("[TaskCreateCompileRunner] Failed registering task in registry.yaml: %s", e)
            logger.debug("[TaskCreateCompileRunner] registry update exception", exc_info=True)

    @staticmethod
    def _update_session(*, session_id: str, compiled_task_id: str) -> None:
        try:
            from app.assistant.ServiceLocator.service_locator import DI
            from app.assistant.room_session_manager.services.task_creation_session_service import (
                TaskCreationSessionService,
            )
            bb = getattr(DI, "global_blackboard", None)
            if bb is None:
                logger.error("[TaskCreateCompileRunner] global_blackboard not available; cannot update session compiled_task_id")
                return
            TaskCreationSessionService(blackboard=bb).set_compiled_task_id(
                session_id=session_id,
                compiled_task_id=compiled_task_id,
            )
        except ValueError as e:
            logger.debug("[TaskCreateCompileRunner] session update skipped (no session record): %s", e)
        except Exception as e:
            logger.error("[TaskCreateCompileRunner] Failed updating session compiled_task_id: %s", e)
            logger.debug("[TaskCreateCompileRunner] session update exception", exc_info=True)

    @staticmethod
    def _emit_compiled(*, room_id: str, session_id: str, compiled_task_id: str) -> None:
        if not room_id:
            logger.warning(
                "[TaskCreateCompileRunner] Cannot emit task_compiled: room_id is empty. "
                "session=%s compiled_task_id=%s — UI will not be notified.",
                session_id,
                compiled_task_id,
            )
            return
        try:
            from app.assistant.ServiceLocator.service_locator import DI
            from app.assistant.utils.pydantic_classes import UserMessage, UserMessageData
            assistant_name = get_required_assistant_name()
            msg = UserMessage(
                data_type="user_msg",
                sender=assistant_name,
                receiver=None,
                role="assistant",
                metadata={"reply_to": {"type": "socketio", "room_id": room_id}},
                user_message_data=UserMessageData(
                    widget_data=[
                        {
                            "data_type": "task_compiled",
                            "session_id": session_id,
                            "compiled_task_id": compiled_task_id,
                        }
                    ]
                ),
            )
            msg.event_topic = "socket_emit"
            DI.event_hub.publish(msg)
            logger.debug(
                "[TaskCreateCompileRunner] Emitted task_compiled socket=%s compiled_task_id=%s",
                room_id,
                compiled_task_id,
            )
        except Exception as e:
            logger.error("[TaskCreateCompileRunner] Failed emitting task_compiled: %s", e)
            logger.debug("[TaskCreateCompileRunner] task_compiled emit exception", exc_info=True)

    @staticmethod
    def _emit_compile_failed(*, room_id: str, session_id: str, error: str) -> None:
        if not room_id:
            logger.warning(
                "[TaskCreateCompileRunner] Cannot emit task_compile_failed: room_id is empty. "
                "session=%s error=%s",
                session_id,
                error,
            )
            return
        try:
            from app.assistant.ServiceLocator.service_locator import DI
            from app.assistant.utils.pydantic_classes import UserMessage, UserMessageData
            assistant_name = get_required_assistant_name()
            msg = UserMessage(
                data_type="user_msg",
                sender=assistant_name,
                receiver=None,
                role="assistant",
                metadata={"reply_to": {"type": "socketio", "room_id": room_id}},
                user_message_data=UserMessageData(
                    widget_data=[
                        {
                            "data_type": "task_compile_failed",
                            "session_id": session_id,
                            "error": error,
                        }
                    ]
                ),
            )
            msg.event_topic = "socket_emit"
            DI.event_hub.publish(msg)
        except Exception as e:
            logger.error("[TaskCreateCompileRunner] Failed emitting task_compile_failed: %s", e)
            logger.debug("[TaskCreateCompileRunner] task_compile_failed emit exception", exc_info=True)


def _build_data_bindings(steps: list) -> list:
    """
    Pure graph traversal: re-materialise data_bindings from consumes/produces IDs.
    Does not require phase_i_atoms or logic_tree — safe to run on editor-saved IRs.
    """
    step_order: dict[str, int] = {}
    producer_by_id: dict[str, str] = {}
    consumers_by_id: dict[str, list] = {}
    kind_by_id: dict[str, str] = {}

    for idx, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        step_id = str(step.get("id") or "").strip()
        if not step_id:
            continue
        step_order[step_id] = idx

        for data_id in (step.get("consumes_data_ids") or []):
            data_id = str(data_id or "").strip()
            if not data_id:
                continue
            consumers_by_id.setdefault(data_id, []).append(step_id)
            if data_id not in kind_by_id:
                kind_by_id[data_id] = "artifact" if data_id.startswith("artifact_") else "fact"

        for data_id in (step.get("produces_data_ids") or []):
            data_id = str(data_id or "").strip()
            if not data_id or data_id in producer_by_id:
                continue
            producer_by_id[data_id] = step_id
            kind_by_id[data_id] = "artifact" if data_id.startswith("artifact_") else "fact"

    bindings = []
    for data_id, consumers in consumers_by_id.items():
        producer = producer_by_id.get(data_id)
        if not producer:
            continue
        producer_idx = step_order.get(producer, -1)
        valid_consumers = [
            c for c in consumers
            if c != producer and step_order.get(c, -1) > producer_idx
        ]
        if not valid_consumers:
            continue
        state_path = (
            f"task_state.artifacts.{data_id}"
            if data_id.startswith("artifact_")
            else f"task_state.facts.{data_id}"
        )
        bindings.append({
            "data_id": data_id,
            "data_kind": kind_by_id[data_id],
            "state_path": state_path,
            "producer_step_id": producer,
            "consumer_step_ids": valid_consumers,
        })
    bindings.sort(key=lambda b: (step_order.get(b["producer_step_id"], 0), b["data_id"]))
    return bindings
