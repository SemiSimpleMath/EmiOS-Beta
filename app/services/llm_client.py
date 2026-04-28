#llm_client.py
from typing import List, Dict, Any, Optional

import base64
import mimetypes
from pathlib import Path
import re

from openai import OpenAI

import os
import threading
import sys

from app.assistant.utils.logging_config import get_logger
from app.assistant.performance.performance_monitor import performance_monitor
logger = get_logger(__name__)


def _is_placeholder_api_key(value: Any) -> bool:
    if not isinstance(value, str):
        return True
    v = value.strip().lower()
    if not v:
        return True
    placeholders = {
        "dummy_api_key",
        "your_api_key_here",
        "change_me",
        "changeme",
        "none",
        "null",
    }
    return v in placeholders


def _repo_root_from_here() -> Path:
    # app/services/llm_client.py -> repo root
    return Path(__file__).resolve().parents[2]


def _resolve_local_upload_image_path(path: str) -> str:
    """
    Deterministically resolve local image references.

    In many places, agents only see the *filename* (e.g. "mcp_....png") in summaries.
    The system is responsible for expanding that into an absolute path under uploads/temp/.
    """
    s = (path or "").strip()
    if not s:
        return s

    p = Path(s)
    if p.is_absolute():
        return str(p)

    # Filename-only or relative: assume it lives under repo uploads/temp/.
    fname = p.name
    root = _repo_root_from_here()
    cand = (root / "uploads" / "temp" / fname).resolve()
    if cand.exists():
        return str(cand)

    # Fallback: try repo_root/<relative> if caller passed something like "uploads/temp/foo.png".
    cand2 = (root / p).resolve()
    if cand2.exists():
        return str(cand2)

    # Keep original (so caller can see what failed).
    return s


