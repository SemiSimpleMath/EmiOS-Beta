import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from app.services.llm_factory import LLMFactory, resolve_tier
from app.configs.llm_classes_dict import _key_is_present, _PROVIDER_KEY_ENV
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def _is_provider_available(provider: str) -> bool:
    """Check if a provider has credentials configured.

    Thin alias over the provider registry's _key_is_present. This used to be a
    hand-rolled if/elif chain that needed extending for every new provider and
    had already drifted from the registry's rules — it tested prefixes where
    the registry tested exact matches, so the two disagreed on values like
    "your_opencode_api_key_here". Registry-driven now, so a provider added to
    llm_classes_dict is understood here with no further edits.
    """
    return _key_is_present(provider)


# Warn once per (requested provider, fallback provider, tier). Without this
# the fallback emits a WARNING on literally every agent call, which is what
# buried the real errors in the log.
_reroute_warned: set[tuple[str, str, str]] = set()


def _provider_preference_order(provider: str) -> tuple[str, ...]:
    """Providers to try, most-preferred first, excluding *provider* itself.

    DEFAULT_LLM_PROVIDER is tried first so this agrees with the rerouting
    LLMFactory.resolve_effective_params already performs. This used to be a
    hardcoded tuple, so the correct provider was only ever chosen by accident
    of ordering — and a deployment whose default was not listed first would
    silently land on a different provider than the factory picked.
    """
    # Explicit preference first, then every other provider the registry knows
    # about. The preference cannot be derived — it is a judgement about which
    # provider to reach for — but the SET must not be hardcoded, or a provider
    # added to llm_classes_dict is silently unreachable as a fallback.
    _preferred = ("opencode", "openai", "gemini", "anthropic")
    order = [p for p in _preferred if p in _PROVIDER_KEY_ENV]
    order += [p for p in _PROVIDER_KEY_ENV if p not in order]
    default = os.environ.get("DEFAULT_LLM_PROVIDER", "").strip().lower()
    if default in order:
        order.remove(default)
        order.insert(0, default)
    return tuple(p for p in order if p != provider)


def _available_provider(*, provider: str, model_tier: str, agent_name: str) -> str:
    """The provider to call: *provider*, or when it has no key, the first available one in
    preference order; the tier names its model there. Returns *provider* when none is available,
    so the call fails with a clear error."""
    if _is_provider_available(provider):
        return provider

    for other in _provider_preference_order(provider):
        if not _is_provider_available(other):
            continue
        other_engine, _ = resolve_tier(other, model_tier)

        _warn_key = (provider, other, model_tier)
        if _warn_key not in _reroute_warned:
            _reroute_warned.add(_warn_key)
            logger.warning(
                "[%s] Provider '%s' not available, rerouting to '%s' engine='%s' (tier=%s). "
                "Repeats of this pairing log at debug level.",
                agent_name, provider, other, other_engine, model_tier,
            )
        else:
            logger.debug(
                "[%s] Provider '%s' not available, rerouting to '%s' engine='%s' (tier=%s).",
                agent_name, provider, other, other_engine, model_tier,
            )
        return other

    return provider


