import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from app.assistant.agent_runtime.exceptions import QuotaExhaustedError
from app.services.llm_factory import LLMFactory
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

# Provider availability check + tier-based fallback from configs/model_tiers.yaml.
_model_tiers_cache: dict | None = None

def _load_model_tiers() -> dict:
    """Load tier → provider → engine mapping from configs/model_tiers.yaml."""
    global _model_tiers_cache
    if _model_tiers_cache is not None:
        return _model_tiers_cache
    try:
        import yaml
        from app.assistant.utils.path_utils import get_repo_root
        path = get_repo_root() / "configs" / "model_tiers.yaml"
        if path.exists():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            _model_tiers_cache = data.get("tiers") or {}
            return _model_tiers_cache
    except Exception as e:
        logger.warning("Failed to load model_tiers.yaml: %s", e)
    _model_tiers_cache = {}
    return _model_tiers_cache


def _is_provider_available(provider: str) -> bool:
    """Check if a provider has credentials configured."""
    if provider == "openai":
        key = os.environ.get("OPENAI_API_KEY", "").strip()
        return bool(key) and not key.startswith("your_") and not key.startswith("sk-placeholder")
    elif provider == "gemini":
        key = os.environ.get("GOOGLE_API_KEY", "").strip()
        return bool(key) and not key.startswith("your_") and not key.startswith("placeholder")
    elif provider == "anthropic":
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        return bool(key) and not key.startswith("your_") and not key.startswith("placeholder")
    return False


def _resolve_provider_fallback(
    *, params: dict, provider: str, engine: str, model_tier: str, agent_name: str,
) -> tuple[dict, str, str]:
    """If the preferred provider isn't available, fall back to one that is."""
    if _is_provider_available(provider):
        return params, provider, engine

    tiers = _load_model_tiers()

    # Find a fallback provider.
    for fallback_provider in ("openai", "gemini", "anthropic"):
        if fallback_provider == provider:
            continue
        if not _is_provider_available(fallback_provider):
            continue

        # Resolve the equivalent engine from model_tiers.yaml.
        tier = model_tier.strip().lower() if model_tier else "mini"
        tier_cfg = tiers.get(tier) or tiers.get("mini") or {}
        fallback_engine = str(tier_cfg.get(fallback_provider) or "").strip()
        if not fallback_engine:
            # Last resort hardcoded defaults.
            _defaults = {"openai": "gpt-5.4-mini", "gemini": "gemini-3-flash-preview", "anthropic": "claude-3-5-haiku-20241022"}
            fallback_engine = _defaults.get(fallback_provider, "gpt-5-mini")

        logger.warning(
            "[%s] Provider '%s' not available, falling back to '%s' engine='%s' (tier=%s).",
            agent_name, provider, fallback_provider, fallback_engine, tier,
        )
        params = dict(params)
        params["llm_provider"] = fallback_provider
        params["engine"] = fallback_engine
        return params, fallback_provider, fallback_engine

    # No fallback available — return original and let it fail with a clear error.
    return params, provider, engine


