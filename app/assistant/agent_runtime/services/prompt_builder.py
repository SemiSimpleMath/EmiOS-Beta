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

        if not system_prompt:
            logger.error("[%s] Error forming the system prompt.", agent.name)
        if not user_prompt:
            logger.error("[%s] Error forming the user prompt.", agent.name)

        emi_image_refs: list[str] = []
        try:
            seen_paths: set[str] = set()
            pat = re.compile(r"\[emi_image:\s*([^\]]+?)\s*\]")
            for m in pat.finditer(user_prompt or ""):
                p = (m.group(1) or "").strip()
                if p and p not in seen_paths:
                    emi_image_refs.append(p)
                    seen_paths.add(p)
            pat_legacy = re.compile(r"\[mcp_image_path:\s*([^\]]+?)\s*\]")
            for m in pat_legacy.finditer(user_prompt or ""):
                p = (m.group(1) or "").strip()
                if p and p not in seen_paths:
                    emi_image_refs.append(p)
                    seen_paths.add(p)
            user_prompt = pat.sub("", user_prompt or "")
            user_prompt = pat_legacy.sub("", user_prompt or "")

            # New image-pod path: when the prompt mentions
            # ``datapod:image:<sha>``, resolve to the on-disk file via
            # PodStore so the multimodal call sees the actual pixels.
            # The URI itself is left in the text — the agent uses it as a
            # handle for tool calls (send_email pod_ids, etc.).
            #
            # Recent_history can carry many image URIs from old turns; we
            # bound to the last ``IMAGE_POD_RECENT_LIMIT`` *unique* URIs
            # by position (i.e. the most recent ones in the rendered
            # prompt) so token cost stays predictable.
            from app.assistant.pod_store.pod_uri import POD_URI_RE
            from app.assistant.pod_store.pod_store import PodStore
            from app.assistant.utils.path_utils import get_repo_root
            IMAGE_POD_RECENT_LIMIT = 4
            seen_uris: set[str] = set()
            uri_hits: list[str] = []
            for m in POD_URI_RE.finditer(user_prompt or ""):
                uri = m.group(0)
                if not uri.startswith("datapod:image:") or uri in seen_uris:
                    continue
                seen_uris.add(uri)
                uri_hits.append(uri)
            recent_uris = uri_hits[-IMAGE_POD_RECENT_LIMIT:]
            pod_store = None
            for uri in recent_uris:
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
                emi_image_refs.append(abs_path)
                seen_paths.add(abs_path)
        except Exception as e:
            logger.error("[%s] Error parsing image markers in prompt: %s", agent.name, e)
            logger.debug("[%s] image marker parse exception details", agent.name, exc_info=True)
            emi_image_refs = []

        provider = (agent.llm_params or {}).get("llm_provider", "")
        if provider != "gemini":
            system_prompt = normalize_to_ascii(system_prompt)
            user_prompt = normalize_to_ascii(user_prompt)

        msg = [
            {"role": "system", "content": system_prompt or f"[{agent.name}] Error forming system prompt."},
            {"role": "user", "content": user_prompt or f"[{agent.name}] Error forming user prompt."},
        ]

        if emi_image_refs:
            blocks = [{"type": "input_text", "text": msg[1]["content"]}]
            for p in emi_image_refs:
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

