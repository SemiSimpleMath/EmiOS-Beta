"""Materializer for `identity.ssn` pod type.

Takes a raw SSN (read once at create time from an env var) and
produces five projections at descending authority bands:

  full       100 (courier-only)    "123-45-6789"          via env_ref
  area_code   70                   "123"                  plain
  last4       50                   "6789"                 plain
  redacted    10                   "***-**-6789"          plain
  format      10                   "9 digits, optional    plain
                                    dashes"

The raw SSN is never persisted plaintext anywhere — the only enduring
storage is the env var the user set. The materializer reads it once,
computes the plain projections (which are non-sensitive in isolation —
4 digits or a 3-digit area code don't unlock anything without the
rest), and writes only the pointer for the full projection.
"""
from __future__ import annotations

import re
from typing import List

from app.assistant.pod_store.authority import (
    AUTH_PUBLIC, AUTH_CHAT, AUTH_GATED, AUTH_COURIER,
)
from app.assistant.pod_store.materializers import ProjectionSpec, register


# Matches both unformatted (123456789) and dashed (123-45-6789) SSNs.
_SSN_PATTERN = re.compile(r"^(\d{3})-?(\d{2})-?(\d{4})$")


def materialize(*, raw_value: str, env_ref: str, **_kwargs) -> List[ProjectionSpec]:
    """Compute projections from a raw SSN string. Returns the spec list
    that PodStore.put_secret_pod writes to pod_projection.

    Args:
        raw_value: the SSN string as read from the env var. Must match
            `\\d{3}-?\\d{2}-?\\d{4}`. Used to compute the derived
            projections, then released — not persisted.
        env_ref: the env var name where raw_value lives. Recorded on
            the full-projection row so courier reads can re-resolve.

    Raises ValueError on malformed input — the user gets a clear error
    at setup time rather than a confused pod with partial projections.
    """
    if not raw_value:
        raise ValueError("identity.ssn materialize: raw_value is empty")
    m = _SSN_PATTERN.match(raw_value.strip())
    if not m:
        raise ValueError(
            f"identity.ssn materialize: raw_value {raw_value!r} doesn't "
            f"match SSN format (9 digits, optional dashes)"
        )
    area, group, serial = m.group(1), m.group(2), m.group(3)

    return [
        # Full value — pointer only, no inline. Courier reads via env.
        ProjectionSpec(
            projection_name="full",
            min_authority=AUTH_COURIER,
            storage_kind="env",
            env_ref=env_ref,
        ),
        # Area code (first 3 digits) — sometimes used in forms that
        # only ask for a partial / state-routing question.
        ProjectionSpec(
            projection_name="area_code",
            min_authority=AUTH_GATED,
            storage_kind="plain",
            plain_value=area,
        ),
        # Last 4 — the canonical "confirm SSN ending in ****" projection.
        # Chat-surface authority; agents can render this in confirmations.
        ProjectionSpec(
            projection_name="last4",
            min_authority=AUTH_CHAT,
            storage_kind="plain",
            plain_value=serial,
        ),
        # Redacted display value — safe to render anywhere. Useful for
        # ticket previews ("filling SSN ***-**-6789, proceed?").
        ProjectionSpec(
            projection_name="redacted",
            min_authority=AUTH_PUBLIC,
            storage_kind="plain",
            plain_value=f"***-**-{serial}",
        ),
        # Format description — for form-fill agents that need to confirm
        # "yes the pod's content is 9 digits in SSN shape" without
        # reading any actual digits.
        ProjectionSpec(
            projection_name="format",
            min_authority=AUTH_PUBLIC,
            storage_kind="plain",
            plain_value="9 digits, optional dashes (US SSN)",
        ),
    ]


register("identity.ssn", materialize)
