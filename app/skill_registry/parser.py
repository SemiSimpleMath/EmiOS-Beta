"""Parse + validate a SKILL.md file per the agentskills.io spec.

Validation rules applied (see ``docs/architecture/21_SKILLS.md`` and
<https://agentskills.io/specification>):

  name:
    - 1-64 characters
    - lowercase alphanumeric + hyphens only ([a-z0-9-])
    - cannot start or end with a hyphen
    - no consecutive hyphens (--)
    - MUST match the parent directory name

  description:
    - 1-1024 characters
    - non-empty after stripping

  license, compatibility, metadata, allowed_tools — optional, well-typed.

  body:
    - any markdown after the frontmatter
    - empty body warns but doesn't fail

Errors block loading; warnings are advisory.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Tuple

import frontmatter

from app.skill_registry.models import AutoInjectTrigger, Skill, ValidationResult


# Per spec: 1-64 chars, [a-z0-9], hyphens allowed but not at edges and no --
_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_CONSECUTIVE_HYPHEN_RE = re.compile(r"--")

_NAME_MAX = 64
_DESCRIPTION_MAX = 1024
_COMPATIBILITY_MAX = 500

# requires_scope may gate ONLY on identity/context ScopeContext fields — never the
# permission bucket (tools/pods/approval/writes/resources/entities/cards). Gating a
# skill on permission would make skills a backdoor authorization lever; skills gate
# RELEVANCE, not access. Explicit + reviewable; widen only deliberately.
_REQUIRES_SCOPE_ALLOWED_FIELDS = frozenset(
    {"acting_as", "surface", "room_id", "room_context_id", "visibility"}
)


def parse_skill_md(path: Path) -> Tuple[Optional[Skill], ValidationResult]:
    """Parse and validate one SKILL.md file.

    Returns ``(skill, result)``. ``skill`` is None when validation fails.
    ``result.ok`` is True iff no errors. Warnings always advisory.

    The skill's parent directory name is the canonical identity; the
    frontmatter ``name`` field MUST match it (per spec). This is checked
    here, not later, because the registry indexes by name and a mismatch
    would cause silent miswiring.
    """
    skill_dir = path.parent
    dir_name = skill_dir.name
    result = ValidationResult(ok=False, skill_dir=str(skill_dir))

    # Read file
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        result.errors.append(f"Could not read {path}: {exc}")
        return None, result

    # Parse frontmatter
    try:
        post = frontmatter.loads(text)
    except Exception as exc:
        result.errors.append(f"Failed to parse YAML frontmatter: {exc}")
        return None, result

    fm = post.metadata or {}
    body = (post.content or "").lstrip("\n")

    # name: required + spec rules + dir match
    name = fm.get("name")
    if not isinstance(name, str) or not name:
        result.errors.append("Frontmatter 'name' is required (non-empty string).")
    else:
        if len(name) > _NAME_MAX:
            result.errors.append(f"Frontmatter 'name' exceeds {_NAME_MAX} chars: {len(name)}.")
        if not _NAME_RE.match(name):
            result.errors.append(
                f"Frontmatter 'name' must match [a-z0-9-], no leading/trailing hyphens. Got: {name!r}"
            )
        if _CONSECUTIVE_HYPHEN_RE.search(name):
            result.errors.append(f"Frontmatter 'name' contains consecutive hyphens: {name!r}")
        if name != dir_name:
            result.errors.append(
                f"Frontmatter 'name' ({name!r}) must match parent directory name ({dir_name!r})."
            )

    # description: required + length cap
    description = fm.get("description")
    if not isinstance(description, str) or not description.strip():
        result.errors.append("Frontmatter 'description' is required (non-empty string).")
    elif len(description) > _DESCRIPTION_MAX:
        result.errors.append(
            f"Frontmatter 'description' exceeds {_DESCRIPTION_MAX} chars: {len(description)}."
        )

    # Optional fields — well-typed when present
    license_field = fm.get("license")
    if license_field is not None and not isinstance(license_field, str):
        result.errors.append("Frontmatter 'license' must be a string when provided.")

    compatibility = fm.get("compatibility")
    if compatibility is not None:
        if not isinstance(compatibility, str):
            result.errors.append("Frontmatter 'compatibility' must be a string when provided.")
        elif len(compatibility) > _COMPATIBILITY_MAX:
            result.errors.append(
                f"Frontmatter 'compatibility' exceeds {_COMPATIBILITY_MAX} chars: {len(compatibility)}."
            )

    md = fm.get("metadata")
    metadata: dict = {}
    if md is not None:
        if not isinstance(md, dict):
            result.errors.append("Frontmatter 'metadata' must be a mapping when provided.")
        else:
            metadata = md

    allowed_tools = fm.get("allowed-tools") or fm.get("allowed_tools")
    if allowed_tools is not None and not isinstance(allowed_tools, str):
        result.errors.append("Frontmatter 'allowed-tools' must be a string when provided.")
    elif allowed_tools is not None:
        # Accepted for agentskills.io portability, carried on the header, but
        # EmiOS does not enforce it — tool permission flows from ScopeContext
        # (allowed_tools/per_manager), never from a skill. Warn so an author
        # doesn't mistake it for a working control (2026-07-07 skills audit).
        result.warnings.append(
            "'allowed-tools' is accepted for spec portability but NOT enforced by "
            "EmiOS — tool permission comes from the scope contract, not skills."
        )

    # auto_inject_when (parsed from metadata.auto_inject_when) — EmiOS extension.
    # Per spec, metadata is free-form; this key is invisible to other clients
    # but lets the SkillInjector know when to attach this skill automatically.
    auto_inject = None
    raw_auto = metadata.get("auto_inject_when") if isinstance(metadata, dict) else None
    if raw_auto is not None:
        if not isinstance(raw_auto, dict):
            result.errors.append("metadata.auto_inject_when must be a mapping when provided.")
        else:
            kw = raw_auto.get("task_keywords")
            if kw is not None and not (
                isinstance(kw, list) and all(isinstance(k, str) and k for k in kw)
            ):
                result.errors.append(
                    "metadata.auto_inject_when.task_keywords must be a list of non-empty strings."
                )
            else:
                principal_gate = raw_auto.get("requires_scope_acting_as")
                if principal_gate is not None and not (isinstance(principal_gate, str) and principal_gate.strip()):
                    result.errors.append(
                        "metadata.auto_inject_when.requires_scope_acting_as must be a non-empty string when provided."
                    )
                # Generic requires_scope gate. Identity/context bucket ONLY —
                # permission fields are forbidden (skills gate relevance, never
                # authorization). Fail-loud on any non-allowlisted field.
                req_scope: dict = {}
                raw_req = raw_auto.get("requires_scope")
                if raw_req is not None and not isinstance(raw_req, dict):
                    result.errors.append(
                        "metadata.auto_inject_when.requires_scope must be a mapping when provided."
                    )
                elif isinstance(raw_req, dict):
                    for fld, val in raw_req.items():
                        if fld not in _REQUIRES_SCOPE_ALLOWED_FIELDS:
                            result.errors.append(
                                f"metadata.auto_inject_when.requires_scope: field {fld!r} is not "
                                f"allowed. Allowed (identity/context only): "
                                f"{sorted(_REQUIRES_SCOPE_ALLOWED_FIELDS)}. Permission/authority "
                                f"fields are forbidden — skills gate relevance, not authorization."
                            )
                            continue
                        if not (isinstance(val, str) and val.strip()):
                            result.errors.append(
                                f"metadata.auto_inject_when.requires_scope.{fld} must be a non-empty string."
                            )
                            continue
                        req_scope[fld] = val.strip().lower()
                # requires_scope_acting_as is sugar for requires_scope.acting_as.
                if principal_gate and isinstance(principal_gate, str) and principal_gate.strip():
                    req_scope.setdefault("acting_as", principal_gate.strip().lower())
                if not result.errors:
                    auto_inject = AutoInjectTrigger(
                        task_keywords=[str(k).lower() for k in (kw or [])],
                        requires_scope_acting_as=(str(principal_gate).strip().lower() if principal_gate else None),
                        requires_scope=req_scope,
                    )

    # Body — empty warns but does not block
    if not body.strip():
        result.warnings.append("SKILL.md body is empty — skill will inject nothing useful.")

    if result.errors:
        return None, result

    skill = Skill(
        name=str(name),
        description=str(description).strip(),
        license=license_field,
        compatibility=compatibility,
        metadata=metadata,
        allowed_tools=allowed_tools,
        auto_inject_when=auto_inject,
        skill_dir=str(skill_dir),
        body=body,
    )
    result.ok = True
    return skill, result
