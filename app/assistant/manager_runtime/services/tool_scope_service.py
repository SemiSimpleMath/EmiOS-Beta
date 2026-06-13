from __future__ import annotations

import json
import re
from typing import Any

from app.assistant.lib.tool_execution.tool_access_control import resolve_tool_min_authority
from app.assistant.manager_runtime.services.tool_scope_state_manager import ToolScopeStateManager
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message, ScopeContext

logger = get_logger(__name__)

_PLANNER_MANAGER_NAME = "emi_team::planner"


class KeywordMetadataToolRanker:
    """
    Default tool ranker.

    Uses token overlap across tool name + contract metadata, with configurable
    weights and optional usage-history boosts.
    """

    DEFAULT_WEIGHTS = {
        "exact_tool_name": 8,
        "partial_tool_name": 5,
        "token_in_search_text": 2,
        "category_exact": 10,
        "verb_exact": 4,
        "entity_exact": 3,
        "recently_used": 3,
        "recently_installed": 2,
        "always_show_bonus": 100,
    }

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        if not isinstance(text, str) or not text.strip():
            return []
        toks = re.findall(r"[a-zA-Z0-9_]+", text.lower())
        return [t for t in toks if len(t) >= 2]

    @staticmethod
    def _tool_search_text(tool_name: str, cfg: dict[str, Any]) -> str:
        parts: list[str] = [str(tool_name or "")]
        contract = cfg.get("tool_contract") if isinstance(cfg.get("tool_contract"), dict) else None
        if isinstance(contract, dict):
            desc = contract.get("description")
            if isinstance(desc, str) and desc.strip():
                parts.append(desc.strip())
            inputs = contract.get("inputs")
            if isinstance(inputs, list):
                for item in inputs:
                    if not isinstance(item, dict):
                        continue
                    nm = item.get("name")
                    if isinstance(nm, str) and nm.strip():
                        parts.append(nm.strip())
                    typ = item.get("type")
                    if isinstance(typ, str) and typ.strip():
                        parts.append(typ.strip())
            metadata = contract.get("metadata")
            if isinstance(metadata, dict):
                category = metadata.get("category") or metadata.get("domain")
                if isinstance(category, str) and category.strip():
                    parts.append(category.strip())
                verbs = metadata.get("verbs") if isinstance(metadata.get("verbs"), list) else metadata.get("actions")
                if isinstance(verbs, list):
                    parts.extend([str(v).strip() for v in verbs if isinstance(v, str) and str(v).strip()])
                entities = metadata.get("entities") if isinstance(metadata.get("entities"), list) else metadata.get("selectors")
                if isinstance(entities, list):
                    parts.extend([str(v).strip() for v in entities if isinstance(v, str) and str(v).strip()])
        return " ".join(parts).lower()

    @classmethod
    def _resolve_weights(cls, vis_cfg: dict[str, Any]) -> dict[str, int]:
        weights = dict(cls.DEFAULT_WEIGHTS)
        raw = vis_cfg.get("weights") if isinstance(vis_cfg.get("weights"), dict) else {}
        for key, val in raw.items():
            if key not in weights:
                continue
            try:
                weights[key] = int(val)
            except Exception as e:
                logger.debug("Ignoring non-int tool_visibility weight %s=%r: %s", key, val, e, exc_info=True)
        return weights

    def rank_tools(
        self,
        *,
        all_tools_cfg: dict[str, Any],
        task: str | None,
        information: str | None,
        always_show: list[str],
        recently_used: list[str],
        recently_installed: list[str],
        vis_cfg: dict[str, Any],
    ) -> list[str]:
        query = f"{task or ''} {information or ''}".strip()
        tokens = self._tokenize(query)
        weights = self._resolve_weights(vis_cfg)
        recently_used_set = set(recently_used)
        recently_installed_set = set(recently_installed)

        scores: list[tuple[int, str]] = []
        for tool_name, cfg_row in (all_tools_cfg.items() if isinstance(all_tools_cfg, dict) else []):
            if not isinstance(tool_name, str) or not tool_name.strip():
                continue
            cfg_obj = cfg_row if isinstance(cfg_row, dict) else {}
            hay = self._tool_search_text(tool_name, cfg_obj)
            tool_lower = tool_name.lower()
            contract = cfg_obj.get("tool_contract") if isinstance(cfg_obj.get("tool_contract"), dict) else {}
            metadata = contract.get("metadata") if isinstance(contract.get("metadata"), dict) else {}
            category = str(metadata.get("category") or metadata.get("domain") or "").strip().lower()
            verbs_raw = metadata.get("verbs") if isinstance(metadata.get("verbs"), list) else metadata.get("actions")
            verbs = {
                str(v).strip().lower()
                for v in (verbs_raw if isinstance(verbs_raw, list) else [])
                if isinstance(v, str) and str(v).strip()
            }
            entities_raw = metadata.get("entities") if isinstance(metadata.get("entities"), list) else metadata.get("selectors")
            entities = {
                str(v).strip().lower()
                for v in (entities_raw if isinstance(entities_raw, list) else [])
                if isinstance(v, str) and str(v).strip()
            }

            score = 0
            for tok in tokens:
                if tok == tool_lower:
                    score += weights["exact_tool_name"]
                elif tok in tool_lower:
                    score += weights["partial_tool_name"]
                if tok in hay:
                    score += weights["token_in_search_text"]
                if category and tok == category:
                    score += weights["category_exact"]
                if tok in verbs:
                    score += weights["verb_exact"]
                if tok in entities:
                    score += weights["entity_exact"]
            if tool_name in recently_used_set:
                score += weights["recently_used"]
            if tool_name in recently_installed_set:
                score += weights["recently_installed"]
            if tool_name in always_show:
                score += weights["always_show_bonus"]
            scores.append((score, tool_name))

        scores.sort(key=lambda x: x[0], reverse=True)
        return [name for _, name in scores]


