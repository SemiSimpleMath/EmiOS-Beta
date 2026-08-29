#llm_client.py
from typing import List, Dict, Any, Optional

import base64
import mimetypes
from pathlib import Path
import re

from openai import OpenAI, RateLimitError as OpenAIRateLimitError

import os
import threading
import time

from app.assistant.utils.logging_config import get_logger
from app.assistant.performance.performance_monitor import performance_monitor
from app.services.llm_resilience import (
    BillingQuotaExhausted,
    TransientRateLimit,
    classify_quota,
)
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
        kwargs = caps.build_base_kwargs(messages=messages, timeout=timeout, temperature=temperature)
        response = client.responses.create(**kwargs)
        # Capture usage for the call logger before parsing. Persists even
        # if the parse below fails — failed parses still bill real tokens.
        self.last_usage = getattr(response, "usage", None)
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
        from pydantic import BaseModel as _BaseModel  # local import for clarity

        parse_kwargs = caps.build_base_kwargs(messages=messages, timeout=timeout, temperature=temperature)
        parse_kwargs["text_format"] = response_format

        parse_api = getattr(client.responses, "parse", None)
        if not callable(parse_api):
            raise RuntimeError("OpenAI client does not support responses.parse for direct Pydantic output.")

        response = parse_api(**parse_kwargs)
        # Capture usage before extracting parsed result — same reasoning
        # as the prompt-json-validate path. The most-trafficked strategy
        # in production (most agents route through here).
        self.last_usage = getattr(response, "usage", None)
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


# ---------------------------------------------------------------------------
# Provider-boundary quota translation — SDK typed markers → canonical taxonomy
# ---------------------------------------------------------------------------
# Each provider's except-block translates its SDK error into
# BillingQuotaExhausted (interface layer stops the world) or
# TransientRateLimit (the retry ladder owns it), using the STRUCTURED
# markers the SDKs expose — error.code / QuotaFailure.quotaId — with
# classify_quota's message matching only for errors that arrive
# unstructured. Returns None for non-quota errors (caller handles them).

# OpenAI billing-exhaustion markers. Legacy quota billing:
# code="insufficient_quota". Credits billing model:
# code="credit_balance_exhausted", type="insufficient_quota".
_OPENAI_BILLING_MARKERS = frozenset({"insufficient_quota", "credit_balance_exhausted"})


# Plan-level usage caps (OpenCode Zen's "GoUsageLimitError", and weekly /
# monthly caps generally). These arrive as HTTP 429 — indistinguishable by
# status code from a per-minute rate limit — but they do not clear for hours
# or days. Classified as transient they make the retry ladder and the
# scheduler spin on them forever, so they must resolve to billing, which the
# circuit breaker treats as fatal.
_LONG_WINDOW_LIMIT_MARKERS = (
    "gousagelimiterror",
    "usage limit reached",
    "weekly usage limit",
    "monthly usage limit",
    "daily usage limit",
)
_LONG_WINDOW_NAMES = ("weekly", "monthly", "daily")


def _is_long_window_usage_limit(e: Exception) -> bool:
    """True for a usage cap that will not clear inside any sane retry window."""
    s = str(e).lower()
    if any(m in s for m in _LONG_WINDOW_LIMIT_MARKERS):
        return True
    # OpenCode names the window in its own field: {"limitName": "weekly"}
    if "limitname" in s and any(w in s for w in _LONG_WINDOW_NAMES):
        return True
    return False


def _translate_openai_exception(e: Exception, provider: str = "openai") -> Optional[Exception]:
    """OpenAI marks billing exhaustion with a typed marker in error.code or
    error.type; any other RateLimitError = transient window, with the
    suggested wait in the Retry-After header. Billing markers must be checked
    in BOTH fields: the credits billing model returns
    code="credit_balance_exhausted" with type="insufficient_quota"
    (2026-08-17 incident — the unknown code shadowed the type and the burn
    never tripped the breaker).

    OpenAI-compatible third parties do not use those markers: OpenCode Zen
    returns a plain 429 whose body carries GoUsageLimitError plus the window
    name, so that shape is checked before the generic RateLimitError branch."""
    fields = {getattr(e, "code", None), getattr(e, "type", None)}
    if fields & _OPENAI_BILLING_MARKERS:
        return BillingQuotaExhausted(provider=provider, message=str(e))
    # Must precede the RateLimitError branch below — a weekly cap is a 429 and
    # would otherwise be misread as a transient per-minute window.
    if _is_long_window_usage_limit(e):
        return BillingQuotaExhausted(provider=provider, message=str(e))
    if isinstance(e, OpenAIRateLimitError):
        retry_after = None
        headers = getattr(getattr(e, "response", None), "headers", None)
        if headers is not None:
            raw = headers.get("retry-after")
            if raw:
                try:
                    retry_after = float(raw)
                except (TypeError, ValueError):
                    retry_after = None
        return TransientRateLimit(str(e), retry_after=retry_after)
    kind = classify_quota(e)
    if kind == "billing":
        return BillingQuotaExhausted(provider=provider, message=str(e))
    if kind == "rate":
        return TransientRateLimit(str(e))
    return None