class LLMClient:
    def __init__(self) -> None:
        self._interfaces_by_agent_id: dict[int, Any] = {}

    def _truncate_terminal_debug(self, value: Any) -> str:
        return str(value or "")

    @staticmethod
    def _normalize_gpt5_params(*, agent_name: str, params: Dict[str, Any], source: str) -> Dict[str, Any]:
        normalized = dict(params or {})
        engine = normalized.get("engine")
        if isinstance(engine, str) and engine.strip().lower().startswith("gpt-5"):
            if "temperature" in normalized:
                logger.info(
                    "[%s] Omitting temperature for GPT-5 model (%s) from %s.",
                    agent_name,
                    engine,
                    source,
                )
                normalized.pop("temperature", None)
        return normalized

    def get_llm_interface(self, *, agent) -> Any:
        aid = id(agent)
        cached = self._interfaces_by_agent_id.get(aid)
        if cached is not None:
            return cached

        raw_params = agent.llm_params if isinstance(agent.llm_params, dict) else {}
        params = self._normalize_gpt5_params(agent_name=agent.name, params=raw_params, source="agent llm_params")
        provider = params.get("llm_provider")
        engine = params.get("engine")
        if not provider or not engine:
            logger.error(
                "[%s] Missing required llm_params. Expected keys: llm_provider, engine. Got: %r",
                agent.name,
                agent.llm_params,
            )
            raise ValueError(f"[{agent.name}] Missing required llm_params (llm_provider, engine).")

        # Provider fallback: if the preferred provider isn't available (no API key),
        # fall back to an available provider using the model_tier for equivalence.
        params, provider, engine = _resolve_provider_fallback(
            params=params, provider=provider, engine=engine,
            model_tier=raw_params.get("model_tier", ""),
            agent_name=agent.name,
        )
        # Update the agent's llm_params so downstream calls also use the fallback.
        if isinstance(agent.llm_params, dict):
            agent.llm_params["llm_provider"] = provider
            agent.llm_params["engine"] = engine

        interface = LLMFactory.get_llm_interface(**params)
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

    def check_for_quota_error(self, *, agent_name: str, response_text: Any) -> None:
        if not response_text:
            return

        response_str = str(response_text).lower()
        quota_keywords = [
            "quota exceeded",
            "rate limit exceeded",
            "insufficient quota",
            "quota exhausted",
        ]

        for keyword in quota_keywords:
            if keyword in response_str:
                logger.critical("❌ LLM QUOTA ERROR DETECTED in agent: %s", agent_name)
                logger.critical("   Response preview: %s", str(response_text))
                logger.critical("   Keyword: '%s'", keyword)
                raise QuotaExhaustedError(
                    f"[{agent_name}] LLM quota exhausted (matched '{keyword}')."
                )

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
            provider_overridden = False
            if isinstance(task_llm, dict):
                for key in ("llm_provider", "engine", "temperature", "timeout"):
                    if key in task_llm and task_llm.get(key) is not None:
                        params[key] = task_llm.get(key)
                if "llm_provider" in task_llm and task_llm.get("llm_provider") is not None:
                    provider_overridden = True

            params = self._normalize_gpt5_params(agent_name=agent.name, params=params, source="merged llm params")

            if response_format is not None:
                params["response_format"] = response_format

            if image_paths:
                params["llm_provider"] = "openai"
                params["engine"] = "gpt-5.2"
                llm_interface = LLMFactory.get_llm_interface(llm_provider="openai")
            elif provider_overridden:
                # Provider changed at runtime — build a fresh interface, do not use the cached one.
                # resolve_effective_params handles rerouting if the override provider has no key.
                resolved = LLMFactory.resolve_effective_params(**{
                    k: v for k, v in params.items()
                    if k in ("llm_provider", "engine", "temperature", "api_key", "model_tier")
                })
                params["llm_provider"] = resolved["llm_provider"]
                params["engine"] = resolved["engine"]
                llm_interface = LLMFactory.get_llm_interface(**resolved)
            else:
                llm_interface = self.get_llm_interface(agent=agent)
                # get_llm_interface may have changed agent.llm_params via provider
                # fallback.  Sync the local params copy so the correct engine name
                # reaches the actual API call (e.g. "gpt-5-mini" instead of
                # "gemini-3-flash-preview" when Gemini key is missing).
                if isinstance(agent.llm_params, dict):
                    params["llm_provider"] = agent.llm_params.get("llm_provider", params.get("llm_provider"))
                    params["engine"] = agent.llm_params.get("engine", params.get("engine"))

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
            try:
                response = llm_interface.structured_output(
                    messages,
                    use_json=use_json,
                    **params,
                )
            finally:
                set_current_call_context(**_prev_ctx)
            self.check_for_quota_error(agent_name=agent.name, response_text=response)
            return response
        except Exception as e:
            logger.error("[%s] LLM call failed: %s", agent.name, e)
            logger.debug("[%s] LLM call exception details", agent.name, exc_info=True)
            self.check_for_quota_error(agent_name=agent.name, response_text=str(e))
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

