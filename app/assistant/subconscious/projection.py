"""A prompt as a projection of objects: labels for one model call (docs/design/objects_and_projections_2026-10-08.md).

Owner, 2026-10-08: "each prompt needs to be a projection of these objects you create and objects have id and then
data fields"; "ids identify objects they are not part of the data"; "short labels are never written into the
prose. you have to have the schema write the pairs with ids separate from other data."

Code gives each object shown in one call a short label (a kind prefix and a number: W3, C1), the same object always
the same label within the call; the prompt shows the label and the object's data, never its id. The model refers to
objects only through answer fields that hold labels; code maps them back to (kind, id). Labels live in one call and
are never stored. `labels` is a plain dict (label -> [kind, id]) so it travels in the agent's input as JSON.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

Ref = Tuple[str, str]           # (kind, id)


class Projection:
    """The labels of one model call."""

    def __init__(self) -> None:
        self.labels: Dict[str, List[str]] = {}
        self._by_ref: Dict[Ref, str] = {}
        self._counts: Dict[str, int] = {}

    def label(self, prefix: str, kind: str, obj_id: str) -> str:
        """The object's label in this call: its existing one, or the next free one of `prefix`."""
        ref = (kind, str(obj_id))
        if ref in self._by_ref:
            return self._by_ref[ref]
        n = self._counts.get(prefix, 0)
        label = f"{prefix}{n + 1}"
        while label in self.labels:
            n += 1
            label = f"{prefix}{n + 1}"
        self._counts[prefix] = n + 1
        return self._set(label, ref)

    def fixed(self, label: str, kind: str, obj_id: str) -> str:
        """A label the caller already numbered (the day's placements, I1..; the standing directives, D)."""
        ref = (kind, str(obj_id))
        if label in self.labels and tuple(self.labels[label]) != ref:
            raise ValueError(f"projection: label {label} already names {self.labels[label]}")
        return self._set(label, ref)

    def label_of(self, kind: str, obj_id: str) -> Optional[str]:
        return self._by_ref.get((kind, str(obj_id)))

    def _set(self, label: str, ref: Ref) -> str:
        self.labels[label] = [ref[0], ref[1]]
        self._by_ref[ref] = label
        return label


def resolve(labels: Dict[str, List[str]], label: str) -> Optional[Ref]:
    """(kind, id) of a label given in the call, or None."""
    ref = labels.get(str(label).strip())
    return (ref[0], ref[1]) if ref else None


def labels_in(text: str, labels: Iterable[str]) -> List[str]:
    """The call's labels written into prose, as whole words (a label is letters and digits)."""
    words = set("".join(ch if ch.isalnum() else " " for ch in str(text or "")).split())
    return sorted(set(labels) & words)