def _gemini_quota_details(details: Any) -> tuple[list[str], Optional[float]]:
    """Pull QuotaFailure violation quotaIds + RetryInfo.retryDelay seconds
    out of a google-genai APIError.details payload. Tolerates the shapes the
    SDK passes through (error-wrapped dict / bare dict)."""
    quota_ids: list[str] = []
    retry_after: Optional[float] = None
    try:
        node = details
        if isinstance(node, dict) and isinstance(node.get("error"), dict):
            node = node["error"]
        detail_list = node.get("details") if isinstance(node, dict) else None
        if not isinstance(detail_list, list):
            return quota_ids, retry_after
        for item in detail_list:
            if not isinstance(item, dict):
                continue
            at_type = str(item.get("@type") or "")
            if at_type.endswith("QuotaFailure"):
                for violation in item.get("violations") or []:
                    if isinstance(violation, dict):
                        qid = str(violation.get("quotaId") or violation.get("quota_id") or "")
                        if qid:
                            quota_ids.append(qid)
            elif at_type.endswith("RetryInfo"):
                raw = str(item.get("retryDelay") or item.get("retry_delay") or "").strip().rstrip("s")
                if raw:
                    try:
                        retry_after = float(raw)
                    except ValueError:
                        pass
    except Exception:
        logger.debug("gemini quota detail extraction failed", exc_info=True)
    return quota_ids, retry_after


def _translate_gemini_exception(e: Exception) -> Optional[Exception]:
    """Gemini reports BOTH billing/daily exhaustion and per-minute windows
    as 429 RESOURCE_EXHAUSTED with near-identical messages; the structured
    QuotaFailure.quotaId distinguishes them (…PerMinute… vs …PerDay…). A
    429 whose violations name a per-minute window is transient; any other
    429 stops the world — conservative by policy: misclassification must
    mean "stopped unnecessarily", never "kept burning the batch"."""
    code = getattr(e, "code", None)
    status = str(getattr(e, "status", "") or "").upper()
    if code == 429 or "RESOURCE_EXHAUSTED" in status:
        quota_ids, retry_after = _gemini_quota_details(getattr(e, "details", None))
        joined = " ".join(quota_ids).lower().replace("_", "").replace("-", "")
        if "perminute" in joined:
            return TransientRateLimit(str(e), retry_after=retry_after)
        if not quota_ids and classify_quota(e) == "rate":
            # No structured violations arrived, but the message itself names
            # a per-minute window.
            return TransientRateLimit(str(e), retry_after=retry_after)
        return BillingQuotaExhausted(provider="gemini", message=str(e))
    kind = classify_quota(e)
    if kind == "billing":
        return BillingQuotaExhausted(provider="gemini", message=str(e))
    if kind == "rate":
        return TransientRateLimit(str(e))
    return None


def _translate_anthropic_exception(e: Exception, anthropic_module: Any) -> Optional[Exception]:
    """Anthropic types its 429 as RateLimitError; out-of-credit arrives as a
    400 invalid_request_error ("credit balance is too low")."""
    rate_limit_cls = getattr(anthropic_module, "RateLimitError", None)
    if rate_limit_cls is not None and isinstance(e, rate_limit_cls):
        return TransientRateLimit(str(e))
    if "credit balance is too low" in str(e).lower():
        return BillingQuotaExhausted(provider="anthropic", message=str(e))
    kind = classify_quota(e)
    if kind == "billing":
        return BillingQuotaExhausted(provider="anthropic", message=str(e))
    if kind == "rate":
        return TransientRateLimit(str(e))
    return None


