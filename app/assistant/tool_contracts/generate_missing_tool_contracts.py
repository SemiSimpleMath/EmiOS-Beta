from __future__ import annotations

import json
from importlib import util as import_util
from pathlib import Path
from typing import Any, Union, get_args, get_origin
import types

from pydantic import BaseModel

from app.assistant.utils.path_utils import get_repo_root

def _type_name(annotation: Any) -> str:
    if annotation is None:
        return "any"
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (Union, types.UnionType):
        non_none = [a for a in args if a is not type(None)]  # noqa: E721
        if len(non_none) == 1:
            return _type_name(non_none[0])
        return "any"
    if origin in (list, tuple, set):
        return "array"
    if origin is dict:
        return "object"
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return "object"
    mapping: dict[Any, str] = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        dict: "object",
        list: "array",
        Any: "any",
    }
    if annotation in mapping:
        return mapping[annotation]
    if origin is not None and "Literal" in str(origin):
        return "string"
    return "any"


def _load_args_model(tool_dir: Path):
    tool_name = tool_dir.name
    form_file = tool_dir / "tool_forms" / "tool_forms.py"
    if not form_file.exists():
        raise FileNotFoundError(f"Missing tool form file: {form_file}")
    mod_name = f"_contract_gen_{tool_name}_form"
    spec = import_util.spec_from_file_location(mod_name, str(form_file))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module spec: {form_file}")
    module = import_util.module_from_spec(spec)
    spec.loader.exec_module(module)
    args_name = f"{tool_name}_args"
    args_model = getattr(module, args_name, None)
    if not isinstance(args_model, type) or not issubclass(args_model, BaseModel):
        raise RuntimeError(f"{form_file}: missing Pydantic model '{args_name}'")
    return args_model


def _description_from_prompt(tool_dir: Path) -> str:
    p = tool_dir / "prompts" / f"{tool_dir.name}_description.j2"
    if not p.exists():
        raise FileNotFoundError(f"Missing description prompt: {p}")
    text = p.read_text(encoding="utf-8").strip()
    if not text:
        raise RuntimeError(f"Empty description prompt: {p}")
    return text


def _arguments_prompt(tool_dir: Path) -> str | None:
    p = tool_dir / "prompts" / f"{tool_dir.name}_args.j2"
    if not p.exists():
        return None
    text = p.read_text(encoding="utf-8").strip()
    return text or None


def _build_contract(tool_dir: Path) -> dict[str, Any]:
    tool_name = tool_dir.name
    args_model = _load_args_model(tool_dir)
    inputs: list[dict[str, Any]] = []
    for field_name, finfo in args_model.model_fields.items():
        name = str(field_name).strip()
        if not name:
            continue
        annotation = getattr(finfo, "annotation", Any)
        inputs.append(
            {
                "name": name,
                "type": _type_name(annotation),
                "required": bool(getattr(finfo, "is_required", lambda: False)()),
                "description": str(getattr(finfo, "description", "") or "").strip(),
            }
        )

    contract: dict[str, Any] = {
        "name": tool_name,
        "description": _description_from_prompt(tool_dir),
        "inputs": inputs,
        "outputs": [
            {
                "path": "content",
                "type": "text",
                "description": "Human-readable tool result text.",
            },
            {
                "path": "data",
                "type": "object",
                "description": "Structured tool result payload.",
            },
        ],
    }
    ap = _arguments_prompt(tool_dir)
    if isinstance(ap, str) and ap.strip():
        contract["arguments_prompt"] = ap.strip()
    return contract


def main() -> int:
    repo_root = get_repo_root()
    tools_root = repo_root / "app" / "assistant" / "lib" / "tools"
    if not tools_root.exists():
        raise FileNotFoundError(f"Tools root not found: {tools_root}")

    generated: list[str] = []
    for tool_dir in sorted([p for p in tools_root.iterdir() if p.is_dir()]):
        if (tool_dir / ".disabled").exists():
            continue
        if not (tool_dir / f"{tool_dir.name}.py").exists():
            continue
        contract_file = tool_dir / "tool_contract.json"
        if contract_file.exists():
            continue
        contract = _build_contract(tool_dir)
        contract_file.write_text(json.dumps(contract, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
        generated.append(tool_dir.name)

    print(f"generated_contracts={len(generated)}")
    for name in generated:
        print(name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

