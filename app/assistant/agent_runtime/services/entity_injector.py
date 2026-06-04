import json
from typing import Any, Dict, List


from app.assistant.entity_management.entity_card_injector import EntityCardInjector
from app.assistant.utils.identity_names import get_assistant_name
from app.assistant.utils.logging_config import get_logger
from app.models.base import get_session
from app.assistant.entity_management.entity_cards import (
    get_entity_card_for_prompt_injection_level,
)

logger = get_logger(__name__)

ENTITY_INJECTION_KEYS: set[str] = {
    "entity_level_0",
    "entity_summary",
    "entity_context",
    "entity_metadata",
    "entity_key_facts",
    "entity_injection",
    "entity_info",
    "entity_card",
}
DEFAULT_ENTITY_SCAN_KEYS = ["incoming_message", "task", "information", "recent_history"]


class EntityInjector:
    @staticmethod
    def _limit_recent_history_scan(text: str) -> str:
        raw = str(text or "").strip()
        if not raw:
            return ""
        # Guardrail: entity detection over full history is too noisy.
        # Only scan the latest 3 recent-history message blocks.
        blocks = [block.strip() for block in raw.split("\n\n") if block.strip()]
        if not blocks:
            return ""

        marker_blocks = {"CHAT FROM YESTERDAY", "CHAT FROM TODAY"}
        message_blocks = [b for b in blocks if b.upper() not in marker_blocks]
        if not message_blocks:
            return ""

        recent_blocks = message_blocks[-3:]
        return "\n\n".join(recent_blocks).strip()

    @staticmethod
    def get_injection_keys() -> set[str]:
        return set(ENTITY_INJECTION_KEYS)

    def format_entity_cards_leveled(
        self,
        entities: List[str],
        *,
        level: int = 1,
        sections: list[str] | None = None,
    ) -> str:
        """Render detected entities using the view-level system.

        Reads entirely through ``get_entity_card_for_prompt_injection_level``
        which is backed by entity_card_v2. Returns empty for entities without
        an active v2 card.

        When `sections` is provided it overrides `level`: render exactly the
        named card sections (in order). Used by agents whose context budget
        should be narrower than any whole level — e.g. personal_admin wants
        only level_0 + contact, not the L3 superset.
        """
        if not entities:
            return ""
        session = get_session()
        blocks: list[str] = []
        try:
            for name in entities:
                rendered = get_entity_card_for_prompt_injection_level(
                    session, name, level=level, sections=sections,
                )
                if rendered:
                    blocks.append(rendered)
        finally:
            session.close()
        return "\n\n".join(blocks)

    def render_user_prompt_with_entities(
        self,
        *,
        agent: Any,
        message: Any,
        agent_name: str,
        user_prompt_template: str,
        user_context: dict,
        prompt_injections: list,
        entity_injection_keys: set[str],
    ) -> str:
        from app.assistant.agent_runtime.services.prompt_builder import _jinja_env
        template = _jinja_env.from_string(user_prompt_template)
        render_keys = self._resolve_render_keys(prompt_injections, entity_injection_keys)
        render_fields = self._resolve_render_fields(agent=agent, render_keys=render_keys)

        pass1_context = dict(user_context or {})
        for key in entity_injection_keys:
            pass1_context[key] = ""
        rendered_output = template.render(**pass1_context).replace("\n\n", "\n")

        if not render_fields:
            return rendered_output

        try:
            # Detect entities from the rendered prompt (pass 1), NOT raw context values.
            # Raw context values include JSON-dumped complex objects (e.g. 133-item
            # active_dayflow_items list with full email bodies) whose content never
            # appears in the agent's rendered prompt, causing false-positive entity
            # matches.  Scanning the rendered text guarantees we only pick up entities
            # that the agent can actually see.
            room_blocked = self._load_room_blocked_entities(user_context)
            detected_entities = self._detect_and_rank_entities(
                scan_blocks={"__rendered__": rendered_output},
                blocked_entities=room_blocked,
            )
            seeded_entities = self._seed_entities_from_context(user_context, blocked_entities=room_blocked)
            merged_entities = self._merge_seeded_and_detected(seeded_entities=seeded_entities, detected_entities=detected_entities)
            narrowed_entities = self._narrow_entities_by_scope(
                agent=agent,
                user_context=user_context or {},
                entities=merged_entities,
            )
            final_entities = narrowed_entities
            if not final_entities:
                return rendered_output

            final_context = dict(user_context or {})

            # entity_card (and the per-level aliases like entity_level_0,
            # entity_summary, etc.) all render via the level-based v2 path.
            # The granular field-extraction path (entity_info, entity_relationships,
            # entity_key_facts as individual keys) was removed when v1 was
            # retired — no production agents requested those keys.
            card_level = int((agent.config or {}).get("entity_card_level", 1))
            # entity_card_sections, when set, overrides the level for the
            # `entity_card` key only. Per-level aliases (entity_level_0,
            # entity_summary, ...) keep their fixed semantics so an agent that
            # asks for entity_summary still gets a summary even if its
            # entity_card has been narrowed to level_0 + contact.
            raw_sections = (agent.config or {}).get("entity_card_sections")
            card_sections: list[str] | None = (
                [str(x) for x in raw_sections if isinstance(x, str) and x.strip()]
                if isinstance(raw_sections, list) and raw_sections
                else None
            )
            level_by_key = {
                "entity_level_0": 0,
                "entity_summary": 1,
                "entity_context": 2,
                "entity_info": 2,
                "entity_key_facts": 2,
                "entity_metadata": 4,
                "entity_injection": 1,
                "entity_card": card_level,
            }
            for key in entity_injection_keys:
                level = level_by_key.get(key, card_level)
                use_sections = card_sections if key == "entity_card" else None
                final_context[key] = self.format_entity_cards_leveled(
                    final_entities, level=level, sections=use_sections,
                )
            return template.render(**final_context).replace("\n\n", "\n")
        except Exception as e:
            logger.error("[%s] Entity detection failed during prompt rendering: %s", agent_name, e)
            logger.debug("[%s] Entity detection render exception details", agent_name, exc_info=True)
            raise

    @staticmethod
    def _resolve_scan_keys(agent: Any, *, require_explicit: bool) -> list[str]:
        has_explicit = "entity_scan_keys" in (agent.config or {})
        if require_explicit and not has_explicit:
            raise ValueError(
                f"[{getattr(agent, 'name', 'agent')}] Missing required entity_scan_keys while entity_* "
                "context items are requested."
            )
        raw = agent.config.get("entity_scan_keys", DEFAULT_ENTITY_SCAN_KEYS)
        if not isinstance(raw, list):
            raise ValueError(f"[{getattr(agent, 'name', 'agent')}] entity_scan_keys must be a list when provided.")
        out: list[str] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, str) or not item.strip():
                continue
            key = item.strip()
            if key in seen:
                continue
            seen.add(key)
            out.append(key)
        return out

    @staticmethod
    def _resolve_render_keys(prompt_injections: list, entity_injection_keys: set[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for key in prompt_injections or []:
            if not isinstance(key, str):
                continue
            if key not in entity_injection_keys:
                continue
            if key in seen:
                continue
            seen.add(key)
            out.append(key)
        return out

    @staticmethod
    def _resolve_render_fields(*, agent: Any, render_keys: list[str]) -> list[str]:
        explicit = agent.config.get("entity_render_fields")
        if explicit is not None:
            if not isinstance(explicit, list):
                raise ValueError(f"[{getattr(agent, 'name', 'agent')}] entity_render_fields must be a list when provided.")
            fields = []
            seen = set()
            for item in explicit:
                if not isinstance(item, str) or not item.strip():
                    continue
                key = item.strip()
                if key in seen:
                    continue
                seen.add(key)
                fields.append(key)
            return fields

        fields = []
        seen = set()
        for key in render_keys:
            if not key.startswith("entity_"):
                continue
            field = key[len("entity_") :].strip()
            if field in {"", "info"}:
                continue
            if field in seen:
                continue
            seen.add(field)
            fields.append(field)
        return fields

    @staticmethod
    def _collect_scan_blocks(*, user_context: Dict[str, Any], scan_keys: list[str]) -> dict[str, str]:
        """Collect text blocks for entity scanning.

        Only string values are scanned — these are the pre-formatted texts
        that will appear verbatim in the rendered prompt (built by prep nodes).
        Dict/list values are accessed selectively by templates, so scanning
        their full JSON would detect entities from fields the LLM never sees.
        """
        blocks: dict[str, str] = {}
        for key in scan_keys:
            if key not in user_context:
                continue
            value = user_context.get(key)
            if not isinstance(value, str):
                continue
            text = value.strip()
            if not text:
                continue
            if key == "recent_history":
                text = EntityInjector._limit_recent_history_scan(text)
                if not text:
                    continue
            blocks[key] = text
        return blocks

    def _detect_and_rank_entities(self, *, scan_blocks: dict[str, str], blocked_entities: set[str] | None = None) -> list[str]:
        if not scan_blocks:
            return []
        injector = EntityCardInjector()
        all_detected: list[str] = []
        for value in scan_blocks.values():
            detected = injector.detect_entities_in_text(value) or []
            for ent in detected:
                if isinstance(ent, str) and ent.strip():
                    all_detected.append(ent.strip())
        filtered = self._filter_entities_for_prompt(all_detected, blocked_entities=blocked_entities)
        return self._rank_entities(filtered, "\n".join(scan_blocks.values()))

    @staticmethod
    def _rank_entities(entities: list[str], scan_text: str) -> list[str]:
        unique: list[str] = []
        seen: set[str] = set()
        for ent in entities:
            if ent in seen:
                continue
            seen.add(ent)
            unique.append(ent)
        lowered = str(scan_text or "").lower()
        if not lowered:
            return unique
        scored = []
        for ent in unique:
            low = ent.lower()
            first = lowered.find(low)
            count = lowered.count(low)
            first_key = first if first >= 0 else 10**9
            scored.append((first_key, -count, -len(low), ent))
        scored.sort()
        return [x[-1] for x in scored]

    @staticmethod
    def _merge_seeded_and_detected(*, seeded_entities: list[str], detected_entities: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for ent in [*seeded_entities, *detected_entities]:
            if ent in seen:
                continue
            seen.add(ent)
            out.append(ent)
        return out

    def _narrow_entities_by_scope(self, *, agent: Any, user_context: dict, entities: list[str]) -> list[str]:
        # Placeholder hook: scope-based narrowing will be enforced here in a follow-up step.
        _ = agent
        _ = user_context
        return list(entities or [])

    @staticmethod
    def _load_room_blocked_entities(user_context: dict | None) -> set[str]:
        if not isinstance(user_context, dict):
            return set()
        raw = user_context.get("room_blocked_entities")
        if not isinstance(raw, list):
            return set()
        return {str(v).strip() for v in raw if isinstance(v, str) and v.strip()}

    @staticmethod
    def _normalize_entity_name(value: str) -> str:
        return " ".join(str(value or "").strip().lower().split())

    def _filter_entities_for_prompt(self, entities: List[str], *, blocked_entities: set[str] | None = None) -> List[str]:
        if not entities:
            return []
        assistant_name = self._normalize_entity_name(get_assistant_name())
        blocked_names = {"assistant"}
        if assistant_name:
            blocked_names.update({assistant_name, f"{assistant_name} agent"})
        for name in (blocked_entities or set()):
            normalized = self._normalize_entity_name(name)
            if normalized:
                blocked_names.add(normalized)
        out: List[str] = []
        for ent in entities:
            normalized = self._normalize_entity_name(ent)
            if not normalized:
                continue
            if normalized in blocked_names:
                continue
            out.append(ent)
        return out

    def _seed_entities_from_context(self, user_context: dict | None, *, blocked_entities: set[str] | None = None) -> List[str]:
        if not isinstance(user_context, dict):
            return []
        ordered_keys = [
            "room_pinned_entities",
            "pinned_entities",
            "room_allowed_entity_cards",
            "allowed_entity_cards",
        ]
        seeds: list[str] = []
        for key in ordered_keys:
            raw = user_context.get(key)
            if not isinstance(raw, list):
                continue
            for value in raw:
                if not isinstance(value, str):
                    continue
                candidate = value.strip()
                if not candidate:
                    continue
                if candidate.lower() == "all":
                    continue
                seeds.append(candidate)
        filtered = self._filter_entities_for_prompt(seeds, blocked_entities=blocked_entities)
        deduped: list[str] = []
        seen: set[str] = set()
        for ent in filtered:
            if ent in seen:
                continue
            seen.add(ent)
            deduped.append(ent)
        return deduped


