"""Materializer registry — turns a raw secret value into a set of
authority-banded projections at pod-creation time.

When the user creates an identity pod ("here's my SSN"), the
materializer for that pod type is invoked. It receives:
  - the pod type (e.g., 'identity.ssn')
  - a context dict with whatever the user supplied, typically
    including `env_ref` (the env var name where the raw value lives)
    and `raw_value` (the actual value, available ONLY at materialize
    time — read once from os.environ[env_ref], used to compute
    derived projections, never persisted to Python globals).

It returns a list of `ProjectionSpec` records — one per projection
to be inserted into the pod_projection table.

## The "raw_value read once" invariant

At materialize time the codebase HAS to see the raw value once to
compute derived projections (last4 from full SSN, area code from
phone number, etc.). The contract is:

  1. The materialize function receives raw_value as a parameter.
  2. It computes all derived projections, returning a list of
     ProjectionSpec.
  3. The full-value projection is recorded as `storage_kind='env'`
     with `env_ref` pointing back at the env var. NOT as
     `storage_kind='plain', plain_value=<the raw value>`.
  4. After materialize returns, the caller releases its local
     reference to raw_value. No persistence beyond the env var.

So the only enduring storage of the raw value is the env var the
user set. The Python process holds it transiently during
materialization and forgets.

## Adding a new pod type

  1. Create a file under this directory.
  2. Define a `materialize(*, raw_value: str, **kwargs) -> List[ProjectionSpec]`
     function with the projections that pod type produces.
  3. Register it with `register('your.type.name', materialize)`.

That's it. The registry is module-level; importing the file
side-effects the registration. The package's __init__.py auto-imports
known types so they're available without explicit imports at call
sites.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional


@dataclass(frozen=True)
class ProjectionSpec:
    """One row's worth of pod_projection data, pre-DB.

    `storage_kind` determines which of the value-carrying fields is
    populated (mutually exclusive):

      'plain' → plain_value (low-authority derived projections)
      'env'   → env_ref (pointer to env var; raw value lives there)
      'file'  → file_ref (pointer to data/pod_secrets/<file_ref>)
    """
    projection_name: str
    min_authority: int
    storage_kind: str
    plain_value: Optional[str] = None
    env_ref: Optional[str] = None
    file_ref: Optional[str] = None


# Materialize function signature.
MaterializeFn = Callable[..., List[ProjectionSpec]]


_REGISTRY: Dict[str, MaterializeFn] = {}


def register(pod_type: str, fn: MaterializeFn) -> None:
    """Register a materializer for a pod type. Idempotent on identical
    function; re-registration with a different function raises."""
    existing = _REGISTRY.get(pod_type)
    if existing is not None and existing is not fn:
        raise ValueError(
            f"materializer for pod type {pod_type!r} already registered "
            f"({existing!r}); cannot replace with {fn!r}"
        )
    _REGISTRY[pod_type] = fn


def get_materializer(pod_type: str) -> MaterializeFn:
    """Return the registered materializer for a pod type. Raises if
    nothing is registered — the caller is asking to create a pod
    type the system has no projection plan for."""
    fn = _REGISTRY.get(pod_type)
    if fn is None:
        raise KeyError(
            f"no materializer registered for pod type {pod_type!r}. "
            f"Known types: {sorted(_REGISTRY)}"
        )
    return fn


def known_types() -> List[str]:
    """Return the list of registered pod types."""
    return sorted(_REGISTRY)


# Auto-register all built-in types via side-effecting imports.
# Add new pod-type modules to this list when you create them.
from app.assistant.pod_store.materializers import identity_ssn  # noqa: F401,E402