class BaseLLMProvider:
    """Provider contract: structured output is the ONE live surface.
    (The old send_query / build_messages / streaming methods had zero
    callers anywhere — every consumer goes through structured output.)"""

    # Prefix for this provider's log lines. Subclasses override.
    _log_tag = "LLM"

    def structured_output(self, messages, **send_params):
        raise NotImplementedError("This method should be implemented by subclasses.")

    def structured_output_json(self, messages, **send_params):
        raise NotImplementedError("This method should be implemented by subclasses.")

    def _structured_output_once(self, messages, **send_params):
        raise NotImplementedError("This method should be implemented by subclasses.")

    def _structured_output_with_timeout_ladder(self, messages, **send_params):
        """Run _structured_output_once against progressively longer timeouts.

        Structured-output calls occasionally hang for minutes even though they
        would eventually succeed; a short first attempt catches those and
        usually succeeds on retry. If the caller already budgeted a large
        timeout (>= LLM_STRUCTURED_OUTPUT_SKIP_RETRY_ABOVE) they are expecting a
        slow call, so the ladder is skipped rather than burning 60s + 120s on
        attempts that were always going to need the full budget.

        Shared by every OpenAI-protocol provider. It lived as two byte-identical
        copies in OpenAILLM and OpenCodeLLM, which is precisely the kind of
        duplicate that gets fixed in one copy and not the other.
        """
        original_timeout = send_params.get('timeout', 240)
        try:
            large_timeout_threshold = int(
                os.environ.get("LLM_STRUCTURED_OUTPUT_SKIP_RETRY_ABOVE", "180"))
        except ValueError:
            large_timeout_threshold = 180

        if original_timeout >= large_timeout_threshold:
            attempt_timeouts = [original_timeout]
        else:
            try:
                short = min(original_timeout, int(
                    os.environ.get("LLM_STRUCTURED_OUTPUT_FAST_TIMEOUT", "60")))
                medium = min(original_timeout, int(
                    os.environ.get("LLM_STRUCTURED_OUTPUT_MEDIUM_TIMEOUT", "120")))
            except ValueError:
                short, medium = 60, 120
            attempt_timeouts = [t for t in (short, medium, original_timeout) if t > 0]
            # Deduplicate while preserving order.
            seen = set()
            attempt_timeouts = [t for t in attempt_timeouts if not (t in seen or seen.add(t))]

        last_exc: Optional[Exception] = None
        for idx, t in enumerate(attempt_timeouts):
            # Copy-and-overwrite rather than passing timeout= alongside
            # **send_params: send_params already carries 'timeout' whenever the
            # caller set one, and Python raises TypeError on the duplicate
            # keyword before the request is ever made.
            send_params_copy = dict(send_params)
            send_params_copy['timeout'] = t
            try:
                return self._structured_output_once(messages, **send_params_copy)
            except TimeoutError as exc:
                # Only timeouts earn another attempt. Quota, schema and parse
                # failures are deterministic — retrying them just spends the
                # same budget three times over.
                last_exc = exc
                if idx < len(attempt_timeouts) - 1:
                    logger.warning(
                        "[%s] structured_output timed out at %ss (attempt %d/%d); retrying with %ss",
                        self._log_tag, t, idx + 1, len(attempt_timeouts),
                        attempt_timeouts[idx + 1],
                    )
                    continue
                raise
        if last_exc:
            raise last_exc
        raise RuntimeError("structured_output: no attempts were made")


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
        """Public entrypoint — progressive-timeout ladder around
        _structured_output_once. See BaseLLMProvider for the mechanics.

        Fact_extractor on gpt-5.4 is the typical caller that budgets a large
        timeout up front and therefore skips the ladder entirely.
        """
        return self._structured_output_with_timeout_ladder(messages, **send_params)

    def _structured_output_once(self, messages, **send_params):
        import time as _time

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

        # llm_call_log telemetry (Phase 1, 2026-05-26). Wall-clock around
        # the API call so duration_ms captures the full provider round
        # trip. Strategy is built (and stays as None) before the API call
        # — populated in the try block once we know which one to use.
        strategy: Optional[OpenAIStructuredOutputStrategy] = None
        log_status = "error"
        _call_started = _time.monotonic()

        try:
            from pydantic import BaseModel as _BaseModel  # local import for clarity
            if not isinstance(model, str) or not model.strip():
                raise ValueError("model must be a non-empty string")
            caps = OpenAIModelCapabilityNormalizer(model_name=model)

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
            result = strategy.execute(
                client=self.client,
                caps=caps,
                messages=messages,
                timeout=timeout,
                temperature=temperature,
                response_format=response_format,
            )
            log_status = "ok"
            return result

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

            # Already canonical (nested call translated it) — pass through.
            if isinstance(e, (BillingQuotaExhausted, TransientRateLimit)):
                log_status = "quota" if isinstance(e, BillingQuotaExhausted) else "rate_limit"
                raise

            # Quota taxonomy from the SDK's structured markers (error.code),
            # message matching only for unstructured shapes.
            canonical = _translate_openai_exception(e)
            if canonical is not None:
                if isinstance(canonical, BillingQuotaExhausted):
                    log_status = "quota"
                    logger.critical(
                        "LLM billing quota exhausted — context: structured_output (model: %s) — %s",
                        model, e,
                    )
                else:
                    log_status = "rate_limit"
                raise canonical from e

            error_str = str(e).lower()
            # Fail loudly: never convert provider errors into plain strings.
            if "timeout" in error_str or "timed out" in error_str:
                log_status = "timeout"
                raise TimeoutError(f"LLM request timed out after {timeout} seconds") from e
            if "rate limit" in error_str:
                log_status = "rate_limit"
                raise TransientRateLimit(str(e)) from e
            if "invalid json" in error_str or "validation error" in error_str:
                log_status = "parse_error"
            else:
                log_status = "error"
            raise RuntimeError(f"LLM structured_output failed: {e}") from e
        finally:
            # Persist one llm_call_log row regardless of outcome. Failure-
            # tolerant inside the helper — won't raise even if the DB write
            # fails (telemetry must never break the actual LLM call).
            try:
                from app.services.llm_call_logger import record_llm_call
                duration_ms = int((_time.monotonic() - _call_started) * 1000.0)
                record_llm_call(
                    engine=model,
                    provider="openai",
                    usage=getattr(strategy, "last_usage", None) if strategy else None,
                    duration_ms=duration_ms,
                    status=log_status,
                )
            except Exception as _telemetry_err:  # paranoia layer; logger already absorbs
                logger.debug("llm_call_log write skipped: %s", _telemetry_err)

    def structured_output_json(self, messages, **send_params):
        """Dict-schema structured output — same call path as
        structured_output (which routes a dict response_format to the JSON
        schema strategy natively), so the telemetry finally-hook, the quota
        translation, and the timeout ladder all apply here too. This route
        used to be a separate duplicated body with NO llm_call_log hook —
        agents on dict schemas (fast_tool::final_answer,
        orchestrator_evaluator) had zero telemetry rows ever (audit L3).
        Gemini and Anthropic already alias the same way.
        """
        return self.structured_output(messages, **send_params)