def _image_file_to_data_uri(path: str) -> str:
    path = _resolve_local_upload_image_path(path)
    p = Path(path)
    data = p.read_bytes()
    mime, _ = mimetypes.guess_type(str(p))
    if not mime:
        # Default to PNG since Playwright screenshots are often PNG.
        mime = "image/png"
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _normalize_openai_responses_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Normalize legacy chat-style message content into the OpenAI Responses API
    "content blocks" format, while preserving existing text-only behavior.

    Supports:
    - content: "string"  (left as-is)
    - content: [{"type":"text","text":...}]  -> input_text
    - content: [{"type":"image_url","image_url":{"url":...}}] -> input_image
    - content: [{"type":"image_path","path":"..."}] -> input_image (data URI)
    - content: [{"type":"image_base64","data":"...","mime":"image/png"}] -> input_image (data URI)
    - content: [{"type":"input_text",...}] / [{"type":"input_image",...}] (passed through)
    """
    normalized: List[Dict[str, Any]] = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue

        out = dict(msg)
        content = out.get("content")

        # Common case: plain string content (keep for backwards compatibility).
        if content is None or isinstance(content, str):
            normalized.append(out)
            continue

        # Responses API prefers a list of content blocks for multimodal.
        if isinstance(content, list):
            blocks: List[Dict[str, Any]] = []
            for part in content:
                if isinstance(part, str):
                    blocks.append({"type": "input_text", "text": part})
                    continue
                if not isinstance(part, dict):
                    continue

                ptype = part.get("type")

                if ptype in ("input_text",):
                    blocks.append({"type": "input_text", "text": part.get("text") or ""})
                    continue
                if ptype in ("text",):
                    blocks.append({"type": "input_text", "text": part.get("text") or ""})
                    continue

                if ptype in ("input_image",):
                    image_url = part.get("image_url") or part.get("url")
                    if image_url:
                        blocks.append({"type": "input_image", "image_url": image_url})
                    continue
                if ptype in ("image_url",):
                    # Chat-completions style: {"image_url": {"url": "..."}}
                    image_url_obj = part.get("image_url")
                    if isinstance(image_url_obj, dict) and image_url_obj.get("url"):
                        blocks.append({"type": "input_image", "image_url": image_url_obj["url"]})
                    elif isinstance(image_url_obj, str):
                        blocks.append({"type": "input_image", "image_url": image_url_obj})
                    continue

                if ptype == "image_path":
                    path = part.get("path")
                    if path:
                        try:
                            resolved = _resolve_local_upload_image_path(str(path))
                            blocks.append({"type": "input_image", "image_url": _image_file_to_data_uri(resolved)})
                        except FileNotFoundError:
                            # Do NOT crash the pipeline if an image path is missing.
                            # Convert to a text hint and continue text-only.
                            blocks.append(
                                {
                                    "type": "input_text",
                                    "text": f"[image load failed: file not found: {path}]",
                                }
                            )
                        except Exception as e:
                            blocks.append(
                                {
                                    "type": "input_text",
                                    "text": f"[image load failed: {path} ({e})]",
                                }
                            )
                    continue

                if ptype == "image_base64":
                    data = part.get("data") or ""
                    mime = part.get("mime") or "image/png"
                    if data:
                        blocks.append({"type": "input_image", "image_url": f"data:{mime};base64,{data}"})
                    continue

                # Unknown part type: best-effort stringify as text so we don't drop signal.
                try:
                    import json as _json
                    blocks.append({"type": "input_text", "text": _json.dumps(part, ensure_ascii=True)})
                except Exception:
                    blocks.append({"type": "input_text", "text": str(part)})

            out["content"] = blocks
            normalized.append(out)
            continue

        # If some caller passed a dict, stringify it rather than breaking the request.
        try:
            import json as _json
            out["content"] = _json.dumps(content, ensure_ascii=True)
        except Exception:
            out["content"] = str(content)
        normalized.append(out)

    return normalized


def _strip_markdown_code_fences(text: str) -> str:
    """
    Remove surrounding markdown code fences (``` or ```json) if present.
    Best-effort; returns original if no fences found.
    """
    s = (text or "").strip()
    if not s.startswith("```"):
        return s
    # Common pattern: ```json\n{...}\n```
    # Remove first line fence and trailing fence.
    lines = s.splitlines()
    if len(lines) >= 2 and lines[0].startswith("```"):
        # Drop leading fence line
        lines = lines[1:]
        # Drop trailing fence line(s)
        while lines and lines[-1].strip().startswith("```"):
            lines.pop()
        return "\n".join(lines).strip()
    return s


def _parse_first_json_object(text: str) -> Any:
    """
    Parse the first valid JSON value from a string, ignoring any trailing garbage.
    This avoids failures like: `Extra data: line ...` when the model emits multiple
    JSON objects or appends commentary.
    """
    import json as _json

    s = _strip_markdown_code_fences(text)
    s = s.lstrip("\ufeff").strip()  # remove BOM if present
    if not s:
        raise ValueError("empty json text")

    decoder = _json.JSONDecoder()

    # Fast path: try from the beginning (after whitespace).
    try:
        obj, _idx = decoder.raw_decode(s)
        return obj
    except Exception:
        logger.debug("JSON fast-path decode failed, trying best-effort scan", exc_info=True)

    # Best-effort: find first likely JSON start.
    m = re.search(r"[\{\[]", s)
    if not m:
        raise ValueError("no json object/array start found")
    start = m.start()
    obj, _idx = decoder.raw_decode(s[start:])
    return obj


def _extract_response_text(response: Any) -> str:
    """
    Robustly extract text from OpenAI Responses API objects.

    Why:
    - `response.output[0]` is not always the assistant message (it can be a
      reasoning item with `content=None`).
    - Different SDK versions expose `output_text` and/or structured output items.
    """
    # Prefer SDK convenience field when present.
    out_text = getattr(response, "output_text", None)
    if isinstance(out_text, str) and out_text.strip():
        return out_text

    output_items = getattr(response, "output", None)
    if not isinstance(output_items, list):
        raise ValueError("OpenAI response has no output items")

    for item in output_items:
        content = getattr(item, "content", None)
        if not isinstance(content, list):
            continue
        for part in content:
            text = getattr(part, "text", None)
            if isinstance(text, str) and text.strip():
                return text

    raise ValueError("OpenAI response contained no text content in output items")


class OpenAIModelCapabilityNormalizer:
    """
    Normalize model-family behavior in one place.
    """

    def __init__(self, model_name: str):
        if not isinstance(model_name, str) or not model_name.strip():
            raise ValueError("model_name must be a non-empty string")
        self.model_name = model_name.strip()

    @property
    def is_gpt5_family(self) -> bool:
        return self.model_name.startswith("gpt-5")

    def build_base_kwargs(self, *, messages: List[Dict[str, Any]], timeout: int, temperature: Any) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {
            "model": self.model_name,
            "input": messages,
            "timeout": timeout,
        }
        # gpt-5 rejects temperature.
        if not self.is_gpt5_family and temperature is not None:
            kwargs["temperature"] = temperature
        if self.is_gpt5_family:
            kwargs["reasoning"] = {"effort": "medium"}
            kwargs.pop("temperature", None)
        return kwargs


class OpenAIStructuredOutputStrategy:
    route_name = "base"

    def execute(
        self,
        *,
        client: OpenAI,
        caps: OpenAIModelCapabilityNormalizer,
        messages: List[Dict[str, Any]],
        timeout: int,
        temperature: Any,
        response_format: Any,
    ) -> Dict[str, Any]:
        raise NotImplementedError


class OpenAIPromptJsonValidateStrategy(OpenAIStructuredOutputStrategy):
    route_name = "prompt_json_validate"

    def execute(
        self,
        *,
        client: OpenAI,
        caps: OpenAIModelCapabilityNormalizer,
        messages: List[Dict[str, Any]],
        timeout: int,
        temperature: Any,
        response_format: Any,
    ) -> Dict[str, Any]:
        kwargs = caps.build_base_kwargs(messages=messages, timeout=timeout, temperature=temperature)
        response = client.responses.create(**kwargs)
        raw_text = _extract_response_text(response)
        if not isinstance(raw_text, str) or not raw_text.strip():
            raise ValueError("OpenAI response contained no parsable text output")
        obj = _parse_first_json_object(raw_text)
        if not isinstance(obj, dict):
            raise ValueError("Structured output must be a JSON object")
        validated = response_format.model_validate(obj)
        return validated.model_dump()


class OpenAIDirectPydanticParseStrategy(OpenAIStructuredOutputStrategy):
    route_name = "direct_pydantic_parse"

    def execute(
        self,
        *,
        client: OpenAI,
        caps: OpenAIModelCapabilityNormalizer,
        messages: List[Dict[str, Any]],
        timeout: int,
        temperature: Any,
        response_format: Any,
    ) -> Dict[str, Any]:
        from pydantic import BaseModel as _BaseModel  # local import for clarity

        parse_kwargs = caps.build_base_kwargs(messages=messages, timeout=timeout, temperature=temperature)
        parse_kwargs["text_format"] = response_format

        parse_api = getattr(client.responses, "parse", None)
        if not callable(parse_api):
            raise RuntimeError("OpenAI client does not support responses.parse for direct Pydantic output.")

        response = parse_api(**parse_kwargs)
        parsed = getattr(response, "output_parsed", None)
        if parsed is None:
            raise ValueError("OpenAI parse response did not include output_parsed.")
        if isinstance(parsed, _BaseModel):
            return parsed.model_dump()
        if isinstance(parsed, dict):
            return parsed
        raise TypeError(f"OpenAI parse returned unsupported parsed type: {type(parsed)}")


class OpenAIJsonSchemaStrategy(OpenAIStructuredOutputStrategy):
    route_name = "json_schema"

    def __init__(self):
        self.last_usage = None

    def execute(
        self,
        *,
        client: OpenAI,
        caps: OpenAIModelCapabilityNormalizer,
        messages: List[Dict[str, Any]],
        timeout: int,
        temperature: Any,
        response_format: Any,
    ) -> Dict[str, Any]:
        if not isinstance(response_format, dict):
            raise ValueError("JSON schema strategy requires dict response_format")
        if "format" in response_format:
            text_cfg = response_format
        else:
            schema = dict(response_format)
            try:
                sanitize_schema(schema)
                schema = inline_refs(schema)
                sanitize_schema(schema)
            except Exception as e:
                logger.debug("JSON schema sanitization failed, using original schema: %s", e, exc_info=True)
            text_cfg = {
                "format": {
                    "type": "json_schema",
                    "name": "EmiStructuredOutput",
                    "schema": schema,
                    "strict": True,
                }
            }

        kwargs = caps.build_base_kwargs(messages=messages, timeout=timeout, temperature=temperature)
        kwargs["text"] = text_cfg
        response = client.responses.create(**kwargs)
        self.last_usage = getattr(response, "usage", None)
        raw_text = _extract_response_text(response)
        if not isinstance(raw_text, str) or not raw_text.strip():
            raise ValueError("OpenAI response contained no parsable text output")
        result = _parse_first_json_object(raw_text)
        if not isinstance(result, dict):
            raise ValueError("Structured output must be a JSON object")
        return result


class LLMQuotaExhaustedError(RuntimeError):
    """Raised when the LLM API returns a quota/billing error."""


def _handle_fatal_quota_error(error_msg: str, context: str = ""):
    """
    Handle LLM quota errors by raising a loud, catchable exception.

    Previous behaviour was os._exit(1), which bypasses all exception handlers
    and makes batch pipelines impossible to run — any transient rate-limit
    error that contains "quota" would silently kill the process.
    """
    logger.critical("LLM Quota Exhausted — context: %s — error: %s", context, error_msg)
    raise LLMQuotaExhaustedError(
        f"LLM Quota Exhausted | context={context} | {error_msg}"
    )

# print("\nthe key is: ", os.environ.get('OPENAI_API_KEY'))  # Should return your API key

# Removed: Time checking moved to maintenance manager for surgical control


class BaseLLMProvider:
    def send_query(self, messages, **send_request):
        raise NotImplementedError("This method should be implemented by subclasses.")

    def send_function_query(self, messages, **send_request):
        raise NotImplementedError("This method should be implemented by subclasses.")

    def build_messages(self, **send_params):
        raise NotImplementedError("This method should be implemented by subclasses.")

    def stream_response_to_socket(self, messages, socket_id, user_id, audio_output, **kwargs):
        raise NotImplementedError("This method should be implemented by subclasses.")

class OpenAILLM(BaseLLMProvider):
    _instance = None  # Singleton instance
    _instance_lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        # Thread-safe singleton init: parallel web_managers can create LLMs concurrently.
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = super(OpenAILLM, cls).__new__(cls)
                    cls._instance._init_once(*args, **kwargs)
        else:
            # If another thread created the instance but hasn't fully initialized yet,
            # ensure we don't return a partially constructed object.
            if not hasattr(cls._instance, "client"):
                with cls._instance_lock:
                    cls._instance._init_once(*args, **kwargs)
        return cls._instance

    def _init_once(self, engine="gpt-4.1-mini", temperature=0.1, **kwargs):
        """Initializes OpenAILLM once."""
        if hasattr(self, "client"):  # Prevent reinitialization
            return

        if "api_key" in kwargs:
            raise ValueError("Passing api_key is not supported. Set OPENAI_API_KEY in environment.")

        self.api_key = os.environ.get('OPENAI_API_KEY')
        if _is_placeholder_api_key(self.api_key):
            raise ValueError("OPENAI_API_KEY is missing or invalid. Set a real API key in environment.")
        self.engine = engine
        self.temperature = temperature
        self.client = OpenAI(api_key=self.api_key)  # Shared OpenAI client instance

        # Apply additional settings if needed
        for key, value in kwargs.items():
            setattr(self, key, value)


    def structured_output(self, messages, **send_params):
        """Public entrypoint — runs _structured_output_once with retry-on-timeout
        backoff. OpenAI structured-output calls occasionally hang at ~4 min even
        though they eventually succeed; retrying with a shorter first attempt
        catches those hangs and usually succeeds on retry.
        """
        original_timeout = send_params.get('timeout', 240)
        # Progressive attempts: short first (catches OpenAI structured-output
        # slow-mode calls), then longer, then the caller's full timeout.
        #
        # If the caller already budgeted a large timeout (>=180s), they expect
        # slow calls — skip the fast-retry ladder to avoid burning 60s+120s
        # on attempts that consistently need the full budget. Fact_extractor
        # on gpt-5.4 is a typical case here.
        try:
            large_timeout_threshold = int(os.environ.get("LLM_STRUCTURED_OUTPUT_SKIP_RETRY_ABOVE", "180"))
        except ValueError:
            large_timeout_threshold = 180
        if original_timeout >= large_timeout_threshold:
            attempt_timeouts = [original_timeout]
        else:
            try:
                short = min(original_timeout, int(os.environ.get("LLM_STRUCTURED_OUTPUT_FAST_TIMEOUT", "60")))
                medium = min(original_timeout, int(os.environ.get("LLM_STRUCTURED_OUTPUT_MEDIUM_TIMEOUT", "120")))
            except ValueError:
                short, medium = 60, 120
            attempt_timeouts = [t for t in (short, medium, original_timeout) if t > 0]
            # Deduplicate while preserving order
            seen = set()
            attempt_timeouts = [t for t in attempt_timeouts if not (t in seen or seen.add(t))]

        last_exc: Optional[Exception] = None
        for idx, t in enumerate(attempt_timeouts):
            send_params_copy = dict(send_params)
            send_params_copy['timeout'] = t
            try:
                return self._structured_output_once(messages, **send_params_copy)
            except TimeoutError as exc:
                last_exc = exc
                if idx < len(attempt_timeouts) - 1:
                    logger.warning(
                        "LLM structured_output timed out at %ss (attempt %d/%d); retrying with %ss",
                        t, idx + 1, len(attempt_timeouts), attempt_timeouts[idx + 1],
                    )
                    continue
                raise
        if last_exc:
            raise last_exc
        raise RuntimeError("structured_output: no attempts were made")

    def _structured_output_once(self, messages, **send_params):
        messages = _normalize_openai_responses_messages(messages)
        response_format = send_params.get('response_format')
        model = send_params.get('engine') or send_params.get('model') or "gpt-4.1-mini"
        temperature = send_params.get('temperature', None)
        timeout = send_params.get('timeout', 240)
        pydantic_mode = str(send_params.get("openai_pydantic_mode", "direct_parse") or "direct_parse").strip().lower()

        # Check if response_format is None and log an error if so
        if response_format is None:
            logger.error("Response format is None. Please ensure a valid response format is provided.")
            # Optionally, raise an exception or handle it accordingly
            raise ValueError("Invalid response format: None")

        # Log which model is being used with all parameters
        _thread = threading.current_thread()
        logger.info(f"Using model: {model} for structured output, with temperature {temperature}, timeout {timeout}s. thread={_thread.name}({_thread.ident})")

        # Start timing the LLM call
        timer_id = performance_monitor.start_timer('llm_structured_output', f"{model}_{len(messages)}")
        
        try:
            from pydantic import BaseModel as _BaseModel  # local import for clarity
            if not isinstance(model, str) or not model.strip():
                raise ValueError("model must be a non-empty string")
            caps = OpenAIModelCapabilityNormalizer(model_name=model)

            strategy: OpenAIStructuredOutputStrategy
            if isinstance(response_format, type) and issubclass(response_format, _BaseModel):
                if pydantic_mode == "prompt_json_validate":
                    strategy = OpenAIPromptJsonValidateStrategy()
                else:
                    strategy = OpenAIDirectPydanticParseStrategy()
                logger.info(
                    "OpenAI structured_output route=%s model=%s response_format=%s",
                    strategy.route_name,
                    model,
                    getattr(response_format, "__name__", str(response_format)),
                )
            elif isinstance(response_format, dict):
                strategy = OpenAIJsonSchemaStrategy()
                logger.info(
                    "OpenAI structured_output route=%s model=%s prewrapped=%s",
                    strategy.route_name,
                    model,
                    bool("format" in response_format),
                )
            else:
                raise ValueError(f"Invalid response format: {type(response_format)}")
            return strategy.execute(
                client=self.client,
                caps=caps,
                messages=messages,
                timeout=timeout,
                temperature=temperature,
                response_format=response_format,
            )

        except Exception as e:
            # End timing and record error
            performance_monitor.end_timer(timer_id, {
                'status': 'error',
                'model': model,
                'message_count': len(messages),
                'temperature': temperature,
                'timeout': timeout,
                'error': str(e)
            })
            
            logger.error(f"Error processing input function_query: {e}")
            logger.debug("OpenAI structured_output exception details", exc_info=True)
            error_str = str(e).lower()
            
            # FATAL: Quota errors require immediate program exit
            if "quota" in error_str or "insufficient_quota" in error_str:
                _handle_fatal_quota_error(str(e), f"structured_output (model: {model})")

            # Fail loudly: never convert provider errors into plain strings.
            if "timeout" in error_str or "timed out" in error_str:
                raise TimeoutError(f"LLM request timed out after {timeout} seconds") from e
            if "rate limit" in error_str:
                raise RuntimeError("LLM rate limit exceeded") from e
            raise RuntimeError(f"LLM structured_output failed: {e}") from e

    def structured_output_json(self, messages, **send_params):
        messages = _normalize_openai_responses_messages(messages)
        json_schema = send_params.get('response_format')

        model = send_params.get('engine') or send_params.get('model') or "gpt-4.1"
        temperature = send_params.get('temperature')
        timeout = send_params.get('timeout', 120)

        # Allow passing a Pydantic model here too.
        try:
            from pydantic import BaseModel as _BaseModel  # type: ignore
            if isinstance(json_schema, type) and issubclass(json_schema, _BaseModel):
                json_schema = pydantic_to_openai_schema(json_schema)
        except Exception:
            logger.debug("Could not convert Pydantic schema to OpenAI schema", exc_info=True)

        if json_schema is None:
            logger.error("Response format is None. Please ensure a valid response format is provided.")
            raise ValueError("Invalid response format: None")

        # Log which model is being used with all parameters
        logger.info(f"Using model: {model} for structured JSON output, with temperature {temperature}, timeout {timeout}s.")

        # Start timing the LLM call
        timer_id = performance_monitor.start_timer('llm_structured_output_json', f"{model}_{len(messages)}")

        try:
            if not isinstance(model, str) or not model.strip():
                raise ValueError("model must be a non-empty string")
            caps = OpenAIModelCapabilityNormalizer(model_name=model)
            strategy = OpenAIJsonSchemaStrategy()
            event = strategy.execute(
                client=self.client,
                caps=caps,
                messages=messages,
                timeout=timeout,
                temperature=temperature,
                response_format=json_schema if isinstance(json_schema, dict) else {"type": "object"},
            )

            # Extract detailed token usage information from responses API
            usage = getattr(strategy, "last_usage", None)
            if usage:
                prompt_tokens = getattr(usage, 'prompt_tokens', 0)
                completion_tokens = getattr(usage, 'completion_tokens', 0)
                cached_tokens = getattr(usage, 'cached_tokens', 0)
                total_tokens = getattr(usage, 'total_tokens', 0)
                
                # Log detailed token usage
                logger.info(f"Token usage (JSON) - Input: {prompt_tokens}, Output: {completion_tokens}, Cached: {cached_tokens}, Total: {total_tokens}")
            else:
                prompt_tokens = completion_tokens = cached_tokens = total_tokens = None
                logger.warning("No token usage information available in JSON response")

            # End timing and record success
            performance_monitor.end_timer(timer_id, {
                'status': 'success',
                'model': model,
                'message_count': len(messages),
                'temperature': temperature,
                'timeout': timeout,
                'total_tokens': total_tokens,
                'prompt_tokens': prompt_tokens,
                'completion_tokens': completion_tokens,
                'cached_tokens': cached_tokens
            })

            return event

        except Exception as e:
            # End timing and record error
            performance_monitor.end_timer(timer_id, {
                'status': 'error',
                'model': model,
                'message_count': len(messages),
                'temperature': temperature,
                'timeout': timeout,
                'error': str(e)
            })
            
            logger.error(f"Error processing input function_query: {e}")
            error_str = str(e).lower()
            
            # FATAL: Quota errors require immediate program exit
            if "quota" in error_str or "insufficient_quota" in error_str:
                _handle_fatal_quota_error(str(e), f"structured_output_json (model: {model})")

            # Fail loudly: never convert provider errors into plain strings.
            if "timeout" in error_str:
                raise TimeoutError(f"LLM request timed out after {timeout} seconds") from e
            if "rate limit" in error_str:
                raise RuntimeError("LLM rate limit exceeded") from e
            raise RuntimeError(f"LLM structured_output_json failed: {e}") from e

# ---------------------------------------------------------------------------
# Gemini schema utilities — available for manually inlining $ref/$defs or
# stripping unsupported keys.  GeminiLLM now uses response_json_schema
# (with model_json_schema()) which supports $ref/$defs natively.
# ---------------------------------------------------------------------------

def _gemini_inline_refs(schema: dict) -> dict:
    """Inline all $defs/$ref references to produce a flat JSON Schema dict."""
    defs = schema.get("$defs", {})
    if not defs:
        return schema

    def _resolve(obj):
        if isinstance(obj, dict):
            if "$ref" in obj:
                ref_key = obj["$ref"].split("/")[-1]
                resolved = defs.get(ref_key, obj)
                return _resolve(resolved)
            return {k: _resolve(v) for k, v in obj.items() if k != "$defs"}
        if isinstance(obj, list):
            return [_resolve(i) for i in obj]
        return obj

    return _resolve(schema)


def _gemini_clean_schema(obj, _inside_properties: bool = False) -> object:
    """Recursively strip JSON Schema keys unsupported by Gemini's response_schema.

    Removes additionalProperties/$schema at all levels.
    Strips the `title` *metadata* key (injected by Pydantic at every schema node)
    but preserves it when it is a *property name* inside a `properties` mapping.
    Also prunes `required` entries that have no matching key in `properties`.
    """
    _ALWAYS_STRIP = {"additional_properties", "additionalProperties", "$schema"}
    if isinstance(obj, dict):
        cleaned: dict = {}
        for k, v in obj.items():
            if k in _ALWAYS_STRIP:
                continue
            if k == "title" and not _inside_properties:
                continue
            if k == "properties" and isinstance(v, dict):
                cleaned[k] = {
                    pk: _gemini_clean_schema(pv, _inside_properties=False)
                    for pk, pv in v.items()
                }
            else:
                cleaned[k] = _gemini_clean_schema(v, _inside_properties=False)
        if "required" in cleaned and "properties" in cleaned:
            valid = set(cleaned["properties"].keys())
            cleaned["required"] = [r for r in cleaned["required"] if r in valid]
            if not cleaned["required"]:
                del cleaned["required"]
        return cleaned
    if isinstance(obj, list):
        return [_gemini_clean_schema(i, _inside_properties=False) for i in obj]
    return obj


class GeminiLLM(BaseLLMProvider):
    _instance = None  # Singleton instance

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(GeminiLLM, cls).__new__(cls)
            cls._instance._init_once(*args, **kwargs)
        return cls._instance

    def _init_once(self, engine="gemini-1.5-flash", temperature=0.1, **kwargs):
        """Initializes GeminiLLM once using the new google-genai package."""
        if hasattr(self, "client"):
            return

        try:
            from google import genai
            from google.genai import types
            self.genai = genai
            self.types = types
        except ImportError:
            logger.error("google-genai is not installed. Please run 'pip install google-genai'")
            raise

        if "api_key" in kwargs:
            raise ValueError("Passing api_key is not supported. Set GOOGLE_API_KEY in environment.")

        self.api_key = os.environ.get('GOOGLE_API_KEY')
        if _is_placeholder_api_key(self.api_key):
            raise ValueError("GOOGLE_API_KEY is missing or invalid. Set a real API key in environment.")
        
        # New API uses Client instead of configure
        self.client = self.genai.Client(api_key=self.api_key)
        self.engine = engine
        self.temperature = temperature
        
        # Apply additional settings if needed
        for key, value in kwargs.items():
            setattr(self, key, value)

    def _convert_messages_to_contents(self, messages: List[Dict]):
        """Converts OpenAI-style messages to new Gemini contents format."""
        from typing import Tuple, Optional
        system_instruction = None
        contents = []
        
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content")
            
            if role == "system":
                system_instruction = content
            elif role == "user":
                contents.append(content)
            elif role == "assistant":
                # For multi-turn, we'd need to handle this differently
                # For now, append as user context
                pass
                
        # Join all user messages
        combined_content = "\n\n".join(contents) if contents else ""
        return system_instruction, combined_content

    def _permissive_safety_settings(self):
        """Build safety settings that disable content filtering.

        Gemini's default filters false-positive on prompts containing UUIDs,
        hex identifiers, and knowledge-graph entity cards, causing blocked
        responses or hard process crashes.
        """
        categories = [
            self.types.HarmCategory.HARM_CATEGORY_HARASSMENT,
            self.types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
            self.types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
            self.types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
        ]
        return [
            self.types.SafetySetting(
                category=cat,
                threshold=self.types.HarmBlockThreshold.OFF,
            )
            for cat in categories
        ]

    @staticmethod
    def _to_json_schema(response_format) -> dict:
        """Convert a Pydantic class or raw dict into a JSON Schema dict.

        The google-genai SDK's ``response_schema`` parameter silently crashes
        when given a Pydantic class (it tries an internal conversion that
        segfaults on nested ``$ref``/``$defs``).  The documented approach is to
        use ``response_json_schema`` with a plain dict from
        ``BaseModel.model_json_schema()``.
        """
        if isinstance(response_format, type) and hasattr(response_format, "model_json_schema"):
            return response_format.model_json_schema()
        if isinstance(response_format, dict):
            return response_format
        raise TypeError(
            f"response_format must be a Pydantic BaseModel class or a dict, "
            f"got {type(response_format)}"
        )

    def structured_output(self, messages, **send_params):
        response_format = send_params.get('response_format')
        model_name = send_params.get('engine', self.engine)
        temperature = send_params.get('temperature', self.temperature)
        timeout = send_params.get('timeout', 240)

        if response_format is None:
            logger.error("Response format is None for Gemini structured output.")
            raise ValueError("Invalid response format: None")

        json_schema = self._to_json_schema(response_format)

        logger.info(f"Using Gemini model: {model_name} for structured output")

        timer_id = performance_monitor.start_timer('llm_structured_output_gemini', f"{model_name}_{len(messages)}")

        try:
            system_instruction, content = self._convert_messages_to_contents(messages)

            import json as json_lib
            import uuid as _uuid

            # Unique prefix defeats the SDK's implicit prompt caching which
            # can crash or produce infinite-loop JSON on the second call with
            # a matching prefix.
            cache_bust = f"[req-{_uuid.uuid4().hex[:8]}]\n"

            if system_instruction:
                full_prompt = f"{cache_bust}{system_instruction}\n\n{content}"
            else:
                full_prompt = f"{cache_bust}{content}"

            _thread = threading.current_thread()
            logger.info(
                "Gemini generate_content: model=%s, prompt_len=%d, schema_keys=%s, thread=%s(%s)",
                model_name, len(full_prompt), list(json_schema.get("properties", {}).keys()),
                _thread.name, _thread.ident,
            )
            print(f"    [Gemini] >> {model_name} prompt={len(full_prompt)} chars [thread:{_thread.name}]", flush=True)

            response = self.client.models.generate_content(
                model=model_name,
                contents=full_prompt,
                config=self.types.GenerateContentConfig(
                    response_mime_type='application/json',
                    response_json_schema=json_schema,
                    temperature=temperature,
                    safety_settings=self._permissive_safety_settings(),
                )
            )

            print(f"    [Gemini] << OK [thread:{_thread.name}]", flush=True)
            logger.info("Gemini generate_content returned (model=%s, thread=%s)", model_name, _thread.name)
            raw_text = response.text
            if not raw_text:
                block_reason = getattr(getattr(response, "prompt_feedback", None), "block_reason", None)
                finish_reason = None
                safety_ratings = None
                if response.candidates:
                    finish_reason = getattr(response.candidates[0], "finish_reason", None)
                    safety_ratings = getattr(response.candidates[0], "safety_ratings", None)
                logger.error(
                    "Gemini returned empty response text "
                    "(model=%s, block_reason=%s, finish_reason=%s, safety_ratings=%s)",
                    model_name, block_reason, finish_reason, safety_ratings,
                )
                raise ValueError(
                    f"Gemini returned empty response "
                    f"(block_reason={block_reason}, finish_reason={finish_reason})"
                )
            result_dict = json_lib.loads(raw_text)

            performance_monitor.end_timer(timer_id, {'status': 'success', 'model': model_name})
            logger.info(f"✅ Gemini response received successfully")
            return result_dict

        except Exception as e:
            performance_monitor.end_timer(timer_id, {'status': 'error', 'model': model_name, 'error': str(e)})
            logger.error(f"Gemini LLM error: {e}", exc_info=True)
            raise

    def structured_output_json(self, messages, **send_params):
        """Gemini equivalent for structured JSON output."""
        return self.structured_output(messages, **send_params)


class AnthropicLLM(BaseLLMProvider):
    _instance = None
    _instance_lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = super(AnthropicLLM, cls).__new__(cls)
                    cls._instance._init_once(*args, **kwargs)
        else:
            if not hasattr(cls._instance, "client"):
                with cls._instance_lock:
                    cls._instance._init_once(*args, **kwargs)
        return cls._instance

    def _init_once(self, engine="claude-3-5-haiku-20241022", temperature=0.1, **kwargs):
        if hasattr(self, "client"):
            return

        if "api_key" in kwargs:
            raise ValueError("Passing api_key is not supported. Set ANTHROPIC_API_KEY in environment.")

        try:
            import anthropic as _anthropic
            self._anthropic_module = _anthropic
        except ImportError:
            logger.error("anthropic package is not installed. Please run 'pip install anthropic'")
            raise

        self.api_key = os.environ.get('ANTHROPIC_API_KEY')
        if _is_placeholder_api_key(self.api_key):
            raise ValueError("ANTHROPIC_API_KEY is missing or invalid. Set a real API key in environment.")

        self.client = self._anthropic_module.Anthropic(api_key=self.api_key)
        self.engine = engine
        self.temperature = temperature

        for key, value in kwargs.items():
            setattr(self, key, value)

    def _split_messages(self, messages):
        """Split OpenAI-style messages into system string + chat messages list."""
        system_parts = []
        chat_messages = []
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content", "")
            if role == "system":
                system_parts.append(content)
            elif role in ("user", "assistant"):
                chat_messages.append({"role": role, "content": content})
        if not chat_messages:
            chat_messages = [{"role": "user", "content": "Please respond."}]
        return "\n\n".join(system_parts) if system_parts else None, chat_messages

    def structured_output(self, messages, **send_params):
        response_format = send_params.get('response_format')
        model_name = send_params.get('engine', self.engine)
        temperature = send_params.get('temperature', self.temperature)

        if response_format is None:
            logger.error("Response format is None for Anthropic structured output.")
            raise ValueError("Invalid response format: None")

        logger.info(f"Using Anthropic model: {model_name} for structured output")
        timer_id = performance_monitor.start_timer('llm_structured_output_anthropic', f"{model_name}_{len(messages)}")

        try:
            import json as json_lib

            system_text, chat_messages = self._split_messages(messages)

            schema = response_format.model_json_schema()
            schema_str = json_lib.dumps(schema, indent=2)
            json_instruction = (
                f"\n\nRespond ONLY with a single valid JSON object that matches this schema exactly. "
                f"No explanation, no markdown fences, no text outside the JSON:\n{schema_str}"
            )

            full_system = (system_text + json_instruction) if system_text else json_instruction.lstrip()

            response = self.client.messages.create(
                model=model_name,
                max_tokens=8192,
                temperature=temperature,
                system=full_system,
                messages=chat_messages,
            )

            raw_text = response.content[0].text.strip()

            # Strip markdown fences if present
            if raw_text.startswith("```"):
                raw_text = re.sub(r"^```[a-z]*\n?", "", raw_text)
                raw_text = re.sub(r"\n?```$", "", raw_text).strip()

            result_dict = json_lib.loads(raw_text)

            performance_monitor.end_timer(timer_id, {'status': 'success', 'model': model_name})
            logger.info("✅ Anthropic response received successfully")
            return result_dict

        except Exception as e:
            performance_monitor.end_timer(timer_id, {'status': 'error', 'model': model_name, 'error': str(e)})
            logger.error(f"Anthropic LLM error: {e}", exc_info=True)
            raise

    def structured_output_json(self, messages, **send_params):
        return self.structured_output(messages, **send_params)

    def send_query(self, messages, **send_request):
        raise NotImplementedError("AnthropicLLM.send_query not implemented.")

    def send_function_query(self, messages, **send_request):
        raise NotImplementedError("AnthropicLLM.send_function_query not implemented.")

    def build_messages(self, **send_params):
        raise NotImplementedError("AnthropicLLM.build_messages not implemented.")

    def stream_response_to_socket(self, messages, socket_id, user_id, audio_output, **kwargs):
        raise NotImplementedError("AnthropicLLM.stream_response_to_socket not implemented.")


class QuotaExhaustedError(Exception):
    """Raised when an LLM provider returns a quota/billing error.

    Callers should catch this, notify the user, and stop further LLM calls
    rather than retrying or cascading failures.
    """
    def __init__(self, provider: str, message: str = ""):
        self.provider = provider
        super().__init__(message or f"LLM quota exhausted for provider '{provider}'. Check billing.")


# Global circuit breaker state: provider -> True means tripped (quota exhausted).
_quota_tripped: dict[str, bool] = {}
_quota_tripped_lock = threading.Lock()


def _check_and_trip_quota(provider: str, exc: Exception) -> bool:
    """If *exc* is a quota/billing error, notify the user and terminate the process.

    This is intentionally aggressive: a quota error means every subsequent LLM call
    will also fail.  Continuing would let batch loops (KG pipeline, entity cards, etc.)
    race through items, catch the exception, and mark work as done without results.
    The only safe action is to stop immediately.
    """
    exc_str = str(exc).lower()
    is_quota = (
        "insufficient_quota" in exc_str
        or "exceeded your current quota" in exc_str
        or ("429" in exc_str and "quota" in exc_str)
    )
    if not is_quota:
        return False

    with _quota_tripped_lock:
        already = _quota_tripped.get(provider, False)
        _quota_tripped[provider] = True

    if already:
        # Another thread already handling shutdown — just raise so this thread stops.
        return True

    logger.error(
        "🚨 QUOTA EXHAUSTED for provider '%s'. Notifying user and shutting down to prevent data corruption.",
        provider,
    )

    # Best-effort UI notification via EventHub + SocketIO
    try:
        from app.assistant.ServiceLocator.service_locator import DI
        from app.assistant.utils.pydantic_classes import Message
        event_hub = getattr(DI, "event_hub", None)
        if event_hub:
            event_hub.publish(Message(
                sender="system",
                event_topic="socket_emit",
                data={
                    "event": "system_alert",
                    "payload": {
                        "level": "error",
                        "title": "API Quota Exhausted — Shutting Down",
                        "message": f"The {provider.upper()} API quota has been exceeded. "
                                   "EmiAi is shutting down to prevent data corruption. "
                                   "Please check your billing and restart.",
                    },
                },
            ))
    except Exception:
        logger.debug("Could not publish quota alert to UI", exc_info=True)

    # Give the socket notification a moment to flush, then exit.
    import time
    time.sleep(2)

    logger.error("🛑 Terminating process due to quota exhaustion (provider=%s).", provider)
    os._exit(1)  # Hard exit — avoids atexit hooks that might trigger more LLM calls


def reset_quota_breaker(provider: str | None = None) -> None:
    """Manually reset the circuit breaker (e.g., after topping up billing)."""
    with _quota_tripped_lock:
        if provider:
            _quota_tripped.pop(provider, None)
        else:
            _quota_tripped.clear()
    logger.info("Quota circuit breaker reset for %s", provider or "all providers")


class LLMInterface:
    def __init__(self, llm_provider: BaseLLMProvider):
        self.llm_provider = llm_provider

    @property
    def _provider_name(self) -> str:
        return getattr(self.llm_provider, "provider_name", None) or type(self.llm_provider).__name__

    def _guard_quota(self) -> None:
        name = self._provider_name
        with _quota_tripped_lock:
            tripped = _quota_tripped.get(name, False)
        if tripped:
            raise QuotaExhaustedError(name)

    def structured_output(self, message, use_json=False, **params):
        self._guard_quota()
        try:
            if not use_json:
                response = self.llm_provider.structured_output(message, **params)
            else:
                response = self.llm_provider.structured_output_json(message, **params)
            return response
        except QuotaExhaustedError:
            raise
        except Exception as exc:
            if _check_and_trip_quota(self._provider_name, exc):
                raise QuotaExhaustedError(self._provider_name, str(exc)) from exc
            raise

    def structured_output_json(self, message, **params):
        self._guard_quota()
        try:
            response = self.llm_provider.structured_output_json(message, **params)
            return response
        except QuotaExhaustedError:
            raise
        except Exception as exc:
            if _check_and_trip_quota(self._provider_name, exc):
                raise QuotaExhaustedError(self._provider_name, str(exc)) from exc
            raise



from pydantic import BaseModel
import json

def sanitize_schema(schema_part: dict):
    """
    Make a Pydantic/JSON schema compatible with OpenAI Structured Outputs.

    Key requirement (OpenAI): every object schema must have `additionalProperties: false`
    unless it explicitly declares typed additionalProperties.

    Pydantic sometimes emits object-like schemas without an explicit `type: object`
    at the point we see them (e.g., via refs/anyOf), so we treat any schema that
    has `properties` as object-ish.
    """
    if not isinstance(schema_part, dict):
        return schema_part

    t = schema_part.get("type")
    type_list = t if isinstance(t, list) else None
    is_objectish = (
        t == "object"
        or (isinstance(type_list, list) and "object" in type_list)
        or isinstance(schema_part.get("properties"), dict)
    )

    if is_objectish:
        # Enforce additionalProperties=false unless explicitly typed.
        if "additionalProperties" not in schema_part:
            schema_part["additionalProperties"] = False
        elif isinstance(schema_part.get("additionalProperties"), dict):
            # Respect typed additionalProperties (e.g., {"type": "string"})
            pass

        # OpenAI Structured Outputs (strict JSON schema) expects `required` to be present
        # and to include *every* key in `properties`. Optionality should be expressed via
        # nullable types (e.g., anyOf: [<type>, null]) rather than omitting from required.
        props = schema_part.get("properties")
        if isinstance(props, dict) and props:
            schema_part["required"] = sorted([k for k in props.keys() if isinstance(k, str) and k])

    # OpenAI rejects "default" in many schema positions; drop it.
    if "default" in schema_part:
        schema_part.pop("default")

    # Recurse common schema containers.
    props = schema_part.get("properties")
    if isinstance(props, dict):
        for prop in props.values():
            if isinstance(prop, dict):
                sanitize_schema(prop)

    items = schema_part.get("items")
    if isinstance(items, dict):
        sanitize_schema(items)
    elif isinstance(items, list):
        for it in items:
            if isinstance(it, dict):
                sanitize_schema(it)

    for key in ("anyOf", "oneOf", "allOf"):
        alts = schema_part.get(key)
        if isinstance(alts, list):
            for alt in alts:
                if isinstance(alt, dict):
                    sanitize_schema(alt)

    if "definitions" in schema_part and isinstance(schema_part["definitions"], dict):
        for value in schema_part["definitions"].values():
            if isinstance(value, dict):
                sanitize_schema(value)
    if "$defs" in schema_part and isinstance(schema_part["$defs"], dict):
        for value in schema_part["$defs"].values():
            if isinstance(value, dict):
                sanitize_schema(value)

    return schema_part

def inline_refs(schema: dict, root_schema: dict = None) -> dict:
    """
    Recursively replace local $ref occurrences with their actual definitions from root_schema.
    """
    if root_schema is None:
        root_schema = schema

    if isinstance(schema, dict):
        if '$ref' in schema:
            ref = schema['$ref']
            # Only support local references like "#/path/to/definition"
            if not ref.startswith("#/"):
                raise ValueError(f"Only local references are supported, got {ref}")
            # Traverse the root schema using the reference path
            parts = ref[2:].split("/")  # remove "#/" and split
            ref_value = root_schema
            for part in parts:
                if part not in ref_value:
                    raise ValueError(f"Reference {ref} not found in schema")
                ref_value = ref_value[part]
            # Inline the referenced value (and process nested refs)
            return inline_refs(ref_value, root_schema)
        else:
            # Recursively process dictionary items
            return {key: inline_refs(value, root_schema) for key, value in schema.items()}
    elif isinstance(schema, list):
        return [inline_refs(item, root_schema) for item in schema]
    else:
        return schema

def pydantic_to_openai_schema(model: type[BaseModel], name: str | None = None, strict: bool = True) -> dict:
    schema = model.model_json_schema()
    # Sanitize the schema first to set additionalProperties and remove unwanted defaults.
    sanitize_schema(schema)
    # Inline any $ref references.
    schema = inline_refs(schema)
    # Inline can introduce object schemas in new positions; sanitize again to
    # enforce OpenAI Structured Outputs requirements everywhere.
    sanitize_schema(schema)
    # Remove leftover definitions containers
    schema.pop("$defs", None)
    schema.pop("definitions", None)

    return {
        "format": {
            "type": "json_schema",
            "name": name or model.__name__,
            "schema": schema,
            "strict": strict
        }
    }

if __name__ == "__main__":
    class AgentForm(BaseModel):
        final_answer_content: str
        summary: str
        important_links: List[Dict[str, str]]

    schema_output = pydantic_to_openai_schema(AgentForm)
    print(json.dumps(schema_output, indent=2))
