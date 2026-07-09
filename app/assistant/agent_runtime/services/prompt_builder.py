import hashlib
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from jinja2 import ChainableUndefined, Environment, FileSystemLoader, StrictUndefined

from app.assistant.agent_runtime.exceptions import PromptRenderError
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_app_root

logger = get_logger(__name__)

# Shared Jinja2 environment with loader rooted at the agents directory so
# templates can {% import "shared/macros/dayflow_items.j2" %} etc.
_AGENTS_DIR = get_app_root() / "assistant" / "agents"


def _finalize_none_blank(value):
    """A None context value renders as '' — Jinja's default prints the
    literal string 'None', so any fall-through key that resolved to None
    put 'None' into the prompt (137 templates bare-render optional keys;
    context-injection audit C4). Undefined variables are unaffected:
    non-strict already renders them blank, strict still raises."""
    return "" if value is None else value


_jinja_env = Environment(
    loader=FileSystemLoader(str(_AGENTS_DIR)),
    keep_trailing_newline=True,
    finalize=_finalize_none_blank,
)

# Strict variant: undefined template variables RAISE instead of rendering
# as empty string. Opt-in per agent via config `strict_template: true` —
# default for newly authored agents; old agents ratchet over as audited
# (many legacy templates lean on silent-empty optionals).
_strict_jinja_env = Environment(
    loader=FileSystemLoader(str(_AGENTS_DIR)),
    keep_trailing_newline=True,
    undefined=StrictUndefined,
    finalize=_finalize_none_blank,
)


def get_jinja_env_for_agent(agent) -> Environment:
    try:
        strict = bool((agent.config or {}).get("strict_template"))
    except Exception:
        strict = False
    return _strict_jinja_env if strict else _jinja_env


# ── Prompt guards (fragility review #3, 2026-06-12) ──────────────────────
# Blank input does not look like an error to an LLM — it looks like
# CONSERVATIVE JUDGMENT (gates reject everything, matchers match nothing).
# Four judgment agents shipped judging empty input with zero errors before
# these guards existed. Fail loud at the template boundary instead.

def _is_empty_context_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict, tuple, set)):
        return len(value) == 0
    return False


_skeleton_cache: Dict[str, str] = {}

# Skeleton renders simulate "every variable blank". Jinja's DEFAULT
# Undefined raises on attribute access ({{ agent_input.label }} with no
# agent_input at all); ChainableUndefined tolerates attribute chains but
# still raises on method CALLS ({{ agent_input.get('x') }}). The skeleton
# simulation must tolerate both — blank in, blank out.
class _BlankUndefined(ChainableUndefined):
    def __call__(self, *args, **kwargs):
        return self


_skeleton_env = Environment(
    loader=FileSystemLoader(str(_AGENTS_DIR)),
    keep_trailing_newline=True,
    undefined=_BlankUndefined,
)

_SKELETON_UNRENDERABLE = object()


def _skeleton_render(template_str: str):
    """The template rendered with NO context (every variable, attribute
    chain, and method call blank), normalized the same way as the real
    render path. Cached by template hash. Templates using constructs even
    the blank simulation can't survive (e.g. filters that type-check)
    return a sentinel — the skeleton check fails OPEN for those; layer 1
    (required_context_items) remains the hard guard."""
    key = hashlib.sha1(template_str.encode("utf-8")).hexdigest()
    if key not in _skeleton_cache:
        try:
            skeleton = _skeleton_env.from_string(template_str).render()
            _skeleton_cache[key] = skeleton.replace("\n\n", "\n").strip()
        except Exception as e:
            logger.debug("skeleton render unrenderable (%s) — check fails open", e)
            _skeleton_cache[key] = _SKELETON_UNRENDERABLE
    return _skeleton_cache[key]


def enforce_required_context_items(
    agent_name: str, user_context: Optional[dict], required_items,
) -> None:
    """Layer 1: an agent declares which context items its judgment depends
    on (config `required_context_items`); invocation with any of them
    empty refuses loudly instead of judging nothing."""
    for item in required_items or []:
        value = (user_context or {}).get(item)
        if _is_empty_context_value(value):
            raise PromptRenderError(
                f"[{agent_name}] required context item {item!r} resolved "
                f"empty ({type(value).__name__}) — refusing to invoke on "
                f"missing input. Declared in config required_context_items."
            )