class ToolScopeService:
    """
    Manager pre-step tool scoping:
    - keeps full allowed tool policy intact
    - computes a smaller visible tool shortlist for planner prompts
    """

    # -1 means unlimited visibility (no top-N cap).
    # None is also treated as unlimited.
    DEFAULT_MAX_VISIBLE = -1

    def _run_narrower(
        self,
        *,
        ranked: list[str],
        always_show: list[str],
        tool_registry,
        task: str | None,
        information: str | None,
        manager_name: str,
    ) -> list[str]:
        """
        Calls shared::tool_narrower to pick the most task-relevant tools from the
        already-filtered ranked list. always_show tools are preserved regardless.
        Returns the original ranked list unchanged if the narrower fails.
        """
        from app.assistant.ServiceLocator.service_locator import DI
        try:
            descriptions = tool_registry.get_tool_descriptions(ranked) or {}
            candidates = [
                {"name": n, "description": str(descriptions.get(n) or "").strip()}
                for n in ranked
            ]

            # Deterministic pre-filter by tool metadata (domain + actions).
            task_text = f"{task or ''}\n{information or ''}"
            from app.assistant.lib.task_utils.tool_domain_filter import filter_candidates
            pre_filtered, trace = filter_candidates(
                task_text=task_text,
                candidate_tools=candidates,
                tool_registry=tool_registry,
            )
            logger.info(
                "[tool_scope] domain_filter manager=%s %d -> %d (domains=%s actions=%s)",
                manager_name, len(candidates), len(pre_filtered),
                trace.get("inferred_domains"), trace.get("inferred_actions"),
            )
            # Short-circuit: if we got a tight, confident match, skip the LLM.
            if (
                trace.get("inferred_domains")
                and len(pre_filtered) <= 10
                and trace.get("unmigrated_count", 0) == 0
            ):
                narrowed_names = [c["name"] for c in pre_filtered]
                ranked_set = set(ranked)
                always_show_set = set(always_show)
                result: list[str] = []
                for t in always_show:
                    if t in ranked_set and t not in result:
                        result.append(t)
                for t in narrowed_names:
                    if t in ranked_set and t not in result:
                        result.append(t)
                logger.info(
                    "[tool_scope] domain_filter short-circuit manager=%s after=%s",
                    manager_name, result,
                )
                return result
            candidates = pre_filtered
            candidate_payload = json.dumps(candidates, ensure_ascii=False)

            narrower = DI.agent_factory.create_agent("shared::tool_narrower")
            if not narrower:
                raise RuntimeError("shared::tool_narrower agent not available")
            result_msg = narrower.action_handler(
                Message(
                    agent_input={
                        "task": str(task or "").strip(),
                        "information": str(information or "").strip(),
                        "candidate_tools": candidate_payload,
                    }
                )
            )
            result_data = getattr(result_msg, "data", None)
            if not isinstance(result_data, dict):
                raise ValueError("shared::tool_narrower returned invalid data")
            likely_raw = result_data.get("likely_tools")
            if not isinstance(likely_raw, list):
                raise ValueError("shared::tool_narrower missing likely_tools")
            ranked_set = set(ranked)
            always_show_set = set(always_show)
            narrowed = [
                str(t).strip() for t in likely_raw
                if isinstance(t, str) and str(t).strip() in ranked_set
            ]
            # Re-inject always_show at the front so they survive narrowing.
            result: list[str] = []
            for t in always_show:
                if t in ranked_set and t not in result:
                    result.append(t)
            for t in narrowed:
                if t not in result:
                    result.append(t)
            logger.info(
                "[tool_scope] narrower applied manager=%s before=%s after=%s",
                manager_name,
                len(ranked),
                len(result),
            )
            logger.debug("[tool_scope] narrower result=%s", result)
            return result if result else ranked
        except Exception as e:
            logger.error("[tool_scope] narrower failed for manager=%s: %s", manager_name, e)
            logger.debug("[tool_scope] narrower exception details", exc_info=True)
            return ranked

    @staticmethod
    def _read_string_list(blackboard, key: str) -> list[str]:
        try:
            raw = blackboard.get_state_value(key)
        except Exception as e:
            logger.debug("Failed reading blackboard list '%s': %s", key, e, exc_info=True)
            raw = None
        if not isinstance(raw, list):
            return []
        out: list[str] = []
        for item in raw:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
        return out

    @staticmethod
    def _resolve_ranker(vis_cfg: dict[str, Any]):
        ranker_kind = vis_cfg.get("ranker", "keyword_metadata")
        if not isinstance(ranker_kind, str) or not ranker_kind.strip():
            raise ValueError("tool_visibility.ranker must be a non-empty string when provided.")
        ranker_kind = ranker_kind.strip().lower()
        if ranker_kind != "keyword_metadata":
            raise ValueError(
                f"Unsupported tool_visibility.ranker={ranker_kind!r}. "
                "Supported rankers: ['keyword_metadata']."
            )
        return KeywordMetadataToolRanker()

    @staticmethod
    def _read_scope(blackboard) -> tuple[bool, "ScopeContext | None"]:
        """Read (scope_contract_enforced, scope_context) from the blackboard.

        Fails CLOSED: if enforcement is on but the scope is missing or corrupt,
        raise rather than silently expose an unscoped tool list (mirrors the
        execution gate, tool_caller). Single reader so every visibility path —
        pinned and ranked — gets the same fail-closed behavior.
        """
        scope_contract_enforced = False
        scope_context: ScopeContext | None = None
        try:
            scope_contract_enforced = bool(blackboard.get_state_value("scope_contract_enforced", False))
            raw_scope = blackboard.get_state_value("scope_context")
            if scope_contract_enforced and raw_scope is not None:
                if isinstance(raw_scope, ScopeContext):
                    scope_context = raw_scope
                elif isinstance(raw_scope, dict):
                    scope_context = ScopeContext.model_validate(raw_scope)
        except Exception as e:
            logger.error("[tool_scope] Failed reading scope_context for tool filtering: %s", e)
            logger.debug("[tool_scope] scope_context read exception details", exc_info=True)
            if scope_contract_enforced:
                raise ValueError(
                    "[tool_scope] scope_contract_enforced=true but scope_context could not "
                    "be read/validated for tool visibility filtering — refusing to expose "
                    "an unscoped tool list."
                ) from e
        if scope_contract_enforced and scope_context is None:
            raise ValueError(
                "[tool_scope] scope_contract_enforced=true but no scope_context is set for "
                "tool visibility filtering — refusing to expose an unscoped tool list."
            )
        return scope_contract_enforced, scope_context

    @staticmethod
    def _filter_to_ceiling(
        items: list[str], *, scope_contract_enforced: bool,
        scope_context: "ScopeContext | None", all_tools_cfg,
    ) -> list[str]:
        """Filter a tool list to the scope's permission CEILING — the single
        definition of 'allowed'. Two gates, both mirroring execution
        (check_tool_access):
          - allow/block: allowed_tools / blocked_tools (already folded with
            per_manager + the subtree-grant at narrowing time).
          - L1 authority floor: a tool whose min_authority exceeds the scope's
            authority_level is dropped.
        EVERY visibility path runs through this — pinned and ranked alike — so
        visibility is always a strict subset of what execution permits. A
        forced/pinned tool can NARROW visibility, never bypass the ceiling.

        scope_contract_enforced=False => no contract requested => unrestricted.
        """
        if not scope_contract_enforced:
            return items
        result = items
        scope_allowed = scope_context.tools.allowed_tools if isinstance(scope_context.tools.allowed_tools, list) else []
        scope_blocked = scope_context.tools.blocked_tools if isinstance(scope_context.tools.blocked_tools, list) else []
        allow_set = {str(x).strip() for x in scope_allowed if isinstance(x, str) and str(x).strip()}
        block_set = {str(x).strip() for x in scope_blocked if isinstance(x, str) and str(x).strip()}
        # Empty allow_set means "allow nothing" -> show nothing. Only "all"
        # bypasses the allow filter (absence of a contract is handled above).
        if "all" not in allow_set:
            result = [t for t in result if t in allow_set]
        if block_set:
            result = [t for t in result if t not in block_set]
        authority_level = int(getattr(scope_context.approval, "authority_level", 0) or 0)
        kept: list[str] = []
        for t in result:
            floor = resolve_tool_min_authority(t, all_tools_cfg.get(t) if isinstance(all_tools_cfg, dict) else None)
            if floor is None or authority_level >= floor:
                kept.append(t)
        return kept

    def initialize_scope(
        self,
        *,
        blackboard,
        tool_registry,
        manager_config: dict[str, Any] | None,
        task: str | None,
        information: str | None,
    ) -> None:
        cfg = manager_config if isinstance(manager_config, dict) else {}
        vis_cfg = cfg.get("tool_visibility") if isinstance(cfg.get("tool_visibility"), dict) else {}
        manager_name = str(cfg.get("name") or "").strip()

        # If the caller pre-seeded visible_tools (e.g. via pinned_tools on a compiled task step),
        # use that list directly and skip all ranking, hidden_tools filtering, and LLM narrowing.
        # scope_adapter has already validated these against the effective allow/block policy.
        try:
            pinned_raw = blackboard.get_state_value("visible_tools")
        except Exception as e:
            logger.debug("Failed reading pre-seeded visible_tools: %s", e, exc_info=True)
            pinned_raw = None
        if isinstance(pinned_raw, list) and pinned_raw:
            pinned: list[str] = [s.strip() for s in pinned_raw if isinstance(s, str) and s.strip()]
            if pinned:
                # Pinned tools skip RANKING/NARROWING (relevance) but NOT the
                # permission CEILING. A pinned tool that's blocked or above the
                # scope's authority is dropped here, so visibility stays a strict
                # subset of allowed — the same invariant the ranked path holds.
                # (Previously the pinned path returned the list verbatim, a
                # bypass that let a forced tool be shown despite the authority
                # floor; execution then denied it and aborted the manager.)
                scope_contract_enforced, scope_context = self._read_scope(blackboard)
                # No try/except-to-default: a registry read failure must fail
                # loud, not silently leave the ceiling unenforceable.
                pinned = self._filter_to_ceiling(
                    pinned, scope_contract_enforced=scope_contract_enforced,
                    scope_context=scope_context,
                    all_tools_cfg=tool_registry.get_all_tools(),
                )
                logger.info(
                    "[tool_scope] %s pinned_tools override: skipping ranking/narrowing, "
                    "ceiling-filtered to %s tool(s): %s",
                    manager_name or "manager",
                    len(pinned),
                    pinned,
                )
                ToolScopeStateManager(blackboard).initialize_runtime_scope(
                    visible_tools=pinned,
                    max_visible=-1,
                )
                return

        always_show = vis_cfg.get("always_show")
        if not isinstance(always_show, list) or not always_show:
            always_show = ["find_tool", "install_tool", "ask_user"]
        always_show = [str(x).strip() for x in always_show if isinstance(x, str) and str(x).strip()]

        hidden_tools_raw = vis_cfg.get("hidden_tools")
        hidden_tools: set[str] = set()
        if isinstance(hidden_tools_raw, list):
            hidden_tools = {str(x).strip() for x in hidden_tools_raw if isinstance(x, str) and str(x).strip()}

        ranker = self._resolve_ranker(vis_cfg)
        recently_used = self._read_string_list(blackboard, "recently_used_tools")
        recently_installed = self._read_string_list(blackboard, "recently_installed_tools")

        # No try/except-to-default: if the registry can't be read while we're
        # shaping a scoped tool list, fail loud rather than silently rank/filter
        # against an empty config (which would mis-resolve every authority floor).
        all_tools_cfg = tool_registry.get_all_tools()
        ranked = ranker.rank_tools(
            all_tools_cfg=all_tools_cfg if isinstance(all_tools_cfg, dict) else {},
            task=task,
            information=information,
            always_show=always_show,
            recently_used=recently_used,
            recently_installed=recently_installed,
            vis_cfg=vis_cfg,
        )
        if manager_name == _PLANNER_MANAGER_NAME:
            logger.info(
                "[tool_scope] planner initialize_scope manager=%s ranker=%s always_show=%s candidate_count=%s",
                manager_name,
                str(vis_cfg.get("ranker") or "keyword_metadata"),
                always_show,
                len(ranked),
            )
            logger.debug(
                "[tool_scope] planner ranked_tools=%s",
                ranked,
            )

        # When the LLM narrower is enabled, give it the FULL ranked list so it
        # can surface any tool the task needs (including ones in hidden_tools).
        # The narrower IS the filter — hidden_tools is redundant when it runs.
        # When the narrower is NOT enabled, apply hidden_tools as the filter.
        use_narrower = bool(vis_cfg.get("use_narrower"))
        ranked_for_narrower = list(ranked)  # Save pre-hidden list for narrower.

        if hidden_tools and not use_narrower:
            pre_hidden_count = len(ranked)
            ranked = [t for t in ranked if t not in hidden_tools]
            if manager_name == _PLANNER_MANAGER_NAME:
                logger.info(
                    "[tool_scope] planner hidden_tools applied hidden_count=%s before=%s after=%s",
                    len(hidden_tools),
                    pre_hidden_count,
                    len(ranked),
                )

        # Read the scope (fail-closed) and filter to its ceiling — same logic as
        # the pinned path, via the shared helpers, so visibility is a strict
        # subset of allowed on every path.
        scope_contract_enforced, scope_context = self._read_scope(blackboard)

        def _apply_scope_filters(items: list[str], stage: str) -> list[str]:
            """Visibility is a strict subset of the scope's permission ceiling
            (allow/block + L1 authority floor). Called BEFORE the narrower (so it
            never evaluates already-blocked tools) AND AFTER it (so its expanded
            list can't re-introduce out-of-ceiling tools)."""
            return self._filter_to_ceiling(
                items, scope_contract_enforced=scope_contract_enforced,
                scope_context=scope_context, all_tools_cfg=all_tools_cfg,
            )

        # Pre-narrower filter: keeps narrower from wasting tokens on blocked tools.
        ranked = _apply_scope_filters(ranked, stage="pre_narrower")

        # Optional LLM narrowing: tighten visible list to task-relevant tools.
        # Narrower sees the FULL pre-filter ranked list (pre-hidden) so it can
        # surface any tool the task needs, including leaf tools normally hidden
        # from the planner.
        if use_narrower and (task or information):
            ranked = self._run_narrower(
                ranked=ranked_for_narrower,
                always_show=always_show,
                tool_registry=tool_registry,
                task=task,
                information=information,
                manager_name=manager_name,
            )

        # Post-narrower filter: narrower's expanded output runs through scope
        # again so it can't re-introduce out-of-ceiling tools. This is the
        # load-bearing apply — without it the narrowed allowed_tools ceiling has
        # no effect when use_narrower is True (which it is for emi_team and other
        # broad managers, whose narrower can surface anything in the registry).
        ranked = _apply_scope_filters(ranked, stage="post_narrower")

        visible: list[str] = []
        for t in always_show:
            if t in ranked and t not in visible:
                visible.append(t)
        for t in ranked:
            if t in visible:
                continue
            visible.append(t)

        # Show every visible tool; do not cap to top-N.
        max_visible = -1

        ToolScopeStateManager(blackboard).initialize_runtime_scope(
            visible_tools=visible,
            max_visible=max_visible,
        )
        if manager_name == _PLANNER_MANAGER_NAME:
            logger.info(
                "[tool_scope] planner final visible tools count=%s max_visible=%s",
                len(visible),
                max_visible,
            )
            logger.debug(
                "[tool_scope] planner final visible tools=%s",
                visible,
            )

