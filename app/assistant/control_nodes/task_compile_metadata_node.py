from __future__ import annotations

import hashlib
import json
import re

import yaml
from datetime import datetime, timezone

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import as_repo_relative, get_repo_root, resolve_repo_path

logger = get_logger(__name__)


class TaskCompileMetadataNode(ControlNode):
    """
    Deterministic pre-planner node that injects compile metadata.
    """

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        cfg = self._cfg()
        next_agent = self._required_str(cfg, "next_agent")
        compiler_name = self._required_str(cfg, "compiler_name")
        compiler_version = self._required_str(cfg, "compiler_version")

        task = self.blackboard.get_state_value("task", "")
        information = self.blackboard.get_state_value("information", "")
        raw_task_llm_params = self.blackboard.get_state_value("task_llm_params", None)
        task_text = str(task or "").strip()
        information_text = str(information or "").strip()
        info_meta = self._parse_information_metadata(information_text)
        task_llm_params = self._normalize_task_llm_params(raw_task_llm_params)

        # If a task_file is provided, load its body as the authoritative source_task.
        # The caller's task string is just a routing instruction, not the spec content.
        task_file_from_meta = str(info_meta.get("task_file") or "").strip()
        if task_file_from_meta:
            spec_body = self._load_spec_body(task_file_from_meta)
            if spec_body:
                task_text = spec_body

        digest_basis = f"{task_text}\n---\n{information_text}".encode("utf-8")
        source_hash = hashlib.sha256(digest_basis).hexdigest()[:16]
        task_id = self._resolve_compile_task_id(info_meta=info_meta, source_hash=source_hash)
        task_file = self._resolve_task_file(info_meta=info_meta)
        task_description = str(info_meta.get("task_description") or "").strip()
        compile_seed = {
            "task_id": task_id,
            "task_file": task_file,
            "task_description": task_description,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "compiler_name": compiler_name,
            "compiler_version": compiler_version,
            "source_hash": source_hash,
            "source_task": task_text,
            "source_information": information_text,
            "task_llm_params": task_llm_params,
        }
        manager_catalog = self._build_manager_catalog()
        compile_contract = self._build_compile_contract()
        self.blackboard.update_global_state_value("compile_seed", compile_seed)
        self.blackboard.update_state_value("compile_seed", compile_seed)
        self.blackboard.update_global_state_value("task_compile_manager_catalog", manager_catalog)
        self.blackboard.update_state_value("task_compile_manager_catalog", manager_catalog)
        self.blackboard.update_global_state_value("task_compile_contract", compile_contract)
        self.blackboard.update_state_value("task_compile_contract", compile_contract)
        self.blackboard.update_state_value("last_agent", self.name)
        self.blackboard.update_state_value("next_agent", next_agent)

    @staticmethod
    def _normalize_task_llm_params(raw: object) -> dict[str, object] | None:
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise TypeError(f"task_llm_params must be an object when provided, got {type(raw).__name__}.")
        out: dict[str, object] = {}
        engine = raw.get("engine")
        if engine is not None:
            if not isinstance(engine, str) or not engine.strip():
                raise ValueError("task_llm_params.engine must be a non-empty string when provided.")
            out["engine"] = engine.strip()
        temperature = raw.get("temperature")
        if temperature is not None:
            if not isinstance(temperature, (int, float)):
                raise ValueError("task_llm_params.temperature must be numeric when provided.")
            out["temperature"] = float(temperature)
        timeout = raw.get("timeout")
        if timeout is not None:
            if not isinstance(timeout, (int, float)):
                raise ValueError("task_llm_params.timeout must be numeric when provided.")
            out["timeout"] = float(timeout)
        return out if out else None

    @staticmethod
    def _load_spec_body(task_file: str) -> str:
        """
        Load the markdown body of a task spec file (everything after the YAML frontmatter).
        If a sibling pre-compile.md exists, its contents are appended so the compile LLM
        sees spec + machine-generated hints as a single source_task.
        """
        try:
            spec_path = resolve_repo_path(task_file)
            if not spec_path.exists():
                logger.error("task_compile_metadata_node: spec file not found: %s", task_file)
                raise FileNotFoundError(f"Task spec file not found: {task_file}")
            raw = spec_path.read_text(encoding="utf-8")
            if raw.startswith("---"):
                end = raw.find("\n---", 3)
                if end != -1:
                    body = raw[end + 4:].strip()
                else:
                    body = raw.strip()
            else:
                body = raw.strip()

            precompile_path = spec_path.parent / "pre-compile.md"
            if precompile_path.exists():
                hints = precompile_path.read_text(encoding="utf-8").strip()
                if hints:
                    body = body + "\n\n" + hints
            return body
        except FileNotFoundError:
            raise
        except Exception as e:
            logger.error("task_compile_metadata_node: failed loading spec body from %s: %s", task_file, e)
            logger.debug("spec body load exception", exc_info=True)
            raise

    @staticmethod
    def _normalize_task_id(raw: str) -> str:
        value = re.sub(r"[^A-Za-z0-9_]+", "_", str(raw or "").strip().lower()).strip("_")
        if not value:
            raise ValueError("task_id resolved to empty value.")
        return value

    def _resolve_compile_task_id(self, *, info_meta: dict, source_hash: str) -> str:
        raw_task_id = str(info_meta.get("task_id") or "").strip()
        if raw_task_id:
            return self._normalize_task_id(raw_task_id)
        return f"task_{str(source_hash or '').strip()[:12]}"

    def _resolve_task_file(self, *, info_meta: dict) -> str:
        raw_task_file = str(info_meta.get("task_file") or "").strip()
        if not raw_task_file:
            return ""
        repo_root = get_repo_root()
        task_file_path = resolve_repo_path(raw_task_file)
        try:
            rel = as_repo_relative(task_file_path)
        except Exception as e:
            logger.error("Task compile metadata rejected task_file outside repo: %s", raw_task_file)
            logger.debug("task_file normalization exception details", exc_info=True)
            raise ValueError(f"task_file must be inside repository root: {raw_task_file}") from e
        return rel

    @staticmethod
    def _parse_information_metadata(information_text: str) -> dict:
        raw = str(information_text or "").strip()
        if not raw:
            return {}
        if not raw.startswith("{"):
            return {}
        try:
            parsed = json.loads(raw)
        except Exception as e:
            logger.error("Task compile metadata failed to parse information JSON.")
            logger.debug("information JSON parse exception details", exc_info=True)
            raise ValueError("information must be valid JSON object when it starts with '{'.") from e
        if not isinstance(parsed, dict):
            raise ValueError("information JSON must be an object.")
        return parsed

    def _build_manager_catalog(self) -> list[dict]:
        registry = DI.manager_registry
        all_configs = registry.all_configs()
        if not isinstance(all_configs, dict) or not all_configs:
            raise ValueError(f"[{self.name}] manager registry has no preloaded configs.")
        curated_descriptions = {
            "personal_admin_manager": {
                "purpose": "Personal admin operations.",
                "use_for": [
                    "Email read/send workflows",
                    "Calendar operations",
                    "Todo/reminder operations",
                    "User-facing admin messages",
                ],
            },
            "web_manager": {
                "purpose": "Web information retrieval and summarization.",
                "use_for": [
                    "Web search and page scraping",
                    "Cross-source web synthesis",
                    "Research-style web tasks",
                ],
            },
            "playwright_manager": {
                "purpose": "Real browser automation — visiting websites, reading pages, taking screenshots, extracting data.",
                "use_for": [
                    "Visiting a website and extracting content (headlines, prices, text)",
                    "Taking screenshots of web pages",
                    "Interactive website flows (login, forms, multi-page navigation)",
                    "Any step that says 'go to [website]' or 'visit [URL]'",
                ],
            },
            "emi_team_manager": {
                "purpose": "General-purpose fallback and multi-skill synthesis.",
                "use_for": [
                    "General reasoning and orchestration",
                    "File transformations and synthesis",
                    "Fallback when no specialized manager is required",
                ],
            },
            "fact_gate_manager": {
                "purpose": "Typed fact/artifact production with strict output contracts.",
                "use_for": [
                    "Boolean/integer/number/string/list gate outputs",
                    "Schema-constrained single-output steps",
                    "Deterministic typed values for downstream decisions",
                ],
            },
        }
        catalog: list[dict] = []
        for manager_name in sorted(all_configs.keys()):
            raw_cfg = all_configs.get(manager_name)
            if not isinstance(raw_cfg, dict):
                continue
            tools_cfg = raw_cfg.get("tools", {})
            if tools_cfg is None:
                tools_cfg = {}
            if not isinstance(tools_cfg, dict):
                raise TypeError(f"[{self.name}] manager '{manager_name}' tools must be dict when provided.")
            allowed_tools = tools_cfg.get("allowed_tools", [])
            normalized_tools = self._normalize_tools(allowed_tools)
            entry = curated_descriptions.get(manager_name)
            purpose = entry["purpose"] if entry else str(raw_cfg.get("description") or manager_name)
            use_for = entry["use_for"] if entry else []
            catalog.append(
                {
                    "manager_name": manager_name,
                    "purpose": purpose,
                    "use_for": use_for,
                    "allowed_tools": normalized_tools,
                }
            )
        return catalog

    def _build_compile_contract(self) -> dict:
        # Build executor enum dynamically from catalog + task spec manager.
        executor_names = [
            "emi_team_manager",
            "personal_admin_manager",
            "web_manager",
            "playwright_manager",
            "fact_gate_manager",
        ]
        # Add any manager declared in the task spec frontmatter.
        compile_seed = self.blackboard.get_state_value("compile_seed", None)
        if isinstance(compile_seed, dict):
            task_file = str(compile_seed.get("task_file") or "").strip()
            if task_file:
                try:
                    spec_path = resolve_repo_path(task_file)
                    if spec_path.exists():
                        raw = spec_path.read_text(encoding="utf-8")
                        lines = raw.splitlines()
                        if lines and lines[0].strip() == "---":
                            end = None
                            for idx in range(1, len(lines)):
                                if lines[idx].strip() == "---":
                                    end = idx
                                    break
                            if end is not None:
                                fm = yaml.safe_load("\n".join(lines[1:end])) or {}
                                spec_mgr = str(fm.get("manager") or "").strip()
                                if spec_mgr and spec_mgr not in executor_names:
                                    executor_names.append(spec_mgr)
                except Exception:
                    logger.debug("[%s] Could not read spec manager for contract enum", self.name, exc_info=True)

        return {
            "enums": {
                "compiled_task.schema_version": ["task_ir_v1"],
                "step.kind": ["action", "tool_sequence", "wait_for_event", "wait_gate", "decision", "end"],
            },
            "manager_surface_rules": [
                "A manager step can be multi-step internally. Do not split unless the task explicitly requires an external boundary.",
                "Do not split a macro step only because it may internally call specialized managers/tools.",
                "When two consecutive actions use the same manager, keep them in one action step unless a wait/decision boundary exists.",
                "For email workflows, prefer personal_admin_manager compose+send in one action step.",
                "Do not split email draft and send into separate actions unless explicit review/approval/reuse constraints require split.",
                "If recipient facts/context are already explicit in task prose/includes, do not emit standalone context-resolution action steps.",
                "If artifacts/includes are already listed as available task inputs, do not emit standalone load/include actions unless explicitly requested.",
                "Use emi_team_manager as the fallback manager for general tasks.",
                "Do not create calendar/scheduler manager actions solely to implement runtime time_event cadence.",
                "Do not elaborate task parameters for managers.",
                "Keep action instruction text short and minimal.",
                "Manager is expert and determines detailed execution.",
                "Use fact_gate_manager when a step has explicit typed output contract (output_schema or gate_type).",
                "CRITICAL — do not model internal manager logic as IR decision steps: The IR only orchestrates things that require pausing execution to wait for an external event or time condition. If a spec describes a decision where all branches invoke the same manager and no external wait is required (e.g. 'check email, if unreplied send reply, else skip'), fold it into a single action step instruction. The manager handles the internal logic itself. Only emit a decision step when the task genuinely branches to different IR paths based on an external event or time crossing.",
            ],
            "typed_output_rules": [
                "Action steps may declare output_schema (object) or gate_type (alias: TF/int/double/string/list).",
                "output_schema is optional; when absent runtime uses existing generic output mapping behavior.",
                "When output_schema is present, produces_data_ids must contain exactly one data id.",
                "Each action step may produce AT MOST ONE artifact_* id. Never declare more than one artifact_* in produces_data_ids for a single action step.",
                "If a step produces multiple intermediate results, represent only the final meaningful artifact as the single artifact output.",
                "Manager must emit final_answer_data_list entry with key matching produced data id.",
                "final_answer_data_list.value must be JSON literal string compatible with schema type.",
                "Do not use typed gate aliases for artifacts that carry large untyped payloads.",
            ],
            "event_wait_guidance": [
                {
                    "event_type": "email_received",
                    "example_event_name": "signal_router.watch.email_from_katy",
                    "when_to_use": "Wait for a specific sender/content email trigger.",
                },
                {
                    "event_type": "time_reached_alarm",
                    "example_event_name": "clock.local.17_00",
                    "when_to_use": "Wake execution at an exact local time crossing. If already past the time when the run starts or resumes, waits until the next occurrence (e.g. tomorrow). Use for strict daily schedules.",
                },
                {
                    "event_type": "time_reached_gate",
                    "example_event_name": "clock.after.07_00",
                    "when_to_use": "Proceed if already past the target time, or wait until it is reached. Use when the task should run once it is morning/afternoon/etc regardless of when it started.",
                },
                {
                    "event_type": "calendar_reminder",
                    "example_event_name": "calendar.event.reminder_fired",
                    "when_to_use": "Continue when a scheduled calendar reminder occurs.",
                },
                {
                    "event_type": "threshold_crossed",
                    "example_event_name": "signal_router.watch.weather_below_threshold",
                    "when_to_use": "React to monitored metric thresholds.",
                },
                {
                    "event_type": "periodic_tick",
                    "example_event_name": "clock.tick.15m",
                    "when_to_use": "Loop-style periodic checks (runtime-provided periodic tick).",
                },
            ],
            "watch_registration_contracts": [
                {
                    "watch_type": "email_semantic_match",
                    "when_to_use": "Semantic email watcher conditions like sender/topic intent matching.",
                    "required_predicate_keys": ["semantic_query"],
                    "optional_predicate_keys": ["sender_equals", "sender_contains", "subject_contains", "account_id_equals"],
                    "event_name_prefix": "signal_router.watch.email_",
                    "notes": [
                        "Watcher evaluates full email content and metadata.",
                        "Match event is notification only; add explicit fetch/read step if full email content is needed downstream.",
                    ],
                }
            ],
            "branching_rules": [
                "Use decision nodes when behavior diverges by condition/event outcome.",
                "Decision nodes must define both on_true and on_false targets.",
                "Decision nodes must be meaningful: on_true and on_false must not target the same step.",
                "Decision conditions should reuse Phase I condition expressions directly unless deterministic normalization is required.",
                "Every branch path must rejoin the flow or end explicitly.",
                "Use conditions that are declarative and short.",
            ],
            "loop_rules": [
                "Represent loops by linking next_step back to a prior step id.",
                "Include an explicit stop condition via a decision node.",
                "After loop stop, route to a terminal end path.",
            ],
            "compound_wait_rules": [
                "wait_for_event supports a single event_name only.",
                "wait_gate supports multi-event subscriptions via subscriptions[] and release_condition.",
                "Do not invent synthetic composite event names for boolean logic over multiple triggers.",
                "Use explicit boolean gate logic in decisions/conditions to represent A AND B / A OR B semantics.",
                "A successful wait_for_event wake already confirms the awaited event fired; do not add a defensive decision that re-checks the same event_name.",
                "Event payloads are notification metadata, not full resource objects; if downstream logic needs resource content, add an explicit manager action to fetch/read it after wake.",
                "For X OR Y waits, prefer a wait_gate with subscriptions=[X,Y] and release_condition that clears when either predicate is true.",
                "For X AND Y (or more) semantics, prefer a wait_gate with subscriptions and release_condition that clears only when all predicates are true.",
                "For mixed boolean semantics (X AND Y OR Z), use wait_gate with explicit parenthesized release_condition.",
                "For wait-until-time semantics, branch on deterministic facts only (for example task_state.facts.events_observed[...] or dedicated fact_* gates).",
                "Condition expressions must be runtime-parseable: use True/False booleans and quoted string literals.",
            ],
            "ambiguity_resolution_rules": [
                "When prose is ambiguous, choose deterministic defaults and document assumptions in logic_tree edge reasons.",
                "Default boolean precedence assumption is (A AND B) OR C unless user specifies grouping.",
                "Represent all ambiguity resolutions as explicit branch structure, not hidden inside action prose.",
                "Hard time bounds must always produce an explicit stop branch.",
            ],
        }

    @staticmethod
    def _normalize_tools(allowed_tools) -> list[str]:
        if allowed_tools is None:
            return []
        if isinstance(allowed_tools, str):
            value = allowed_tools.strip()
            return [value] if value else []
        if not isinstance(allowed_tools, list):
            raise TypeError(f"allowed_tools must be list[str] or str, got {type(allowed_tools)}.")
        out: list[str] = []
        for item in allowed_tools:
            if not isinstance(item, str):
                raise TypeError(f"allowed_tools entries must be str, got {type(item)}.")
            value = item.strip()
            if value:
                out.append(value)
        return out

    def _cfg(self) -> dict:
        node_cfg_map = self.blackboard.get_state_value("manager_control_node_configs", {})
        if not isinstance(node_cfg_map, dict):
            raise ValueError("manager_control_node_configs must be a dict.")
        node_cfg = node_cfg_map.get(self.name, {})
        if not isinstance(node_cfg, dict):
            raise ValueError(f"manager_control_node_configs['{self.name}'] must be a dict.")
        if not node_cfg:
            raise ValueError(f"[{self.name}] missing config.")
        return node_cfg

    @staticmethod
    def _required_str(cfg: dict, key: str) -> str:
        raw = cfg.get(key)
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"task compile metadata node requires non-empty '{key}'.")
        return raw.strip()