class OpenCodeLLM(BaseLLMProvider):
    """OpenCode Zen (Go plan) — the OpenAI-compatible API at opencode.ai/zen/go.

    A separate singleton from OpenAILLM so both can coexist. Reads
    OPENCODE_API_KEY and OPENCODE_BASE_URL from the environment.

    Deliberately a sibling of OpenAILLM rather than a subclass: it shares the
    wire protocol but not the behaviour. It cannot use response_format (the
    schema is injected into the prompt instead), its 429s mean a weekly plan
    quota rather than a per-minute window, and it is a separate singleton. The
    one part that genuinely is common — the timeout ladder — lives on
    BaseLLMProvider and is inherited by both.
    """
    _log_tag = "OpenCodeLLM"
    _instance = None
    _instance_lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = super(OpenCodeLLM, cls).__new__(cls)
                    cls._instance._init_once(*args, **kwargs)
        else:
            if not hasattr(cls._instance, "client"):
                with cls._instance_lock:
                    cls._instance._init_once(*args, **kwargs)
        return cls._instance

    def _init_once(self, engine="kimi-k2.7-code", temperature=0.1, **kwargs):
        if hasattr(self, "client"):
            return

        self.api_key = os.environ.get('OPENCODE_API_KEY')
        if _is_placeholder_api_key(self.api_key):
            raise ValueError("OPENCODE_API_KEY is missing or invalid. "
                             "Set a real OpenCode Go API key in environment.")

        base_url = os.environ.get('OPENCODE_BASE_URL',
                                  'https://opencode.ai/zen/go/v1')
        self.engine = engine
        self.temperature = temperature
        self.last_usage = None
        self.client = OpenAI(api_key=self.api_key, base_url=base_url)

        for key, value in kwargs.items():
            setattr(self, key, value)

    def structured_output(self, messages, **send_params):
        """Public entrypoint — progressive-timeout ladder around
        _structured_output_once. See BaseLLMProvider for the mechanics.
        """
        return self._structured_output_with_timeout_ladder(messages, **send_params)

    def _structured_output_once(self, messages, **send_params):
        """Single attempt at structured output via chat.completions.create + response_format."""
        import time as _time
        import json as _json

        messages = _normalize_openai_responses_messages(messages)
        response_format = send_params.get('response_format')
        model = send_params.get('engine') or self.engine
        temperature = send_params.get('temperature', self.temperature)
        timeout = send_params.get('timeout', 240)
        max_tokens = send_params.get('max_tokens', 8192)

        if response_format is None:
            raise ValueError("Invalid response format: None")

        _thread = threading.current_thread()
        logger.info(f"[OpenCodeLLM] model={model} temperature={temperature} timeout={timeout}s")

        timer_id = performance_monitor.start_timer('llm_structured_output', f"{model}_{len(messages)}")
        log_status = "error"
        _call_started = _time.monotonic()
        # The provider is a singleton, so a failed call would otherwise log the
        # PREVIOUS call's token counts in the finally block below.
        self.last_usage = None

        try:
            from pydantic import BaseModel as _BaseModel
            if not isinstance(model, str) or not model.strip():
                raise ValueError("model must be a non-empty string")

            # Build request kwargs: model, messages, temperature, max_tokens.
            # `timeout` must reach the SDK — without it every attempt runs on
            # the client default (600s) and the ladder above is decorative.
            create_kwargs = {"max_tokens": max_tokens, "timeout": timeout}

            # OpenCode Go: skip response_format — inject JSON instruction into
            # prompt instead. response_format is guaranteed non-None here (the
            # None case raised above), so no re-check.
            schema_desc = ""
            if isinstance(response_format, type) and issubclass(response_format, _BaseModel):
                schema_desc = _json.dumps(response_format.model_json_schema(), indent=2)
            elif isinstance(response_format, dict):
                schema_desc = _json.dumps(response_format, indent=2)
            if schema_desc:
                messages = list(messages) + [{
                    "role": "system",
                    "content": f"Respond ONLY with a single valid JSON object. Do not include markdown fences, explanations, or any other text. The JSON must match this schema:\n{schema_desc}"
                }]

            response = self.client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                **create_kwargs,
            )
            self.last_usage = getattr(response, "usage", None)

            raw = response.choices[0].message.content
            if response.choices[0].message.refusal:
                raise ValueError(f"OpenCodeLLM refused: {response.choices[0].message.refusal}")

            # Parse JSON from the response. _parse_first_json_object RAISES on
            # empty or unparseable input rather than returning a sentinel, so
            # it has to be guarded: unguarded, the json.loads fallback below is
            # unreachable and an empty completion surfaces as a bare
            # "empty json text" with no hint at the cause.
            result = None
            if isinstance(raw, str) and raw.strip():
                try:
                    result = _parse_first_json_object(raw)
                except Exception:
                    logger.debug(
                        "[OpenCodeLLM] tolerant JSON scan failed; trying strict json.loads",
                        exc_info=True,
                    )
                    try:
                        result = _json.loads(raw.strip())
                    except _json.JSONDecodeError:
                        result = None

            if isinstance(result, dict):
                log_status = "ok"
                return result

            if not (isinstance(raw, str) and raw.strip()):
                # Reasoning models can spend the entire max_tokens budget before
                # emitting any content, making the completion legitimately empty.
                finish = getattr(response.choices[0], "finish_reason", None)
                raise ValueError(
                    f"OpenCodeLLM returned an empty completion (non-JSON; "
                    f"finish_reason={finish!r}, max_tokens={max_tokens}) — raise "
                    f"max_tokens if this model reasons before answering"
                )
            raise ValueError(f"OpenCodeLLM returned non-JSON: {raw[:200]}")

        except Exception as e:
            performance_monitor.end_timer(timer_id, {
                'status': 'error', 'model': model,
                'message_count': len(messages), 'temperature': temperature,
                'timeout': timeout, 'error': str(e),
            })
            logger.error(f"[OpenCodeLLM] structured_output error: {e}")
            logger.debug("[OpenCodeLLM] structured_output exception details", exc_info=True)

            if isinstance(e, (BillingQuotaExhausted, TransientRateLimit)):
                log_status = "quota" if isinstance(e, BillingQuotaExhausted) else "rate_limit"
                raise
            canonical = _translate_openai_exception(e, provider="opencode")
            if canonical is not None:
                if isinstance(canonical, BillingQuotaExhausted):
                    log_status = "quota"
                else:
                    log_status = "rate_limit"
                raise canonical from e
            error_str = str(e).lower()
            if "timeout" in error_str or "timed out" in error_str:
                log_status = "timeout"
                raise TimeoutError(f"LLM request timed out after {timeout} seconds") from e
            if "rate limit" in error_str:
                log_status = "rate_limit"
                raise TransientRateLimit(str(e)) from e
            # "non-json" / "empty completion" are the two shapes raised above
            # when the model answers with prose (or nothing) instead of JSON.
            if (
                "invalid json" in error_str
                or "validation error" in error_str
                or "non-json" in error_str
                or "empty completion" in error_str
            ):
                log_status = "parse_error"
            else:
                log_status = "error"
            raise RuntimeError(f"LLM structured_output failed: {e}") from e
        finally:
            try:
                from app.services.llm_call_logger import record_llm_call
                duration_ms = int((_time.monotonic() - _call_started) * 1000.0)
                record_llm_call(
                    engine=model, provider="opencode",
                    usage=getattr(self, 'last_usage', None),
                    duration_ms=duration_ms, status=log_status,
                )
            except Exception:
                pass

    def structured_output_json(self, messages, **send_params):
        return self.structured_output(messages, **send_params)


