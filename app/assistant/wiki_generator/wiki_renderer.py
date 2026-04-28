"""Deterministic markdown renderer for entity wiki pages.

Takes an ``EntityNeighborhood`` and produces a complete Obsidian-compatible
markdown string with section markers that later passes (LLM prose, manual
edits) can respect.

Section marker convention:
  <!-- WIKI:DET <slug> -->  ... <!-- /WIKI:DET -->    re-rendered every regen
  <!-- WIKI:AUTO <slug> --> ... <!-- /WIKI:AUTO -->   preserved across regens

KG gaps detected during traversal are emitted as HTML comments inside the
relevant section (invisible in rendered view, greppable in raw markdown).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, List, Optional

from app.assistant.kg_projection import (
    BeliefAbout,
    EntityNeighborhood,
    EventConnection,
    GoalTargeting,
    KGGap,
    StateConnection,
)


# ---------- public entry points ----------


def render_page(neighborhood: EntityNeighborhood) -> str:
    """Return a full markdown page for the entity."""
    parts: List[str] = []
    parts.append(_render_frontmatter(neighborhood))
    parts.append(f"# {neighborhood.entity.label}\n")
    parts.append(_render_summary_section(neighborhood))
    parts.append(_render_relationships_section(neighborhood))
    parts.append(_render_events_section(neighborhood))
    parts.append(_render_beliefs_section(neighborhood))
    parts.append(_render_goals_section(neighborhood))
    parts.append(_render_key_facts_section(neighborhood))
    parts.append(_render_see_also_section(neighborhood))
    parts.append(_render_gaps_section(neighborhood))
    return "\n".join(p for p in parts if p)


# ---------- helpers ----------


def _slugify_for_link(label: str) -> str:
    """Produce an Obsidian wiki-link target from an entity label. Keep it human-readable."""
    return label.strip()


def _fmt_date(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    try:
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return str(dt)


def _sort_key_ts(dt: Optional[datetime]) -> float:
    """Mixed tz-aware/tz-naive datetimes can't be compared directly.
    Normalize to a POSIX timestamp for sort purposes only.
    ``None`` becomes 0 (so callers can order empty dates last/first as they wish)."""
    if dt is None:
        return 0.0
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def _det_open(slug: str) -> str:
    return f"<!-- WIKI:DET {slug} -->"


def _det_close() -> str:
    return "<!-- /WIKI:DET -->"


def _auto_open(slug: str) -> str:
    return f"<!-- WIKI:AUTO {slug} -->"


def _auto_close() -> str:
    return "<!-- /WIKI:AUTO -->"


def _comment(text: str) -> str:
    return f"<!-- {text} -->"


def _read_metadata_bits(node, edge) -> str:
    """Render the temporal frame of a State/Event/Goal bullet for downstream agents.

    Surfaces (when populated):
      - start_date_prose: LLM-extracted natural-language anchor when
        start_date itself couldn't be resolved (e.g. "after moving to
        Irvine"). Critical so the writer doesn't assume present-tense.
      - end_date / end_date_prose: same idea on the close.
      - valid_during: explicit validity-window prose if provided by the
        extractor.
      - utterance: when the source message was authored. Source-relative
        phrases in original_sentence ("last year") were already resolved
        INTO start_date against utterance — re-applying them on top is a
        double-count. The writer should NOT subtract from start_date.
      - date_confidence: e.g. heuristic_evidence / inferred / explicit.
    """
    bits: List[str] = []
    start_prose = getattr(node, "start_date_prose", None)
    if start_prose:
        bits.append(f"start_date_prose: {start_prose}")
    end_dt = getattr(node, "end_date", None)
    if end_dt is not None:
        end_fmt = _fmt_date(end_dt)
        if end_fmt:
            bits.append(f"end_date: {end_fmt}")
    end_prose = getattr(node, "end_date_prose", None)
    if end_prose:
        bits.append(f"end_date_prose: {end_prose}")
    valid_during = getattr(node, "valid_during", None)
    if valid_during:
        bits.append(f"valid_during: {valid_during}")
    utt_ts = getattr(edge, "original_message_timestamp", None) if edge is not None else None
    if utt_ts is not None:
        utt_fmt = _fmt_date(utt_ts)
        if utt_fmt:
            bits.append(f"utterance: {utt_fmt}")
    date_conf = getattr(node, "start_date_confidence", None)
    if date_conf:
        bits.append(f"date_confidence: {date_conf}")
    return "; ".join(bits)


# ---------- frontmatter ----------


def _render_frontmatter(n: EntityNeighborhood) -> str:
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        "---",
        f"name: {n.entity.label}",
        f"kg_node_id: {n.entity.id}",
        f"node_type: {n.entity.node_type}",
        f"generated_at: {generated_at}",
        "auto_generated: true",
        "do_not_edit: true",
    ]
    if n.entity.category:
        lines.append(f"category: {n.entity.category}")
    aliases = list(n.entity.aliases or [])
    if aliases:
        lines.append(f"aliases: {aliases}")
    # Counts include inbound — passive entities (locations, institutions)
    # are mostly the target of activity, not the source.
    lines.append(f"relationship_count: {len(n.relationships) + len(n.inbound_relationships)}")
    lines.append(f"event_count: {len(n.events) + len(n.inbound_events)}")
    lines.append(f"kg_gap_count: {len(n.kg_gaps)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


# ---------- summary ----------


def _render_summary_section(n: EntityNeighborhood) -> str:
    # Use the entity_card summary if available (LLM-authored once, at card
    # generation time). Fall back to the node's own description.
    summary = ""
    if n.entity_card and n.entity_card.get("summary"):
        summary = str(n.entity_card["summary"]).strip()
    elif n.entity.description:
        summary = str(n.entity.description).strip()

    body = summary if summary else _comment("No summary — entity_card.summary and node.description are both empty.")
    return "\n".join([
        _det_open("summary"),
        "## Summary",
        "",
        body,
        "",
        _det_close(),
    ])


# ---------- relationships ----------


def _render_relationships_section(n: EntityNeighborhood) -> str:
    lines: List[str] = [_det_open("relationships"), "## Relationships", ""]

    # Outbound + inbound: passive entities (locations, institutions) are
    # mostly the target of states, not the source. Both directions feed the
    # section so the LLM section writer can describe them.
    all_rels = list(n.relationships) + list(n.inbound_relationships)
    dated = [r for r in all_rels if r.state.start_date is not None]
    undated = [r for r in all_rels if r.state.start_date is None]

    # Sort dated by start_date descending (normalize timestamps for mixed tz-aware/naive dates)
    dated.sort(key=lambda r: _sort_key_ts(r.state.start_date), reverse=True)

    if not dated and not undated:
        lines.append(_comment("No relationships in KG."))
        lines.append("")
        lines.append(_det_close())
        return "\n".join(lines)

    for r in dated:
        lines.extend(_render_state_connection(r))
        lines.append("")

    if undated:
        lines.append("### Other")
        lines.append("")
        for r in undated:
            lines.extend(_render_state_connection(r))
            lines.append("")

    lines.append(_det_close())
    return "\n".join(lines)


def _render_state_connection(r: StateConnection) -> List[str]:
    edge = r.edge
    date = _fmt_date(r.state.start_date)
    date_str = f" since {date}" if date else ""

    counterpart_names = [c.label for c, _ in r.counterparts]
    if counterpart_names:
        counterpart_str = ", ".join(f"[[{_slugify_for_link(lbl)}]]" for lbl in counterpart_names)
    else:
        counterpart_str = "(no counterpart in KG)"

    rel_type = (edge.relationship_type or "").replace("_", " ").lower()

    # Annotate with state-node id so downstream (wiki_writer) can carry
    # source provenance forward into per-paragraph references. Format:
    # {node:<uuid>} is NOT stripped by strip_debug_scaffolding.
    node_tag = f" {{node:{r.state.id}}}" if getattr(r.state, "id", None) else ""

    # Inbound: this entity is the TARGET of the State. Phrase it as
    # "site of" so the LLM section writer can describe what activity
    # happens here, not pretend the entity originated the state.
    if r.is_inbound:
        out = [f"- site of **{rel_type}** for {counterpart_str}{date_str} — *{r.state.label}*{node_tag}"]
    else:
        out = [f"- **{rel_type}** {counterpart_str}{date_str} — *{r.state.label}*{node_tag}"]

    # Prefer the State node's original sentence (describes the fact holistically).
    # Fall back to the edge sentence (edge-centric, tends to be more stilted).
    fact_sentence = (r.state.original_sentence or "").strip() or (edge.sentence or "").strip()
    if fact_sentence:
        out.append(f"  > {fact_sentence}")

    meta_bits = _read_metadata_bits(r.state, edge)
    if meta_bits:
        out.append(f"  [{meta_bits}]")

    # Flag suspected duplicate entities (counterpart label is a substring/superset of another).
    if len(r.counterparts) > 1:
        dup_pairs = _detect_likely_duplicates(counterpart_names)
        if dup_pairs:
            for a, b in dup_pairs:
                out.append(f"  {_comment(f'KG GAP: state {r.state.label!r} likely duplicate counterparts: {a!r} and {b!r}')}")
    if r.state.start_date is None and r.state.end_date is None:
        out.append(f"  {_comment(f'KG GAP: state {r.state.label!r} has no start_date or end_date')}")

    return out


def _detect_likely_duplicates(names: Iterable[str]) -> List[tuple[str, str]]:
    """Return pairs of labels that look like aliases of the same entity
    (e.g. ``Jukka`` and ``Jukka's wife`` both appearing as counterparts).
    Heuristic: one name is a substring of the other AND both contain the shorter one's words."""
    names = list(names)
    pairs: List[tuple[str, str]] = []
    for i, a in enumerate(names):
        a_low = a.lower().strip()
        for b in names[i + 1 :]:
            b_low = b.lower().strip()
            if a_low == b_low:
                continue
            if a_low in b_low or b_low in a_low:
                pairs.append((a, b))
    return pairs


# ---------- events ----------


def _render_events_section(n: EntityNeighborhood) -> str:
    lines: List[str] = [_det_open("events"), "## Events", ""]

    # Outbound + inbound events. Inbound covers events that happen AT or
    # ARE TARGETED AT this entity (e.g. Pickup, Drop-off, Attending Class
    # at a school).
    all_events = list(n.events) + list(n.inbound_events)
    if not all_events:
        lines.append(_comment("No events in KG."))
        lines.append("")
        lines.append(_det_close())
        return "\n".join(lines)

    # Sort by start_date desc, None last
    sorted_events = sorted(
        all_events,
        key=lambda e: (e.event.start_date is None, -_sort_key_ts(e.event.start_date)),
    )

    for e in sorted_events:
        edge = e.edge
        date = _fmt_date(e.event.start_date)
        date_str = f" ({date})" if date else ""
        coparts = [c.label for c, _ in e.co_participants]
        coparts_str = ""
        if coparts:
            coparts_str = " with " + ", ".join(f"[[{_slugify_for_link(lbl)}]]" for lbl in coparts)
        node_tag = f" {{node:{e.event.id}}}" if getattr(e.event, "id", None) else ""
        # Inbound: phrase as "site of <Event>" so the LLM understands the
        # entity is the location/recipient, not a participant.
        if e.is_inbound:
            rel = (edge.relationship_type or "").replace("_", " ").lower()
            prep = "site of" if rel in ("location", "at institution", "destination", "location from") else f"{rel} of"
            lines.append(f"- {prep} **{e.event.label}**{date_str}{coparts_str}{node_tag}")
        else:
            lines.append(f"- **{e.event.label}**{date_str}{coparts_str}{node_tag}")
        fact = (e.event.original_sentence or "").strip() or (edge.sentence or "").strip()
        if fact:
            lines.append(f"  > {fact}")
        meta_bits = _read_metadata_bits(e.event, edge)
        if meta_bits:
            lines.append(f"  [{meta_bits}]")
        if e.event.start_date is None:
            lines.append(f"  {_comment(f'KG GAP: event {e.event.label!r} has no start_date')}")
        lines.append("")

    lines.append(_det_close())
    return "\n".join(lines)


# ---------- beliefs ----------


def _render_beliefs_section(n: EntityNeighborhood) -> str:
    if not n.beliefs_about:
        return ""

    lines: List[str] = [_det_open("beliefs"), f"## Beliefs about {n.entity.label}", ""]
    for b in n.beliefs_about:
        believer = f"[[{_slugify_for_link(b.believer.label)}]]" if b.believer else "*(unknown believer)*"
        node_tag = f" {{node:{b.state.id}}}" if getattr(b.state, "id", None) else ""
        lines.append(f"- {believer}: **{b.state.label}**{node_tag}")
        fact = (b.state.original_sentence or "").strip() or (b.edge_in.sentence or "").strip()
        if fact:
            lines.append(f"  > {fact}")
        lines.append("")
    lines.append(_det_close())
    return "\n".join(lines)


# ---------- goals ----------


def _render_goals_section(n: EntityNeighborhood) -> str:
    if not n.goals_targeting:
        return ""

    active = [g for g in n.goals_targeting if (g.goal.goal_status or "").lower() == "active"]
    other = [g for g in n.goals_targeting if (g.goal.goal_status or "").lower() != "active"]
    if not active and not other:
        return ""

    lines: List[str] = [_det_open("goals"), f"## Goals involving {n.entity.label}", ""]
    if active:
        lines.append("### Active")
        lines.append("")
        for g in active:
            owner = f"[[{_slugify_for_link(g.owner.label)}]]" if g.owner else "*(unknown owner)*"
            node_tag = f" {{node:{g.goal.id}}}" if getattr(g.goal, "id", None) else ""
            lines.append(f"- {owner}: {g.goal.label}{node_tag}")
            fact = (g.goal.original_sentence or "").strip() or (g.edge_in.sentence or "").strip()
            if fact:
                lines.append(f"  > {fact}")
            lines.append("")
    if other:
        lines.append("### Other")
        lines.append("")
        for g in other:
            owner = f"[[{_slugify_for_link(g.owner.label)}]]" if g.owner else "*(unknown owner)*"
            status = g.goal.goal_status or "unknown"
            node_tag = f" {{node:{g.goal.id}}}" if getattr(g.goal, "id", None) else ""
            lines.append(f"- {owner}: {g.goal.label} *(status: {status})*{node_tag}")
            lines.append("")
    lines.append(_det_close())
    return "\n".join(lines)


# ---------- key facts ----------


def _render_key_facts_section(n: EntityNeighborhood) -> str:
    if not n.entity_card or not n.entity_card.get("key_facts"):
        return ""
    facts = n.entity_card.get("key_facts") or []
    if not facts:
        return ""
    lines: List[str] = [_det_open("key_facts"), "## Key Facts", ""]
    for fact in facts:
        if isinstance(fact, dict):
            # key_facts may be dicts with 'fact' or 'text' keys depending on how the card was built.
            text = fact.get("fact") or fact.get("text") or fact.get("value") or str(fact)
        else:
            text = str(fact)
        text = text.strip().lstrip("-• ")
        if not text:
            continue
        lines.append(f"- {text}")
    lines.append("")
    lines.append(_det_close())
    return "\n".join(lines)


# ---------- see also ----------


def _render_see_also_section(n: EntityNeighborhood) -> str:
    """Collect every Entity this page links to — Obsidian's graph view will resolve them."""
    seen: set[str] = set()
    ordered: List[str] = []

    def _add(label: str) -> None:
        if not label:
            return
        if label in seen:
            return
        seen.add(label)
        ordered.append(label)

    for r in n.relationships:
        for c, _ in r.counterparts:
            _add(c.label)
    for r in n.inbound_relationships:
        for c, _ in r.counterparts:
            _add(c.label)
    for e in n.events:
        for c, _ in e.co_participants:
            _add(c.label)
    for e in n.inbound_events:
        for c, _ in e.co_participants:
            _add(c.label)
    for b in n.beliefs_about:
        if b.believer:
            _add(b.believer.label)
    for g in n.goals_targeting:
        if g.owner:
            _add(g.owner.label)

    if not ordered:
        return ""

    lines: List[str] = [_det_open("see_also"), "## See also", ""]
    for label in ordered:
        lines.append(f"- [[{_slugify_for_link(label)}]]")
    lines.append("")
    lines.append(_det_close())
    return "\n".join(lines)


# ---------- gaps (hidden comment-only section) ----------


def _render_gaps_section(n: EntityNeighborhood) -> str:
    if not n.kg_gaps:
        return ""
    lines: List[str] = [_det_open("kg_gaps"), _comment("KG gaps detected during rendering — these do not display in Obsidian's reading view.")]
    for gap in n.kg_gaps:
        lines.append(_comment(f"KG GAP [{gap.category}]: {gap.message}"))
    lines.append(_det_close())
    return "\n".join(lines)
