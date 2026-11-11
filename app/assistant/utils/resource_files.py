"""The one way to read, list and write resource files.

Resources live in two roots:

- the CODE root (``get_seed_resources_dir()``, ``<repo>/resources``): what ships with
  the assistant. Read-only to this module.
- the USER root (``get_resources_dir()``, ``<EMI_DATA_DIR>/resources``): the owner's
  files and what the assistant writes. In a dev checkout without ``EMI_DATA_DIR`` both roots are
  the same directory.

A resource is named by its path relative to a root (``rel``), e.g.
``"instructions/resource_owner_escalation.md"``. Four kinds share one rule set:

1. Shipped text: ``<code>/<rel>``. Read as shipped.
2. Template the owner may replace: ``<code>/<rel>.example``. Read until the owner has
   their own ``<user>/<rel>``; from then on theirs is read.
3. Shipped text plus the owner's add-on: ``<user>/<stem>_personal<suffix>`` is appended
   after the text of the resource (shipped, template or owner's).
4. Written by the assistant: ``<user>/<rel>``.

Read order for ``rel``: ``<user>/<rel>``, else ``<code>/<rel>.example``, else
``<code>/<rel>``; then the add-on, if one exists. Every write goes to the user root.
Nothing here deletes a file, and a write never targets the code root unless both roots
are the same directory (dev checkout).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator, Optional

from app.assistant.utils.atomic_write import write_json_atomic, write_text_atomic
# Roots are looked up through the module at call time, so patching path_utils redirects them.
from app.assistant.utils import path_utils

TEMPLATE_SUFFIX = ".example"
ADDON_MARKER = "_personal"


def code_root() -> Path:
    return path_utils.get_seed_resources_dir()


def user_root() -> Path:
    return path_utils.get_resources_dir()


def _rel_path(rel: str | Path) -> Path:
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts:
        raise ValueError(f"resource path must be relative to the resources root: {rel!r}")
    return p


def addon_rel(rel: str | Path) -> Path:
    """The ``<stem>_personal<suffix>`` add-on name for a resource."""
    p = _rel_path(rel)
    return p.with_name(f"{p.stem}{ADDON_MARKER}{p.suffix}")


def is_addon(rel: str | Path) -> bool:
    return Path(rel).stem.endswith(ADDON_MARKER)


def base_path(rel: str | Path) -> Optional[Path]:
    """The file whose text is the resource before any add-on, or None if there is none."""
    p = _rel_path(rel)
    user_file = user_root() / p
    if user_file.is_file():
        return user_file
    template = code_root() / p.with_name(p.name + TEMPLATE_SUFFIX)
    if template.is_file():
        return template
    shipped = code_root() / p
    if shipped.is_file():
        return shipped
    return None


def addon_path(rel: str | Path) -> Optional[Path]:
    """The owner's add-on file for this resource, if it exists."""
    candidate = user_root() / addon_rel(rel)
    return candidate if candidate.is_file() else None


def source_paths(rel: str | Path) -> list[Path]:
    """Every file the resource's value is made from, base first."""
    paths = []
    base = base_path(rel)
    if base is not None:
        paths.append(base)
    addon = addon_path(rel)
    if addon is not None:
        paths.append(addon)
    return paths


def exists(rel: str | Path) -> bool:
    return base_path(rel) is not None


def mtime(rel: str | Path) -> float:
    """Newest modification time across the resource's files. Raises if it has none."""
    paths = source_paths(rel)
    if not paths:
        raise FileNotFoundError(f"resource not found: {rel}")
    return max(p.stat().st_mtime for p in paths)


def _parse(path: Path, rel: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if rel.suffix.lower() == ".json":
        return json.loads(text)
    return text


def read_base(rel: str | Path) -> Any:
    """The resource without its add-on. Raises FileNotFoundError if it does not exist."""
    p = _rel_path(rel)
    base = base_path(p)
    if base is None:
        raise FileNotFoundError(
            f"resource not found: {p} (looked in {user_root()} and {code_root()}, "
            f"including the {TEMPLATE_SUFFIX} template)"
        )
    return _parse(base, p)


def read_addon(rel: str | Path) -> str:
    """The owner's add-on text, or "" when they have none."""
    addon = addon_path(rel)
    return addon.read_text(encoding="utf-8") if addon is not None else ""


def join_addon(base: Any, addon: str) -> Any:
    """Append the add-on after text, separated by a blank line. JSON values take no add-on."""
    if not isinstance(base, str) or not addon.strip():
        return base
    return base.rstrip() + "\n\n" + addon.lstrip()


def read(rel: str | Path) -> Any:
    """The resource's value: JSON as parsed data, everything else as text, with the
    owner's add-on appended. Raises FileNotFoundError if the resource does not exist."""
    return join_addon(read_base(rel), read_addon(rel))


def _iter_files(root: Path) -> Iterator[Path]:
    if not root.is_dir():
        return
    for path in root.rglob("*"):
        if path.is_file():
            yield path.relative_to(root)


def list_resources(subdir: str | Path = "") -> list[Path]:
    """Every resource under ``subdir`` (relative), from both roots, named as ``read``
    expects: templates without their ``.example`` suffix, add-ons left out."""
    sub = _rel_path(subdir) if str(subdir) else Path()
    found: set[Path] = set()
    for root in (code_root(), user_root()):
        for rel in _iter_files(root / sub):
            rel = sub / rel
            if rel.suffix == TEMPLATE_SUFFIX:
                rel = rel.with_suffix("")
            if is_addon(rel):
                continue
            found.add(rel)
    return sorted(found)


def _atomic_write(path: Path, rel: Path, value: Any) -> Path:
    if rel.suffix.lower() == ".json":
        write_json_atomic(path, value)
    else:
        if not isinstance(value, str):
            raise TypeError(f"text resource {rel} needs a str, got {type(value).__name__}")
        write_text_atomic(path, value)
    return path


def write(rel: str | Path, value: Any) -> Path:
    """Write the owner's version of a resource, or a resource the assistant produces, into the user
    root. From then on it is read instead of any shipped text or template."""
    p = _rel_path(rel)
    if p.suffix == TEMPLATE_SUFFIX:
        raise ValueError(f"templates are shipped with the code and never written: {p}")
    if is_addon(p):
        raise ValueError(f"write add-ons with write_addon(): {p}")
    return _atomic_write(user_root() / p, p, value)


def write_addon(rel: str | Path, text: str) -> Path:
    """Write the owner's add-on for a resource into the user root."""
    p = addon_rel(rel)
    return _atomic_write(user_root() / p, p, text)


def user_path(rel: str | Path) -> Path:
    """Where the user root holds ``rel`` (whether or not it exists yet). For code that
    must hand a path to something else, such as a subprocess or a file server."""
    return user_root() / _rel_path(rel)