# ---------------------------------------------------------------------------
# Gemini schema utilities — available for manually inlining $ref/$defs or
# stripping unsupported keys.  GeminiLLM now uses response_json_schema
# (with model_json_schema()) which supports $ref/$defs natively.
# ---------------------------------------------------------------------------

def _gemini_inline_refs(schema: dict) -> dict:
    """Inline all $defs/$ref references to produce a flat JSON Schema dict.

    Carries a `seen` set of in-progress ref keys so a self-referential model
    (a recursive $ref) breaks the cycle into an open object instead of
    recursing forever — the inliner must never loop on a pathological schema.
    """
    defs = schema.get("$defs", {})
    if not defs:
        return schema

    def _resolve(obj, seen):
        if isinstance(obj, dict):
            if "$ref" in obj:
                ref_key = obj["$ref"].split("/")[-1]
                if ref_key in seen:
                    return {"type": "object"}  # cycle — leave the node open
                return _resolve(defs.get(ref_key, obj), seen | {ref_key})
            return {k: _resolve(v, seen) for k, v in obj.items() if k != "$defs"}
        if isinstance(obj, list):
            return [_resolve(i, seen) for i in obj]
        return obj

    return _resolve(schema, frozenset())


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
    _instance_lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        # Thread-safe singleton init, same shape as OpenAILLM/AnthropicLLM —
        # parallel managers can construct providers concurrently.
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = super(GeminiLLM, cls).__new__(cls)
                    cls._instance._init_once(*args, **kwargs)
        else:
            if not hasattr(cls._instance, "client"):
                with cls._instance_lock:
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
        """Converts OpenAI-style messages to new Gemini contents format.

        Handles the [system, user] pairs every agent prompt produces.
        Assistant-role messages have no mapping here — a caller passing
        real multi-turn history would silently lose those turns, so log
        loudly if any arrive (none do today; agent history is rendered
        as text inside the user prompt).
        """
        system_instruction = None
        contents = []
        dropped_assistant = 0

        for msg in messages:
            role = msg.get("role")
            content = msg.get("content")

            if role == "system":
                system_instruction = content
            elif role == "user":
                contents.append(content)
            elif role == "assistant":
                dropped_assistant += 1

        if dropped_assistant:
            logger.error(
                "[gemini] DROPPED %d assistant-role message(s) — this converter "
                "flattens [system, user] prompts only; multi-turn history sent "
                "to Gemini loses its assistant turns.",
                dropped_assistant,
            )

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

    def _build_thinking_config(self, send_params):
        """Optional Gemini thinking control from agent llm_params — backward compatible.

        Returns a ThinkingConfig, or None when no thinking_* param is set — in which
        case the call uses Gemini's DEFAULT thinking (i.e. behavior is unchanged for
        every agent that doesn't opt in). Accepted llm_params:
          - thinking_level: 'low' | 'medium' | 'high'
          - thinking_budget: int (0 disables thinking on gemini-3; the clean off-switch)
          - include_thoughts: bool
        Never raises: a bad value or an SDK without ThinkingConfig falls back to None
        (default thinking), so this can't break existing calls.
        """
        level = send_params.get('thinking_level')
        budget = send_params.get('thinking_budget')
        include = send_params.get('include_thoughts')
        if level is None and budget is None and include is None:
            return None
        try:
            kwargs = {}
            if level is not None:
                kwargs['thinking_level'] = str(level).strip().lower()
            if budget is not None:
                kwargs['thinking_budget'] = int(budget)
            if include is not None:
                kwargs['include_thoughts'] = bool(include)
            return self.types.ThinkingConfig(**kwargs)
        except Exception as e:
            logger.warning("Gemini thinking_config build failed (%s); using default thinking", e)
            return None

    def _check_structured_response(self, response, model_name, json_lib):
        """Classify a structured-output response. Returns (reason, parsed):
        reason is None on a clean parse (parsed = the dict); otherwise a short
        string naming the degeneracy. Degenerate cases — empty/blocked,
        MAX_TOKENS truncation, or unparseable JSON — are the symptoms of the
        constrained-decoder runaway (the "infinite-loop JSON"); the caller
        retries them with a cache-bust nonce. API/transport errors are NOT
        handled here (they raise from generate_content and propagate)."""
        raw_text = getattr(response, "text", None)
        finish_reason = None
        if getattr(response, "candidates", None):
            finish_reason = getattr(response.candidates[0], "finish_reason", None)
        if not raw_text:
            block_reason = getattr(getattr(response, "prompt_feedback", None), "block_reason", None)
            logger.error(
                "Gemini returned empty response text (model=%s, block_reason=%s, finish_reason=%s)",
                model_name, block_reason, finish_reason,
            )
            return (f"empty_response(block={block_reason},finish={finish_reason})", None)
        if finish_reason is not None and "MAX_TOKENS" in str(finish_reason):
            # The runaway signature: generation hit the ceiling mid-structure,
            # so the JSON is truncated/looping.
            return ("max_tokens_truncation", None)
        try:
            return (None, json_lib.loads(raw_text))
        except Exception as e:
            return (f"json_parse_error({str(e)[:80]})", None)

    def structured_output(self, messages, **send_params):
        import time as _time

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

        # llm_call_log telemetry — Gemini exposes usage at
        # response.usage_metadata (prompt_token_count / candidates_token_count).
        _telemetry_usage = None
        _telemetry_status = "error"
        _call_started = _time.monotonic()

        try:
            system_instruction, content = self._convert_messages_to_contents(messages)

            import json as json_lib
            import uuid as _uuid

            # Sanitize the schema for Gemini: inline $ref/$defs (so the
            # constrained decoder gets a flat target) and strip
            # additionalProperties/$schema/title-metadata. The raw Pydantic
            # schema with refs + additionalProperties gave the decoder an
            # ambiguous target — a contributing cause of the "infinite-loop
            # JSON" the old blanket cache-bust was masking.
            json_schema = _gemini_clean_schema(_gemini_inline_refs(json_schema))

            base_prompt = (
                f"{system_instruction}\n\n{content}" if system_instruction else content
            )

            gen_config_kwargs = dict(
                response_mime_type='application/json',
                response_json_schema=json_schema,
                temperature=temperature,
                safety_settings=self._permissive_safety_settings(),
                # Per-call timeout (google-genai HttpOptions.timeout is MILLISECONDS). Without this
                # Gemini had no request timeout and could wedge a turn forever (reliability R2).
                http_options=self.types.HttpOptions(timeout=int(timeout * 1000)),
            )
            # Optional thinking control — only present if an agent opted in; absent
            # => Gemini default thinking (unchanged behavior for every other agent).
            thinking_config = self._build_thinking_config(send_params)
            if thinking_config is not None:
                gen_config_kwargs['thinking_config'] = thinking_config
            # Optional per-agent output bound. NOT defaulted: on Gemini thinking
            # models max_output_tokens COUNTS thinking tokens, so a low cap
            # starves the answer / returns empty (googleapis/python-genai#782).
            # Leave it None unless an agent explicitly opts in.
            if send_params.get('max_output_tokens') is not None:
                gen_config_kwargs['max_output_tokens'] = int(send_params['max_output_tokens'])

            # Happy path runs WITHOUT a unique prefix, so Gemini implicit caching
            # engages (a static system prefix with the variable content at the
            # end is Google's documented cache-friendly shape; ~75% input
            # discount on the cached prefix). The old code prepended a uuid that
            # defeated caching on EVERY call. The rare degenerate response
            # (empty / MAX_TOKENS truncation / unparseable — the "infinite-loop
            # JSON" symptom) is retried WITH a cache-bust nonce — the proven
            # mitigation, applied surgically only when needed instead of always.
            _MAX_ATTEMPTS = 3
            _thread = threading.current_thread()
            last_reason = None
            result_dict = None
            for _attempt in range(_MAX_ATTEMPTS):
                use_nonce = _attempt > 0
                nonce = f"[req-{_uuid.uuid4().hex[:8]}]\n" if use_nonce else ""
                full_prompt = f"{nonce}{base_prompt}"
                logger.info(
                    "Gemini generate_content: model=%s, prompt_len=%d, attempt=%d/%d, cache_bust=%s, thread=%s",
                    model_name, len(full_prompt), _attempt + 1, _MAX_ATTEMPTS, use_nonce, _thread.name,
                )
                print(f"    [Gemini] >> {model_name} prompt={len(full_prompt)} chars "
                      f"attempt={_attempt + 1} [thread:{_thread.name}]", flush=True)
                response = self.client.models.generate_content(
                    model=model_name,
                    contents=full_prompt,
                    config=self.types.GenerateContentConfig(**gen_config_kwargs),
                )
                _telemetry_usage = getattr(response, "usage_metadata", None)
                reason, parsed = self._check_structured_response(response, model_name, json_lib)
                if reason is None:
                    print(f"    [Gemini] << OK [thread:{_thread.name}]", flush=True)
                    result_dict = parsed
                    break
                last_reason = reason
                logger.warning(
                    "Gemini structured output degenerate (%s) on attempt %d/%d (model=%s) — "
                    "%s", reason, _attempt + 1, _MAX_ATTEMPTS, model_name,
                    "retrying with cache-bust nonce" if _attempt + 1 < _MAX_ATTEMPTS else "no attempts left",
                )

            if result_dict is None:
                # Fail loud: every attempt degenerated (and the cache-bust nonce
                # — today's known mitigation — did not recover it).
                _telemetry_status = "parse_error"
                raise ValueError(
                    f"Gemini structured output degenerate after {_MAX_ATTEMPTS} attempts "
                    f"(model={model_name}, last_reason={last_reason})"
                )

            performance_monitor.end_timer(timer_id, {'status': 'success', 'model': model_name})
            logger.info("✅ Gemini response received successfully")
            _telemetry_status = "ok"
            return result_dict

        except Exception as e:
            performance_monitor.end_timer(timer_id, {'status': 'error', 'model': model_name, 'error': str(e)})
            logger.error(f"Gemini LLM error: {e}", exc_info=True)
            if isinstance(e, (BillingQuotaExhausted, TransientRateLimit)):
                _telemetry_status = "quota" if isinstance(e, BillingQuotaExhausted) else "rate_limit"
                raise
            err_str = str(e).lower()
            # Timeouts FIRST: Gemini's timeout error says "Deadline Exceeded",
            # which the old quota check (bare "exceeded") swallowed — 30 days
            # of situation_auditor timeouts logged as status=quota (audit L4).
            if "timeout" in err_str or "timed out" in err_str or "deadline exceeded" in err_str:
                _telemetry_status = "timeout"
                raise
            canonical = _translate_gemini_exception(e)
            if canonical is not None:
                if isinstance(canonical, BillingQuotaExhausted):
                    _telemetry_status = "quota"
                    logger.critical(
                        "LLM billing quota exhausted — gemini model=%s — %s", model_name, e,
                    )
                else:
                    _telemetry_status = "rate_limit"
                raise canonical from e
            if "rate" in err_str and "limit" in err_str:
                _telemetry_status = "rate_limit"
            elif "json" in err_str or "validation" in err_str or "block_reason" in err_str:
                _telemetry_status = "parse_error"
            else:
                _telemetry_status = "error"
            raise
        finally:
            try:
                from app.services.llm_call_logger import record_llm_call
                duration_ms = int((_time.monotonic() - _call_started) * 1000.0)
                record_llm_call(
                    engine=model_name,
                    provider="gemini",
                    usage=_telemetry_usage,
                    duration_ms=duration_ms,
                    status=_telemetry_status,
                )
            except Exception as _telemetry_err:
                logger.debug("llm_call_log write skipped (gemini): %s", _telemetry_err)

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
        import time as _time

        response_format = send_params.get('response_format')
        model_name = send_params.get('engine', self.engine)
        temperature = send_params.get('temperature', self.temperature)
        timeout = send_params.get('timeout', 240)

        if response_format is None:
            logger.error("Response format is None for Anthropic structured output.")
            raise ValueError("Invalid response format: None")

        logger.info(f"Using Anthropic model: {model_name} for structured output")
        timer_id = performance_monitor.start_timer('llm_structured_output_anthropic', f"{model_name}_{len(messages)}")

        # llm_call_log telemetry — capture usage from `response.usage` on
        # the Anthropic side. Wrap in finally so failures persist too.
        _telemetry_usage = None
        _telemetry_status = "error"
        _call_started = _time.monotonic()

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
                # Per-call timeout (seconds). Without this Anthropic had no request timeout and
                # could wedge a turn forever (reliability R2).
                timeout=timeout,
            )
            _telemetry_usage = getattr(response, "usage", None)

            raw_text = response.content[0].text.strip()

            # Strip markdown fences if present
            if raw_text.startswith("```"):
                raw_text = re.sub(r"^```[a-z]*\n?", "", raw_text)
                raw_text = re.sub(r"\n?```$", "", raw_text).strip()

            result_dict = json_lib.loads(raw_text)

            performance_monitor.end_timer(timer_id, {'status': 'success', 'model': model_name})
            logger.info("✅ Anthropic response received successfully")
            _telemetry_status = "ok"
            return result_dict

        except Exception as e:
            performance_monitor.end_timer(timer_id, {'status': 'error', 'model': model_name, 'error': str(e)})
            logger.error(f"Anthropic LLM error: {e}", exc_info=True)
            if isinstance(e, (BillingQuotaExhausted, TransientRateLimit)):
                _telemetry_status = "quota" if isinstance(e, BillingQuotaExhausted) else "rate_limit"
                raise
            err_str = str(e).lower()
            if "timeout" in err_str or "timed out" in err_str:
                _telemetry_status = "timeout"
                raise
            canonical = _translate_anthropic_exception(e, self._anthropic_module)
            if canonical is not None:
                if isinstance(canonical, BillingQuotaExhausted):
                    _telemetry_status = "quota"
                    logger.critical(
                        "LLM billing quota exhausted — anthropic model=%s — %s", model_name, e,
                    )
                else:
                    _telemetry_status = "rate_limit"
                raise canonical from e
            if "json" in err_str or "validation" in err_str:
                _telemetry_status = "parse_error"
            else:
                _telemetry_status = "error"
            raise
        finally:
            try:
                from app.services.llm_call_logger import record_llm_call
                duration_ms = int((_time.monotonic() - _call_started) * 1000.0)
                record_llm_call(
                    engine=model_name,
                    provider="anthropic",
                    usage=_telemetry_usage,
                    duration_ms=duration_ms,
                    status=_telemetry_status,
                )
            except Exception as _telemetry_err:
                logger.debug("llm_call_log write skipped (anthropic): %s", _telemetry_err)

    def structured_output_json(self, messages, **send_params):
        return self.structured_output(messages, **send_params)


