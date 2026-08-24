from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
from pathlib import Path
from typing import Iterable

import pytest
from pydantic import BaseModel


def _assistant_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _iter_agent_form_files() -> Iterable[Path]:
    agents_dir = _assistant_root() / "agents"
    for path in sorted(agents_dir.rglob("agent_form.py")):
        if path.is_file():
            yield path


def _module_name_for_path(path: Path) -> str:
    digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:12]
    stem = path.parent.name.replace("-", "_")
    return f"app.assistant.tests.dynamic.agent_form_{stem}_{digest}"


def _load_agent_form_model(path: Path) -> type[BaseModel] | None:
    module_name = _module_name_for_path(path)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to create import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    # Register BEFORE exec, exactly as the production loader does
    # (agent_registry._load_agent_form). A form written with
    # `from __future__ import annotations` has string annotations that Pydantic
    # resolves by looking its module up in sys.modules; without this the schema
    # build dies with "AgentForm is not fully defined; you should define `List`"
    # BEFORE the contract assertion below ever runs. That hid six forms
    # (belief_engine ×3, daily_summary, task_compile ×2) from validation and
    # left this test permanently red for a reason it does not test.
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    candidate = getattr(module, "AgentForm", None)
    if isinstance(candidate, type) and issubclass(candidate, BaseModel):
        return candidate
    return None


def _collect_agent_form_models() -> list[tuple[str, type[BaseModel]]]:
    models: list[tuple[str, type[BaseModel]]] = []
    for path in _iter_agent_form_files():
        model = _load_agent_form_model(path)
        if model is None:
            continue
        rel = path.relative_to(_assistant_root())
        models.append((str(rel).replace("\\", "/"), model))
    return models


def _selected_models() -> list[tuple[str, type[BaseModel]]]:
    all_models = _collect_agent_form_models()
    only = str(os.environ.get("OPENAI_SCHEMA_TEST_ONLY", "") or "").strip()
    if not only:
        return all_models
    wanted = {x.strip() for x in only.split(",") if x.strip()}
    return [row for row in all_models if row[0] in wanted]


def _bool_env(name: str, default: bool = False) -> bool:
    raw = str(os.environ.get(name, str(default)) or str(default)).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    raw = str(os.environ.get(name, str(default)) or str(default)).strip()
    return int(raw)


def test_agent_form_models_have_strict_required_shape():
    """
    Local contract test:
    every AgentForm should convert to strict JSON schema where `required`
    exactly covers object `properties`.
    """
    from openai.lib._pydantic import to_strict_json_schema

    failures: list[str] = []
    for model_path, model_cls in _selected_models():
        schema = to_strict_json_schema(model_cls)
        props = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(props, dict):
            failures.append(f"{model_path}: strict schema has non-dict properties")
            continue
        if not isinstance(required, list):
            failures.append(f"{model_path}: strict schema has non-list required")
            continue
        prop_keys = set(props.keys())
        req_keys = set(required)
        if prop_keys != req_keys:
            missing = sorted(prop_keys - req_keys)
            extra = sorted(req_keys - prop_keys)
            failures.append(
                f"{model_path}: required/property mismatch missing={missing} extra={extra}"
            )
    assert not failures, "Strict schema validation failures:\n- " + "\n- ".join(failures)


@pytest.mark.skipif(
    not _bool_env("RUN_OPENAI_SCHEMA_TESTS", False),
    reason="Set RUN_OPENAI_SCHEMA_TESTS=1 to run live OpenAI schema parse checks.",
)
def test_openai_responses_parse_accepts_agent_forms():
    """
    Live integration test:
    call OpenAI responses.parse directly with each AgentForm class.

    This isolates schema compatibility in the provider path (no Emi wrappers).
    """
    from openai import OpenAI

    client = OpenAI()
    model_name = str(os.environ.get("OPENAI_SCHEMA_TEST_MODEL", "gpt-5-mini") or "gpt-5-mini").strip()
    timeout_s = _int_env("OPENAI_SCHEMA_TEST_TIMEOUT_S", 60)
    max_models = _int_env("OPENAI_SCHEMA_TEST_MAX_MODELS", 200)

    failures: list[str] = []
    selected = _selected_models()[:max_models]
    for model_path, model_cls in selected:
        try:
            response = client.responses.parse(
                model=model_name,
                input=[
                    {
                        "role": "system",
                        "content": "Return only valid data that matches the provided response schema.",
                    },
                    {
                        "role": "user",
                        "content": "Schema compatibility probe. Fill required fields with minimal valid values.",
                    },
                ],
                text_format=model_cls,
                timeout=timeout_s,
                max_output_tokens=300,
            )
            parsed = getattr(response, "output_parsed", None)
            if parsed is None:
                failures.append(f"{model_path}: output_parsed is None")
        except Exception as e:
            failures.append(f"{model_path}: {type(e).__name__}: {e}")

    assert not failures, (
        "OpenAI responses.parse schema compatibility failures:\n- "
        + "\n- ".join(failures)
    )


@pytest.mark.skipif(
    not _bool_env("RUN_OPENAI_SCHEMA_TESTS", False),
    reason="Set RUN_OPENAI_SCHEMA_TESTS=1 to run live OpenAI schema parse checks.",
)
def test_openai_direct_parse_rejects_dict_str_str_schema():
    from openai import OpenAI
    from pydantic import create_model

    client = OpenAI()
    model_name = str(os.environ.get("OPENAI_SCHEMA_TEST_MODEL", "gpt-5-mini") or "gpt-5-mini").strip()

    DictModel = create_model("DictModel", summary_by_id=(dict[str, str], ...))
    with pytest.raises(Exception) as exc_info:
        client.responses.parse(
            model=model_name,
            input=[
                {"role": "system", "content": "Return valid JSON only."},
                {"role": "user", "content": "Return one id->summary mapping."},
            ],
            text_format=DictModel,
            timeout=60,
            max_output_tokens=200,
        )
    err_text = str(exc_info.value)
    assert "invalid_json_schema" in err_text or "Invalid schema" in err_text


@pytest.mark.skipif(
    not _bool_env("RUN_OPENAI_SCHEMA_TESTS", False),
    reason="Set RUN_OPENAI_SCHEMA_TESTS=1 to run live OpenAI schema parse checks.",
)
def test_openai_direct_parse_accepts_object_pair_list_schema():
    from openai import OpenAI
    from pydantic import BaseModel, create_model

    class SummaryPair(BaseModel):
        history_id: str
        summary: str

    client = OpenAI()
    model_name = str(os.environ.get("OPENAI_SCHEMA_TEST_MODEL", "gpt-5-mini") or "gpt-5-mini").strip()
    PairListModel = create_model("PairListModel", summary_pairs=(list[SummaryPair], ...))
    response = client.responses.parse(
        model=model_name,
        input=[
            {"role": "system", "content": "Return valid JSON only."},
            {"role": "user", "content": "Return one summary pair object with history_id and summary."},
        ],
        text_format=PairListModel,
        timeout=60,
        max_output_tokens=800,
    )
    parsed = getattr(response, "output_parsed", None)
    assert parsed is not None, "Expected output_parsed for object-pair list schema."