class LLMClient:
    def __init__(self) -> None:
        self._interfaces_by_agent_id: dict[int, Any] = {}

    def _truncate_terminal_debug(self, value: Any) -> str:
        return str(value or "")

    @staticmethod
    def resolved_params(agent) -> dict:
        """The agent's llm_params with the provider it will call (a provider without a key is
        rerouted) and its tier's model (`engine`) and `reasoning_effort` there."""
        params = dict(agent.llm_params) if isinstance(agent.llm_params, dict) else {}
        provider = params.get("llm_provider")
        model_tier = params.get("model_tier")
        if not provider or not model_tier:
            raise ValueError(f"[{agent.name}] llm_params needs llm_provider and model_tier, got {agent.llm_params!r}")
        params["llm_provider"] = _available_provider(provider=provider, model_tier=model_tier, agent_name=agent.name)
        return LLMFactory.resolve_effective_params(**params)

    def get_llm_interface(self, *, agent) -> Any:
        aid = id(agent)
        cached = self._interfaces_by_agent_id.get(aid)
        if cached is not None:
            return cached

        interface = LLMFactory.get_llm_interface(**self.resolved_params(agent))
        self._interfaces_by_agent_id[aid] = interface
        logger.info("Initialized LLM interface for agent: %s", agent.name)
        return interface

    def resolve_prompt_debug_flags(self, *, agent) -> tuple[bool, bool]:
        if os.environ.get("EMI_PRINT_PROMPTS", "0") == "1":
            return True, True

        config_flags = {}
        try:
            cfg = agent.config.get("prompt_debug", {}) if isinstance(agent.config, dict) else {}
            if isinstance(cfg, dict):
                config_flags = cfg
        except Exception as e:
            logger.error("[%s] Failed reading prompt_debug config: %s", agent.name, e)
            logger.debug("[%s] prompt_debug config exception details", agent.name, exc_info=True)
            config_flags = {}

        system_flag = None
        user_flag = None
        if "system" in config_flags or "print_system" in config_flags:
            system_flag = bool(config_flags.get("system", config_flags.get("print_system")))
        if "user" in config_flags or "print_user" in config_flags:
            user_flag = bool(config_flags.get("user", config_flags.get("print_user")))

        try:
            from app.assistant.user_settings_manager.user_settings import get_settings_manager

            settings_mgr = get_settings_manager()
            pd = settings_mgr.get("prompt_debug", {}) if settings_mgr else {}
            if isinstance(pd, dict):
                default_cfg = pd.get("default", {}) if isinstance(pd.get("default"), dict) else {}
                agents_cfg = pd.get("agents", {}) if isinstance(pd.get("agents"), dict) else {}
                raw_agent_cfg = agents_cfg.get(agent.name)
                agent_cfg = raw_agent_cfg if isinstance(raw_agent_cfg, dict) else {}
                if "system" in agent_cfg:
                    system_flag = bool(agent_cfg.get("system"))
                elif "system" in default_cfg and system_flag is None:
                    system_flag = bool(default_cfg.get("system"))
                if "user" in agent_cfg:
                    user_flag = bool(agent_cfg.get("user"))
                elif "user" in default_cfg and user_flag is None:
                    user_flag = bool(default_cfg.get("user"))
        except Exception as e:
            logger.error("[%s] Failed resolving prompt debug settings override: %s", agent.name, e)
            logger.debug("[%s] prompt debug override exception details", agent.name, exc_info=True)

        return bool(system_flag), bool(user_flag)

    def resolve_llm_result_debug_flag(self, *, agent) -> bool:
        if os.environ.get("EMI_PRINT_LLM_RESULTS", "0") == "1":
            return True

        config_flag = None
        try:
            cfg = agent.config.get("prompt_debug", {}) if isinstance(agent.config, dict) else {}
            if isinstance(cfg, dict) and "results" in cfg:
                config_flag = bool(cfg.get("results"))
        except Exception as e:
            logger.error("[%s] Failed reading llm result debug config: %s", agent.name, e)
            logger.debug("[%s] llm result debug config exception details", agent.name, exc_info=True)
            config_flag = None

        try:
            from app.assistant.user_settings_manager.user_settings import get_settings_manager

            settings_mgr = get_settings_manager()
            pd = settings_mgr.get("prompt_debug", {}) if settings_mgr else {}
            if isinstance(pd, dict):
                default_cfg = pd.get("default", {}) if isinstance(pd.get("default"), dict) else {}
                agents_cfg = pd.get("agents", {}) if isinstance(pd.get("agents"), dict) else {}
                raw_agent_cfg = agents_cfg.get(agent.name)
                agent_cfg = raw_agent_cfg if isinstance(raw_agent_cfg, dict) else {}
                if "results" in agent_cfg:
                    config_flag = bool(agent_cfg.get("results"))
                elif "results" in default_cfg and config_flag is None:
                    config_flag = bool(default_cfg.get("results"))
        except Exception as e:
            logger.error("[%s] Failed resolving llm result debug settings override: %s", agent.name, e)
            logger.debug("[%s] llm result debug override exception details", agent.name, exc_info=True)

        return bool(config_flag)

    """
    LLM orchestration service used by Agent runtime.
    """

    def call_structured_output(
        self,
        *,
        agent,
        messages: List[Dict[str, Any]],
        response_format: Optional[Union[dict, str]] = None,
        use_json: bool = False,
    ) -> Any:
        image_paths: list[str] = []
        try:
            for msg in messages or []:
                content = msg.get("content")
                if isinstance(content, list):
                    for part in content:
                        if (
                            isinstance(part, dict)
                            and part.get("type") == "image_path"
                            and part.get("path")
                        ):
                            image_paths.append(str(part.get("path")))

            params = dict(agent.llm_params or {})

            try:
                task_llm = agent.blackboard.get_state_value("task_llm_params")
            except Exception as e:
                logger.error("[%s] Error reading task_llm_params: %s", agent.name, e)
                logger.debug("[%s] task_llm_params read exception details", agent.name, exc_info=True)
                task_llm = None
            overridden = False
            if isinstance(task_llm, dict):
                for key in ("llm_provider", "model_tier", "temperature", "timeout"):
                    if key in task_llm and task_llm.get(key) is not None:
                        params[key] = task_llm.get(key)
                overridden = any(task_llm.get(k) is not None for k in ("llm_provider", "model_tier"))

            if response_format is not None:
                params["response_format"] = response_format

            if image_paths or overridden:
                # Images go to OpenAI; a task-level provider or tier is resolved afresh, never from
                # the agent's cached interface. resolve_effective_params reroutes a provider without a key.
                if image_paths:
                    params["llm_provider"] = "openai"
                resolved = LLMFactory.resolve_effective_params(**{
                    k: v for k, v in params.items() if k in ("llm_provider", "temperature", "model_tier")
                })
                params.update(llm_provider=resolved["llm_provider"], engine=resolved["engine"],
                              reasoning_effort=resolved["reasoning_effort"])
                llm_interface = LLMFactory.get_llm_interface(**resolved)
            else:
                resolved = self.resolved_params(agent)
                params.update(llm_provider=resolved["llm_provider"], engine=resolved["engine"],
                              reasoning_effort=resolved["reasoning_effort"])
                llm_interface = self.get_llm_interface(agent=agent)

            print_system, print_user = self.resolve_prompt_debug_flags(agent=agent)
            if print_system or print_user:
                for msg in messages:
                    role = msg.get("role")
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        parts = []
                        for part in content:
                            if isinstance(part, str):
                                parts.append(part)
                                continue
                            if not isinstance(part, dict):
                                continue
                            ptype = part.get("type")
                            if ptype in ("input_text", "text"):
                                parts.append(part.get("text") or "")
                            elif ptype in ("input_image", "image_url", "image_path", "image_base64"):
                                parts.append("[image]")
                            else:
                                parts.append(f"[{ptype or 'block'}]")
                        content = "\n".join([p for p in parts if p])
                    if role == "system" and print_system:
                        logger.model_system("SYSTEM PROMPT %s\n%s\n%s\n%s\n%s",
                                     agent.name, "=" * 60,
                                     self._truncate_terminal_debug(content), "=" * 60, "")
                    elif role == "user" and print_user:
                        logger.model_user("USER PROMPT %s\n%s\n%s\n%s\n%s",
                                     agent.name, "=" * 60,
                                     self._truncate_terminal_debug(content), "=" * 60, "")

            engine = params.get("engine") or "unknown"
            _total_chars = sum(
                len(p.get("text") or "") if isinstance(p, dict) else len(str(p or ""))
                for msg in (messages or [])
                for p in (msg.get("content") if isinstance(msg.get("content"), list) else [msg.get("content") or ""])
            )
            logger.info("[%s] LLM request engine=%s input_tokens≈%d", agent.name, engine, _total_chars // 4)

            # Set call-context for the telemetry logger (llm_call_log).
            # Stays scoped to this call via try/finally so nested calls
            # inherit / restore correctly.
            from app.services.llm_call_logger import set_current_call_context
            try:
                _scope_ctx = agent.blackboard.get_state_value("scope_context")
            except Exception:
                _scope_ctx = None
            _scope_id = None
            if _scope_ctx is not None:
                _scope_id = getattr(_scope_ctx, "scope_id", None) or (
                    _scope_ctx.get("scope_id") if isinstance(_scope_ctx, dict) else None
                )
            _prev_ctx = set_current_call_context(
                agent_name=agent.name,
                caller_scope_id=_scope_id,
            )
            # The brain's agents: the exact prompts and result, for the /brain page.
            from datetime import datetime, timezone
            from app.assistant.subconscious.brain_trace import record_call
            _started = datetime.now(timezone.utc)
            try:
                from app.services.llm_resilience import retry_transient

                def _invoke(msgs):
                    return llm_interface.structured_output(msgs, use_json=use_json, **params)

                # R2: bounded retry on TRANSIENT infra failures only (the classifier is the SSOT),
                # then one re-ask if a Pydantic-model output fails validation. Both fail loud if they
                # don't recover — retry/re-ask buy resilience, never silence.
                response = retry_transient(lambda: _invoke(messages), label=f"{agent.name}:{engine}")
                response = self._revalidate_or_reask(
                    agent=agent,
                    response=response,
                    response_format=response_format,
                    invoke=_invoke,
                    messages=messages,
                    engine=engine,
                )
            except Exception as exc:
                record_call(agent_name=agent.name, messages=messages, engine=engine, started=_started, error=exc)
                raise
            finally:
                set_current_call_context(**_prev_ctx)
            record_call(agent_name=agent.name, messages=messages, engine=engine, started=_started, result=response)
            return response
        except Exception as e:
            logger.error("[%s] LLM call failed: %s", agent.name, e)
            logger.debug("[%s] LLM call exception details", agent.name, exc_info=True)
            raise
        finally:
            # Auto-clean ephemeral image paths (legacy chat-upload temps,
            # MCP playwright screenshots) but never touch persistent files
            # under data/ — that directory holds the canonical pod store
            # and the wiki vault is its own location entirely. Deleting a
            # pod's PNG breaks every subsequent reference to that pod.
            try:
                from app.assistant.utils.path_utils import get_repo_root
                persistent_root = (get_repo_root() / "data").resolve()
            except Exception:
                persistent_root = None
            for path in image_paths:
                try:
                    if not path or not os.path.exists(path):
                        continue
                    if persistent_root is not None:
                        try:
                            if Path(path).resolve().is_relative_to(persistent_root):
                                continue
                        except Exception:
                            pass
                    os.remove(path)
                except Exception as e:
                    logger.error("[%s] Failed to cleanup image path '%s': %s", agent.name, path, e)
                    logger.debug("[%s] image cleanup exception details", agent.name, exc_info=True)

    def _revalidate_or_reask(self, *, agent, response, response_format, invoke, messages, engine):
        """R2: one bounded re-ask when a Pydantic-model structured output fails validation.

        Every provider returns a dict here (OpenAI's parse strategy returns the validated model's
        model_dump(), so its dicts re-validate trivially; Gemini and Anthropic return raw dicts that
        this gate actually screens). On a validation failure we re-ask ONCE with the validation error
        fed back, then return whatever that produces. A still-bad result is returned unchanged so the
        normal downstream validation fails loud — no new masking. The return SHAPE is never changed
        (dict stays a dict); model_validate here is only a gate.
        """
        from pydantic import BaseModel, ValidationError

        if not (isinstance(response_format, type) and issubclass(response_format, BaseModel)):
            return response  # dict-schema / json / str outputs: nothing to validate against
        if not isinstance(response, dict):
            return response  # already a model instance (e.g. OpenAI) — nothing to gate on

        try:
            response_format.model_validate(response)
            return response
        except ValidationError as ve:
            logger.warning(
                "[%s] structured output failed schema validation; re-asking once: %s", agent.name, ve
            )
            correction = {
                "role": "user",
                "content": (
                    "Your previous response did not match the required JSON schema and failed "
                    f"validation with these errors:\n{ve}\n\n"
                    "Respond again with ONLY a single valid JSON object that matches the schema "
                    "exactly — no markdown, no commentary."
                ),
            }
            from app.services.llm_resilience import retry_transient
            reask_messages = list(messages) + [correction]
            return retry_transient(lambda: invoke(reask_messages), label=f"{agent.name}:{engine}:reask")