class QuotaExhaustedError(Exception):
    """Raised when an LLM provider returns a quota/billing error.

    Callers should catch this, notify the user, and stop further LLM calls
    rather than retrying or cascading failures.
    """
    def __init__(self, provider: str, message: str = ""):
        self.provider = provider
        super().__init__(message or f"LLM quota exhausted for provider '{provider}'. Check billing.")


# Global circuit breaker state: provider -> _BreakerState. Absent means closed.
_quota_tripped: dict[str, "_BreakerState"] = {}
_quota_tripped_lock = threading.Lock()

# How long an armed breaker refuses everything before it lets ONE call through
# to find out whether billing recovered. Short enough that a mid-day top-up is
# picked up without anyone watching; long enough that a genuinely empty account
# is not hammered (at most 4 wasted calls an hour).
QUOTA_PROBE_AFTER_S = 15 * 60


class _BreakerState:
    """Bookkeeping for one armed provider.

    ``armed_at`` is monotonic, so a system clock change cannot strand the
    breaker. ``probe_in_flight`` makes the half-open trial single-flight: a
    hundred blocked threads elect exactly ONE prober, not a hundred.
    """

    __slots__ = ("armed_at", "probe_in_flight")

    def __init__(self) -> None:
        self.armed_at = time.monotonic()
        self.probe_in_flight = False


