import re
from pathlib import Path
from typing import Dict, List

from jinja2 import Environment, FileSystemLoader

from app.assistant.agent_runtime.exceptions import PromptRenderError
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.utils import normalize_to_ascii

logger = get_logger(__name__)

# Shared Jinja2 environment with loader rooted at the agents directory so
# templates can {% import "shared/macros/dayflow_items.j2" %} etc.
_AGENTS_DIR = Path(__file__).resolve().parents[2] / "agents"
_jinja_env = Environment(
    loader=FileSystemLoader(str(_AGENTS_DIR)),
    keep_trailing_newline=True,
)


class PromptBuilder:
    def construct_prompt(self, agent, message=None, entity_injection_keys: set[str] | None = None) -> List[Dict[str, str]]:
        system_prompt = self.get_system_prompt(agent, message, entity_injection_keys)
        user_prompt = self.get_user_prompt(agent, message, entity_injection_keys)
        user_prompt = self._append_runtime_injections(agent, user_prompt)

        if not system_prompt:
            logger.error("[%s] Error forming the system prompt.", agent.name)
        if not user_prompt:
            logger.error("[%s] Error forming the user prompt.", agent.name)

        # Strip legacy attachment markers (their text position is just
        # an annotation; image blocks for them go at the end).
        legacy_image_paths: list[str] = []
        seen_paths: set[str] = set()
        try:
            pat = re.compile(r"\[emi_image:\s*([^\]]+?)\s*\]")
            for m in pat.finditer(user_prompt or ""):
                p = (m.group(1) or "").strip()
                if p and p not in seen_paths:
                    legacy_image_paths.append(p)
                    seen_paths.add(p)
            pat_legacy = re.compile(r"\[mcp_image_path:\s*([^\]]+?)\s*\]")
            for m in pat_legacy.finditer(user_prompt or ""):
                p = (m.group(1) or "").strip()
                if p and p not in seen_paths:
                    legacy_image_paths.append(p)
                    seen_paths.add(p)
            user_prompt = pat.sub("", user_prompt or "")
            user_prompt = pat_legacy.sub("", user_prompt or "")
        except Exception as e:
            logger.error("[%s] Error parsing legacy image markers: %s", agent.name, e)
            legacy_image_paths = []

        provider = (agent.llm_params or {}).get("llm_provider", "")
        if provider != "gemini":
            system_prompt = normalize_to_ascii(system_prompt)
            user_prompt = normalize_to_ascii(user_prompt)

        # Pod-URI images are interleaved RIGHT AFTER each URI mention so
        # the LLM has unambiguous text↔image binding ("Katy in this pic
        # [image]" instead of bunched-at-end blocks where pairing is
        # order-only). Bound to last IMAGE_POD_RECENT_LIMIT URIs so a
        # long history doesn't blow token cost.
        #
        # Scan the POST-normalized prompt so positions are valid offsets
        # into the same string we'll slice for the content array.
        pod_image_inserts: list[tuple[int, str]] = []
        try:
            from app.assistant.pod_store.pod_uri import POD_URI_RE
            from app.assistant.pod_store.pod_store import PodStore
            from app.assistant.utils.path_utils import get_repo_root
            IMAGE_POD_RECENT_LIMIT = 4
            seen_uris: set[str] = set()
            hits: list[tuple[int, str]] = []
            for m in POD_URI_RE.finditer(user_prompt or ""):
                uri = m.group(0)
                if not uri.startswith("datapod:image:") or uri in seen_uris:
                    continue
                seen_uris.add(uri)
                hits.append((m.end(), uri))
            pod_store = None
            for end_pos, uri in hits[-IMAGE_POD_RECENT_LIMIT:]:
                if pod_store is None:
                    pod_store = PodStore()
                pod = pod_store.get(uri)
                if pod is None:
                    continue
                rel = (pod.metadata or {}).get("stored_path")
                if not rel:
                    continue
                abs_path = str(Path(get_repo_root()) / rel)
                if abs_path in seen_paths:
                    continue
                seen_paths.add(abs_path)
                pod_image_inserts.append((end_pos, abs_path))
        except Exception as e:
            logger.error("[%s] Error resolving pod image URIs: %s", agent.name, e)
            pod_image_inserts = []

        msg = [
            {"role": "system", "content": system_prompt or f"[{agent.name}] Error forming system prompt."},
            {"role": "user", "content": user_prompt or f"[{agent.name}] Error forming user prompt."},
        ]

        if pod_image_inserts or legacy_image_paths:
            user_text = msg[1]["content"]
            blocks: list[dict] = []
            cursor = 0
            for end_pos, abs_path in sorted(pod_image_inserts):
                seg = user_text[cursor:end_pos]
                if seg:
                    blocks.append({"type": "input_text", "text": seg})
                blocks.append({"type": "image_path", "path": abs_path})
                cursor = end_pos
            tail = user_text[cursor:]
            if tail:
                blocks.append({"type": "input_text", "text": tail})
            for p in legacy_image_paths:
                blocks.append({"type": "image_path", "path": p})
            msg[1]["content"] = blocks
        return msg

    def get_system_prompt(self, agent, message=None, entity_injection_keys: set[str] | None = None) -> str:
        system_prompt_template = agent.config.get("prompts", {}).get("system", "")
        if not system_prompt_template:
            logger.error("[%s] No system prompt found.", agent.name)
            raise PromptRenderError(
                f"[{agent.name}] Missing system prompt. Check config 'prompts.system'."
            )

        prompt_injections = agent.config.get("system_context_items", [])
        system_context = (
            agent.components.context_injector.generate_injections_block(
                agent, prompt_injections, message, entity_injection_keys or set()
            )
            if prompt_injections is not None
            else None
        )

        try:
            template = _jinja_env.from_string(system_prompt_template)
            return template.render(**(system_context or {}))
        except Exception as e:
            logger.error("[%s] ERROR while rendering system prompt: %s", agent.name, e)
            logger.error("[%s] Context available for rendering: %s", agent.name, list((system_context or {}).keys()))
            logger.debug("[%s] system prompt render exception details", agent.name, exc_info=True)
            raise

    # Reserved blackboard slot maintained by MultiAgentManager for per-agent
    # runtime injections (see MultiAgentManager._RUNTIME_INJECTIONS_BB_KEY).
    # Append-only list[str] per agent name; each item is a sender-wrapped
    # block of text that should be visible to the agent on its NEXT activation
    # and every subsequent one (chat-style accumulation, never cleared).
    _RUNTIME_INJECTIONS_BB_KEY = "_runtime_injections"

    def _append_runtime_injections(self, agent, user_prompt: str) -> str:
        """If anything has been delivered to the agent's runtime-injection slot
        via the manager mailbox, append it to the rendered user prompt.

        Sender owns the framing (``+++++ Latest instruction from user +++ ...``)
        — this just concatenates whatever's there, in posting order. Never
        clears the slot; subsequent activations see the same accumulated
        history, like chat messages.
        """
        try:
            store = agent.blackboard.get_state_value(self._RUNTIME_INJECTIONS_BB_KEY) or {}
        except Exception:
            return user_prompt or ""
        if not isinstance(store, dict):
            return user_prompt or ""
        items = store.get(agent.name)
        if not isinstance(items, list) or not items:
            return user_prompt or ""
        addition = "\n\n".join(str(x) for x in items if isinstance(x, str) and x.strip())
        if not addition:
            return user_prompt or ""
        base = (user_prompt or "").rstrip()
        return f"{base}\n\n{addition}\n" if base else f"{addition}\n"

    def get_user_prompt(self, agent, message=None, entity_injection_keys: set[str] | None = None) -> str:
        user_prompt_template = agent.config.get("prompts", {}).get("user", "")
        if not user_prompt_template:
            logger.error("[%s] No user prompt found.", agent.name)
            return f"No user prompt available for {agent.name}."
        prompt_injections = agent.config.get("user_context_items", [])

        if prompt_injections is not None:
            try:
                user_context = agent.components.context_injector.generate_injections_block(
                    agent, prompt_injections, message, entity_injection_keys or set()
                )
            except Exception as e:
                logger.error("[%s] Error generating injections: %s", agent.name, e)
                logger.debug("[%s] user context generation exception details", agent.name, exc_info=True)
                raise
        else:
            user_context = {}

        try:
            return agent.components.entity_injector.render_user_prompt_with_entities(
                agent=agent,
                message=message,
                agent_name=agent.name,
                user_prompt_template=user_prompt_template,
                user_context=user_context,
                prompt_injections=prompt_injections or [],
                entity_injection_keys=entity_injection_keys or set(),
            )
        except Exception as e:
            logger.error("[%s] ERROR while rendering user prompt: %s", agent.name, e)
            logger.debug("[%s] user prompt render exception details", agent.name, exc_info=True)
            raise