def enforce_skeleton_guard(
    agent_name: str, template_str: str, rendered: Optional[str],
) -> None:
    """Layer 2 (generic backstop, zero per-agent config): a rendered user
    prompt identical to the empty-context skeleton means NO data reached
    the template at all — every variable resolved blank. Static templates
    (no Jinja constructs) are exempt by construction."""
    if "{{" not in template_str and "{%" not in template_str:
        return
    skeleton = _skeleton_render(template_str)
    if skeleton is _SKELETON_UNRENDERABLE:
        return
    if (rendered or "").strip() == skeleton:
        raise PromptRenderError(
            f"[{agent_name}] rendered user prompt equals the empty-context "
            f"skeleton — no data reached the template. Check the agent's "
            f"context items and the caller's input."
        )


class PromptBuilder:
    def construct_prompt(self, agent, message=None, entity_injection_keys: set[str] | None = None) -> List[Dict[str, str]]:
        system_prompt = self.get_system_prompt(agent, message, entity_injection_keys)
        user_prompt = self.get_user_prompt(agent, message, entity_injection_keys)
        user_prompt = self._append_runtime_injections(agent, user_prompt, message)

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

        # Prompts go to the provider VERBATIM — UTF-8 end to end. An earlier
        # normalize_to_ascii pass here flattened diacritics ("mökki"→"mokki")
        # and silently deleted emoji/untranslatable characters from every
        # non-Gemini prompt, so the model never saw the user's actual words
        # (removed 2026-07-08, context-injection audit C1).

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
            template = get_jinja_env_for_agent(agent).from_string(system_prompt_template)
            return template.render(**(system_context or {}))
        except Exception as e:
            logger.error("[%s] ERROR while rendering system prompt: %s", agent.name, e)
            logger.error("[%s] Context available for rendering: %s", agent.name, list((system_context or {}).keys()))
            logger.debug("[%s] system prompt render exception details", agent.name, exc_info=True)
            raise

    def _append_runtime_injections(self, agent, user_prompt: str, message=None) -> str:
        """Append mid-task @-messages (delivered via the manager mailbox) to the
        rendered user prompt as a time-ordered, attributed block with explicit
        precedence: a later message SUPERSEDES/amends/adds to anything before it
        (including the original task); a user message outranks system/agent; the
        most recent user message is final.

        Each entry is ``{text, posted_at_utc, from_who}`` (legacy bare strings
        still render, unstamped). Times are LOCAL and use the same formatter as
        chat history, so a steering message reads like a normal turn. Append-only
        — never cleared; subsequent activations see the same accumulated history.
        """
        from app.assistant.manager_runtime.mailbox import _RUNTIME_INJECTIONS_BB_KEY
        from app.assistant.utils.time_utils import format_history_local
        try:
            store = agent.blackboard.get_state_value(_RUNTIME_INJECTIONS_BB_KEY) or {}
        except Exception:
            return user_prompt or ""
        if not isinstance(store, dict):
            return user_prompt or ""
        items = store.get(agent.name)
        if not isinstance(items, list) or not items:
            return user_prompt or ""

        lines: list[str] = []
        for entry in items:
            if isinstance(entry, dict):
                text = str(entry.get("text") or "").strip()
                if not text:
                    continue
                who = str(entry.get("from_who") or "system")
                ts = entry.get("posted_at_utc")
                stamp = ""
                if ts:
                    try:
                        stamp = format_history_local(ts)
                    except Exception:
                        stamp = ""
                lines.append(f"[{stamp}] {who}: {text}" if stamp else f"{who}: {text}")
            elif isinstance(entry, str) and entry.strip():
                lines.append(entry.strip())  # legacy bare string — already sender-framed
        if not lines:
            return user_prompt or ""

        # Baseline the messages below supersede: when this task was dispatched.
        dispatched = ""
        ts0 = getattr(message, "timestamp", None) if message is not None else None
        if ts0:
            try:
                dispatched = format_history_local(ts0)
            except Exception:
                dispatched = ""

        header = ["### Out-of-band messages (delivered after this task started — newest is authoritative)"]
        if dispatched:
            header.append(f"Original task dispatched: {dispatched}.")
        header.append(
            "The messages below arrived mid-task, in time order. A later message "
            "SUPERSEDES, amends, or adds to anything before it (including the original "
            "task) where they conflict. A message from the user outranks system/agent "
            "messages; treat the most recent user message as final."
        )
        block = "\n".join(header) + "\n" + "\n".join(lines)
        base = (user_prompt or "").rstrip()
        return f"{base}\n\n{block}\n" if base else f"{block}\n"

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

        enforce_required_context_items(
            agent.name, user_context, agent.config.get("required_context_items"),
        )

        try:
            rendered = agent.components.entity_injector.render_user_prompt_with_entities(
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

        enforce_skeleton_guard(agent.name, user_prompt_template, rendered)
        return rendered