def _quota_refuses_call(provider: str) -> bool:
    """Whether to refuse this call without touching the network.

    Closed breaker -> False. Armed breaker -> True, except that once the
    cooldown has elapsed one caller is elected as the half-open probe and let
    through. Its outcome decides the breaker: success closes it (see
    _note_quota_success), a billing failure re-arms it with a fresh cooldown.
    """
    with _quota_tripped_lock:
        state = _quota_tripped.get(provider)
        if state is None:
            return False
        if state.probe_in_flight:
            return True
        if (time.monotonic() - state.armed_at) < QUOTA_PROBE_AFTER_S:
            return True
        state.probe_in_flight = True
    logger.warning(
        "[quota] cooldown elapsed for provider '%s' — letting ONE trial call through "
        "to test whether billing recovered.", provider,
    )
    return False


def _note_quota_success(provider: str) -> None:
    """Close the breaker after a call succeeds.

    While armed, the elected probe is the only caller that can reach the
    provider at all, so a success here is proof it is serving again. On the hot
    path (breaker closed) this is one unlocked dict membership test.
    """
    if provider not in _quota_tripped:
        return
    with _quota_tripped_lock:
        state = _quota_tripped.pop(provider, None)
    if state is not None:
        logger.warning(
            "[quota] RECOVERED for provider '%s' — trial call succeeded, circuit breaker "
            "closed. Normal LLM traffic resumes.", provider,
        )


def _check_and_trip_quota(provider: str, exc: Exception) -> bool:
    """If *exc* is a confirmed BILLING quota exhaustion, trip the circuit
    breaker for *provider* and return True so the caller raises
    QuotaExhaustedError.

    Billing exhaustion means every subsequent LLM call will also fail. The
    danger is batch loops (KG pipeline, entity cards, etc.) racing through
    items, swallowing the exception, and marking work as done without
    results. The breaker is what prevents that: once tripped, _guard_quota()
    raises QuotaExhaustedError *before* any further call is attempted, so
    those loops fail fast and loudly instead of completing empty.

    This used to `os._exit(1)` instead of returning. That made the process
    die on the first billing 429 — and under `restart: unless-stopped` the
    container simply came back, called the same exhausted provider, and died
    again, roughly every 20 seconds. A crash loop is strictly worse than a
    surfaced error: it takes down the UI and every non-LLM feature too, and
    the hard exit skipped the `raise QuotaExhaustedError(...)` the call sites
    were already written to expect, leaving that path unreachable.

    The judgement rides classify_quota — provider boundaries translate the
    SDKs' structured markers (OpenAI error.code, Gemini QuotaFailure.quotaId)
    into the canonical types, so a per-minute rate window NEVER reaches this
    kill (the retry ladder owns it), while an ambiguous unstructured quota
    mention still stops the world (conservative by policy).
    """
    if classify_quota(exc) != "billing":
        # A half-open probe can fail for reasons that say nothing about billing
        # (network blip, bad request). Release the single-flight slot and restart
        # the cooldown, otherwise no probe is ever elected again and the breaker
        # refuses forever — the exact stall this recovery path exists to avoid.
        with _quota_tripped_lock:
            state = _quota_tripped.get(provider)
            if state is not None and state.probe_in_flight:
                state.probe_in_flight = False
                state.armed_at = time.monotonic()
        return False

    with _quota_tripped_lock:
        already = provider in _quota_tripped
        # (Re)arm with a fresh cooldown. Constructing a new state also clears
        # probe_in_flight, so a probe that found billing still empty releases
        # its slot here.
        _quota_tripped[provider] = _BreakerState()

    if already:
        # Either another thread armed it first, or our probe confirmed billing is
        # still empty. Either way the cooldown just restarted — raise and wait.
        return True

    logger.error(
        "🚨 QUOTA EXHAUSTED for provider '%s'. Circuit breaker tripped — further "
        "LLM calls raise QuotaExhaustedError without touching the network. The app "
        "stays up, and in %d min one trial call goes out to test whether billing "
        "recovered; it closes the breaker by itself if so.",
        provider, QUOTA_PROBE_AFTER_S // 60,
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
                        "title": "API Quota Exhausted",
                        "message": f"The {provider.upper()} API quota has been exceeded. "
                                   "LLM features are paused until billing is restored; "
                                   "everything else keeps running.",
                    },
                },
            ))
    except Exception:
        logger.debug("Could not publish quota alert to UI", exc_info=True)

    # Return True so the caller raises QuotaExhaustedError. The breaker stays
    # armed, so every later call short-circuits in _guard_quota() without
    # touching the network — until the cooldown elects a probe, which recovers
    # on its own. reset_quota_breaker(provider) forces it open immediately.
    return True


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
        if _quota_refuses_call(name):
            raise QuotaExhaustedError(name)

    def structured_output(self, message, use_json=False, **params):
        self._guard_quota()
        try:
            if not use_json:
                response = self.llm_provider.structured_output(message, **params)
            else:
                response = self.llm_provider.structured_output_json(message, **params)
        except QuotaExhaustedError:
            raise
        except Exception as exc:
            if _check_and_trip_quota(self._provider_name, exc):
                raise QuotaExhaustedError(self._provider_name, str(exc)) from exc
            raise
        # Reached the provider and got an answer: if this was the half-open
        # probe, that is proof billing recovered — close the breaker.
        _note_quota_success(self._provider_name)
        return response

    def structured_output_json(self, message, **params):
        self._guard_quota()
        try:
            response = self.llm_provider.structured_output_json(message, **params)
        except QuotaExhaustedError:
            raise
        except Exception as exc:
            if _check_and_trip_quota(self._provider_name, exc):
                raise QuotaExhaustedError(self._provider_name, str(exc)) from exc
            raise
        _note_quota_success(self._provider_name)
        return response



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

