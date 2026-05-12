"""Convert a kg_maintenance_finding row into an investigator brief.

Produces the (task, information) pair that gets handed to
``kg_investigation_manager``. Pre-fetches the obvious context so the
investigator can spend its query budget on real digging instead of
re-discovering what tables already say.

Branches per finding_type:
- wiki_contradiction: quoted prose + paragraph + ground truth + entity neighborhood
- duplicate_node: both nodes side-by-side
- orphan_node / duplicate_edge / missing_description / type_error: minimal brief
- unknown / legacy types (e.g., the historical suspect_node): falls through to
  a generic "investigate and recommend a fix" brief.

The brief stays as text, not structured fields, so the investigator's
planner can read it as a normal task description.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text as sql_text

from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
from app.assistant.utils.logging_config import get_logger
from app.models.db_manager import get_db_manager

logger = get_logger(__name__)


# ----- helpers --------------------------------------------------------------


def _fetch_node(session, node_id: str) -> Optional[Dict[str, Any]]:
    if not node_id:
        return None
    row = session.execute(
        sql_text(
            "SELECT id, label, node_type, category, aliases, description, "
            "       original_sentence, start_date, end_date, importance, "
            "       pagerank_score, source, created_at "
            "FROM kg_node_metadata WHERE id = :nid"
        ),
        {"nid": node_id},
    ).fetchone()
    if row is None:
        return None
    return dict(row._mapping) if hasattr(row, "_mapping") else dict(zip(
        ["id", "label", "node_type", "category", "aliases", "description",
         "original_sentence", "start_date", "end_date", "importance",
         "pagerank_score", "source", "created_at"], row,
    ))


def _fetch_neighborhood(session, node_id: str, *, limit: int = 30) -> Dict[str, List[Dict[str, Any]]]:
    """Return (out_edges, in_edges). Each edge dict: relationship_type, sentence, other_id, other_label, other_type.

    Uses LEFT JOIN so edges whose other endpoint is a pod URI (datapod:*)
    — which has no kg_node_metadata row by design — still surface, with
    other_label hydrated from pod_store.
    """
    if not node_id:
        return {"out": [], "in": []}
    out_rows = session.execute(
        sql_text(
            "SELECT e.relationship_type, e.sentence, "
            "       e.target_id AS other_id, t.label AS other_label, t.node_type AS other_type "
            "FROM kg_edge_metadata e "
            "LEFT JOIN kg_node_metadata t ON t.id = e.target_id "
            "WHERE e.source_id = :nid "
            "ORDER BY e.created_at DESC LIMIT :lim"
        ),
        {"nid": node_id, "lim": limit},
    ).fetchall()
    in_rows = session.execute(
        sql_text(
            "SELECT e.relationship_type, e.sentence, "
            "       e.source_id AS other_id, s.label AS other_label, s.node_type AS other_type "
            "FROM kg_edge_metadata e "
            "LEFT JOIN kg_node_metadata s ON s.id = e.source_id "
            "WHERE e.target_id = :nid "
            "ORDER BY e.created_at DESC LIMIT :lim"
        ),
        {"nid": node_id, "lim": limit},
    ).fetchall()

    # Hydrate pod-URI endpoints from pod_store so other_label/other_type are
    # populated for edges that go to pods. Lookup cache scoped to this call.
    from app.assistant.pod_store.pod_uri import POD_URI_RE
    _pod_cache: Dict[str, Any] = {}
    def _hydrate_pod(other_id: str):
        if other_id in _pod_cache:
            return _pod_cache[other_id]
        if not other_id or not POD_URI_RE.fullmatch(other_id):
            _pod_cache[other_id] = (None, None)
            return _pod_cache[other_id]
        from app.assistant.pod_store.pod_store import PodStore
        pod = PodStore().get(other_id)
        if pod is None:
            _pod_cache[other_id] = (None, None)
        else:
            _pod_cache[other_id] = ((pod.one_liner or other_id)[:200], "Pod")
        return _pod_cache[other_id]

    def _to_dict(r):
        other_id = r[2]
        label = r[3]
        ntype = r[4]
        if label is None:
            pod_label, pod_type = _hydrate_pod(other_id)
            label, ntype = pod_label, pod_type
        return {
            "relationship_type": r[0],
            "sentence": r[1],
            "other_id": other_id,
            "other_label": label,
            "other_type": ntype,
        }
    return {"out": [_to_dict(r) for r in out_rows], "in": [_to_dict(r) for r in in_rows]}


def _format_node_block(label: str, node: Dict[str, Any]) -> str:
    if not node:
        return f"### {label}\n(node not found)\n"
    lines = [f"### {label}"]
    lines.append(f"- id: `{node.get('id')}`")
    lines.append(f"- label: {node.get('label')!r}")
    lines.append(f"- node_type: {node.get('node_type')}")
    lines.append(f"- category: {node.get('category')}")
    aliases = node.get("aliases")
    if aliases:
        lines.append(f"- aliases: {aliases}")
    if node.get("start_date") or node.get("end_date"):
        lines.append(
            f"- validity window: start={node.get('start_date')} end={node.get('end_date')}"
        )
    if node.get("description"):
        desc = str(node["description"])
        lines.append(f"- description: {desc[:300]}{'…' if len(desc) > 300 else ''}")
    if node.get("original_sentence"):
        sent = str(node["original_sentence"])
        lines.append(f"- original_sentence (present-tense canonical): {sent[:300]}{'…' if len(sent) > 300 else ''}")
    return "\n".join(lines) + "\n"


def _format_prior_verdicts_block(node_ids: List[str]) -> str:
    """Pull any existing kg_node_verdict rows touching these node ids and
    format them as a bulleted block for the investigator's brief.

    Future-self optimization: when the same question keeps getting flagged
    (recurring duplicate-detection on the same pair, recurring date-gap
    finding on the same node), the investigator should read the prior
    verdict and either confirm + dismiss again, or explicitly supersede
    it. Either way it stops re-deriving the same conclusion from scratch.

    Returns empty string when no verdicts exist (caller should skip the
    section entirely).
    """
    from app.assistant.kg_maintenance.verdict_store import get_verdicts_for_node

    seen: set[str] = set()
    rows = []
    for nid in node_ids:
        if not nid:
            continue
        for v in get_verdicts_for_node(nid, limit=10):
            if v.id in seen:
                continue
            seen.add(v.id)
            rows.append(v)
    if not rows:
        return ""

    lines = ["## Prior verdicts on these nodes",
             "_Investigators previously decided about these node ids — "
             "consult before re-deriving. Confirm + dismiss if still "
             "valid; supersede if your investigation changes the "
             "verdict._", ""]
    for v in rows[:8]:
        when = v.created_at.isoformat()[:10] if v.created_at else "?"
        lines.append(
            f"- **{v.verdict_type}** ({when}, conf={v.confidence}): "
            f"{v.memo}"
        )
        if v.source_finding_id:
            lines.append(f"  source_finding: `{v.source_finding_id}`")
    lines.append("")
    return "\n".join(lines)


def _format_neighborhood_block(neigh: Dict[str, List[Dict[str, Any]]], *, max_lines: int = 20) -> str:
    out_edges = neigh.get("out", [])[:max_lines]
    in_edges = neigh.get("in", [])[:max_lines]
    lines = []
    if out_edges:
        lines.append("Outgoing edges:")
        for e in out_edges:
            sent = (e.get("sentence") or "")[:140]
            lines.append(
                f"  -[{e.get('relationship_type')}]-> `{e.get('other_id')}` "
                f"({e.get('other_type')}) {e.get('other_label')!r}  | {sent}"
            )
    if in_edges:
        lines.append("Incoming edges:")
        for e in in_edges:
            sent = (e.get("sentence") or "")[:140]
            lines.append(
                f"  `{e.get('other_id')}` ({e.get('other_type')}) {e.get('other_label')!r} "
                f"-[{e.get('relationship_type')}]->  | {sent}"
            )
    if not lines:
        lines.append("(no edges)")
    return "\n".join(lines) + "\n"


# ----- per-finding-type brief builders --------------------------------------


def _brief_wiki_contradiction(session, finding: KGMaintenanceFinding) -> Tuple[str, str]:
    ev = finding.evidence_json or {}
    entity_label = ev.get("entity_label") or "?"
    issue_type = ev.get("issue_type") or "?"
    quoted = (ev.get("quoted_text") or "").strip()
    paragraph = (ev.get("wiki_paragraph") or "").strip()
    section = (ev.get("wiki_section_heading") or "").strip()
    line_no = ev.get("wiki_line_number")
    prose_path = (ev.get("prose_path") or "").strip()
    source_kind = (ev.get("source_kind") or "").strip()
    source_statement = (ev.get("source_statement") or "").strip()

    primary = _fetch_node(session, finding.primary_node_id)
    neigh = _fetch_neighborhood(session, finding.primary_node_id)

    parts: List[str] = []
    parts.append(f"## Finding")
    parts.append(f"- finding_id: `{finding.id}`")
    parts.append(f"- finding_type: wiki_contradiction")
    parts.append(f"- issue_type (per critic): {issue_type}")
    parts.append(f"- priority: {finding.priority}")
    parts.append(f"- critic confidence: {finding.confidence}")
    parts.append(f"- summary: {finding.reason}")
    parts.append("")

    parts.append(f"## Wiki location")
    parts.append(f"- subject: {entity_label}")
    parts.append(f"- prose path: {prose_path}")
    if section:
        parts.append(f"- section: `{section}`")
    if line_no:
        parts.append(f"- line: {line_no}")
    parts.append("")

    parts.append(f"## Quoted contradicting text")
    parts.append(f"```\n{quoted}\n```")
    if paragraph and paragraph != quoted:
        parts.append("")
        parts.append("Paragraph context:")
        parts.append(f"```\n{paragraph}\n```")
    parts.append("")

    if source_kind and source_kind != "internal" and source_statement:
        parts.append(f"## Authoritative source the prose disputes")
        parts.append(f"- source_kind: {source_kind}")
        parts.append(f"- source_statement: {source_statement!r}")
        parts.append("")
    elif source_kind == "internal":
        parts.append(f"## Source")
        parts.append(f"Internal contradiction (the prose contradicts itself).")
        parts.append("")

    parts.append(f"## Subject node (primary)")
    parts.append(_format_node_block(entity_label, primary))
    parts.append("Neighborhood:")
    parts.append(_format_neighborhood_block(neigh))

    task = (
        f"Investigate wiki contradiction on the page for {entity_label!r}. "
        f"Decide whether the root cause is: a duplicate node, a typo / spelling variant, "
        f"a tense or canonical-sentence error on a State/Event/Goal node, "
        f"a wrong start_date/end_date validity window, or a one-off bad fact. "
        f"Recommend a concrete fix via proposed_action."
    )
    return task, "\n".join(parts)


def _brief_node_pair(
    session,
    finding: KGMaintenanceFinding,
    *,
    finding_label: str,
    task_phrase: str,
) -> Tuple[str, str]:
    primary = _fetch_node(session, finding.primary_node_id)
    secondary = _fetch_node(session, finding.secondary_node_id) if finding.secondary_node_id else None
    primary_neigh = _fetch_neighborhood(session, finding.primary_node_id)
    secondary_neigh = (
        _fetch_neighborhood(session, finding.secondary_node_id)
        if finding.secondary_node_id
        else {"out": [], "in": []}
    )

    parts: List[str] = [
        f"## Finding",
        f"- finding_id: `{finding.id}`",
        f"- finding_type: {finding_label}",
        f"- priority: {finding.priority}",
        f"- agent confidence: {finding.confidence}",
        f"- agent reason: {finding.reason}",
        "",
        f"## Primary node",
        _format_node_block("primary", primary),
        "Neighborhood:",
        _format_neighborhood_block(primary_neigh),
    ]
    if secondary is not None:
        parts.extend([
            "",
            f"## Secondary node",
            _format_node_block("secondary", secondary),
            "Neighborhood:",
            _format_neighborhood_block(secondary_neigh),
        ])
    return task_phrase, "\n".join(parts)


def _fetch_dated_neighbors(session, node_id: str, *, limit: int = 20) -> List[Dict[str, Any]]:
    """Pull connected nodes that DO have dates — these anchor temporal
    inference for the missing-dates investigator. Returns a list of dicts
    with the connected node's label, type, dates, and the relationship."""
    if not node_id:
        return []
    rows = session.execute(
        sql_text(
            """
            SELECT
              n.id AS other_id,
              n.label AS other_label,
              n.node_type AS other_type,
              n.start_date AS other_start,
              n.end_date AS other_end,
              n.start_date_prose AS other_start_prose,
              n.end_date_prose AS other_end_prose,
              e.relationship_type AS rel,
              e.sentence AS sentence,
              CASE WHEN e.source_id = :nid THEN 'out' ELSE 'in' END AS direction
            FROM kg_edge_metadata e
            JOIN kg_node_metadata n ON n.id = (CASE WHEN e.source_id = :nid THEN e.target_id ELSE e.source_id END)
            WHERE (e.source_id = :nid OR e.target_id = :nid)
              AND (n.start_date IS NOT NULL OR n.end_date IS NOT NULL
                   OR n.start_date_prose IS NOT NULL OR n.end_date_prose IS NOT NULL)
            ORDER BY COALESCE(n.start_date, n.end_date) DESC
            LIMIT :lim
            """
        ),
        {"nid": node_id, "lim": limit},
    ).fetchall()
    out = []
    for r in rows:
        m = r._mapping if hasattr(r, "_mapping") else dict(zip(
            ["other_id", "other_label", "other_type", "other_start", "other_end",
             "other_start_prose", "other_end_prose", "rel", "sentence", "direction"], r,
        ))
        out.append(dict(m))
    return out


def _fetch_evidence_messages(session, node_id: str, *, limit: int = 8) -> List[Dict[str, Any]]:
    """Pull the original chat messages cited by this node's evidence rows.
    Conversation timestamps are the strongest dating anchor for States/Events
    that have no explicit dates: 'when did this become true?' often reduces to
    'when was this first / most recently said?'.

    Wrapped in try/except because some test DBs don't include the chat-side
    tables (kg_node_evidence / kg_window_message). The brief degrades
    gracefully — date investigation still works using dated neighbors alone.
    """
    if not node_id:
        return []
    try:
        rows = session.execute(
            sql_text(
                """
                SELECT
                  MIN(u.timestamp) AS first_seen,
                  MAX(u.timestamp) AS last_seen,
                  COUNT(DISTINCT u.id) AS n_messages
                FROM kg_node_evidence ev
                LEFT JOIN unified_log_2026 u
                  ON u.id IN (
                    SELECT m.unified_log_id FROM kg_window_message m
                    WHERE m.window_id = ev.window_id
                  )
                WHERE ev.node_id = :nid
                """
            ),
            {"nid": node_id},
        ).fetchall()
    except Exception as e:
        logger.debug("[date_brief] evidence-window lookup failed (likely missing tables): %s", e)
        return []
    if not rows:
        return []
    r = rows[0]
    m = r._mapping if hasattr(r, "_mapping") else dict(zip(
        ["first_seen", "last_seen", "n_messages"], r,
    ))
    return [dict(m)]


def _fetch_node_chat_windows(session, node_id: str, *, limit: int = 4) -> List[Dict[str, Any]]:
    """For each node, pull the top-N chat windows that produced it (via
    kg_node_evidence) and the source messages inside those windows.
    This is the rawest grounded context the investigator can have:
    not summaries, not derived sentences — the actual conversation
    that minted this node.

    Critical for catching recurring-event traps. If two "Friday Night
    Meats" nodes look identical by label but their windows are five
    weeks apart with different attendees and different food, they're
    distinct occurrences of a recurring event, not duplicates.

    Returns a list of {window_id, observed_at, window_summary, transcript}
    dicts, newest first. Empty list when the node has no evidence rows
    or the chat tables aren't available (test envs).
    """
    if not node_id:
        return []
    out: List[Dict[str, Any]] = []

    # Step 1: distinct window_ids for this node, newest first.
    try:
        rows = session.execute(
            sql_text(
                """
                SELECT DISTINCT ev.window_id, MIN(ev.message_timestamp) AS first_seen,
                       MAX(ev.message_timestamp) AS last_seen
                FROM kg_node_evidence ev
                WHERE ev.node_id = :nid AND ev.window_id IS NOT NULL
                GROUP BY ev.window_id
                ORDER BY last_seen DESC
                LIMIT :lim
                """
            ),
            {"nid": node_id, "lim": limit},
        ).fetchall()
    except Exception as e:
        logger.debug(
            "[finding_brief] node_evidence query failed (likely missing tables): %s", e,
        )
        return []

    for r in rows:
        m = r._mapping if hasattr(r, "_mapping") else dict(zip(
            ["window_id", "first_seen", "last_seen"], r,
        ))
        wid = m.get("window_id")
        if not wid:
            continue

        # Step 2: window summary, if the new pipeline made one.
        summary = None
        try:
            srow = session.execute(
                sql_text("SELECT summary FROM kg_window WHERE id = :wid"),
                {"wid": wid},
            ).fetchone()
            if srow:
                summary = srow[0]
        except Exception:
            pass

        # Step 3: original messages — try the new pipeline's window_message
        # table, fall back to legacy kg_chat_conversation_window_item.
        transcript: List[str] = []
        try:
            mrows = session.execute(
                sql_text(
                    """
                    SELECT u.timestamp, u.role, COALESCE(u.speaker_name, u.role) AS speaker,
                           u.message
                    FROM kg_window_message wm
                    JOIN unified_log_2026 u ON u.id = wm.unified_log_id
                    WHERE wm.window_id = :wid
                    ORDER BY wm.item_order ASC
                    LIMIT 25
                    """
                ),
                {"wid": wid},
            ).fetchall()
            for ts, role, speaker, message in mrows:
                msg = (str(message or "")).replace("\n", " ")[:240]
                transcript.append(f"[{ts}] {speaker or role}: {msg}")
        except Exception:
            # Try legacy table.
            try:
                lrows = session.execute(
                    sql_text(
                        """
                        SELECT unified_timestamp, role, speaker_name, text
                        FROM kg_chat_conversation_window_item
                        WHERE window_id = :wid
                        ORDER BY item_order ASC
                        LIMIT 25
                        """
                    ),
                    {"wid": wid},
                ).fetchall()
                for ts, role, speaker, text in lrows:
                    msg = (str(text or "")).replace("\n", " ")[:240]
                    transcript.append(f"[{ts}] {speaker or role}: {msg}")
            except Exception as e:
                logger.debug("[finding_brief] window transcript fetch failed: %s", e)

        out.append({
            "window_id": str(wid),
            "first_seen": m.get("first_seen"),
            "last_seen": m.get("last_seen"),
            "summary": summary,
            "transcript": transcript,
        })

    return out


def _format_chat_windows_block(windows: List[Dict[str, Any]], node_label: str) -> str:
    """Pretty-print the chat-window prefetch for the investigator's brief."""
    if not windows:
        return f"(no chat-window evidence found for {node_label!r})"
    parts = [f"Chat windows that produced {node_label!r} (newest first, {len(windows)} of them):"]
    for w in windows:
        parts.append(f"\n--- window {w['window_id'][:8]} ({w.get('first_seen')} → {w.get('last_seen')}) ---")
        if w.get("summary"):
            parts.append(f"Summary: {w['summary'][:300]}")
        if w.get("transcript"):
            parts.append("Transcript:")
            for line in w["transcript"][:15]:
                parts.append(f"  {line}")
        else:
            parts.append("(no transcript available)")
    return "\n".join(parts)


def _brief_duplicate_node(
    session,
    finding: KGMaintenanceFinding,
) -> Tuple[str, str]:
    """Specialized brief for duplicate_node findings.

    Pre-fetches the chat windows that produced each candidate node
    (kg_node_evidence → kg_window / kg_window_message → unified_log_2026)
    so the investigator decides on rich grounded context, not just
    label / neighborhood overlap. Critical for catching recurring-event
    traps: two 'Friday Night Meats' nodes from chats five weeks apart
    are distinct occurrences of a weekly event series, NOT duplicates,
    no matter how similar their labels look.

    Even when the labels are identical and the neighborhoods overlap
    heavily, the LLM should still read both transcripts and certify
    that this is a single real-world thing, not a repeating event /
    state. We prefer false negatives (skipped merges that were valid)
    over false positives (collapsing recurring events into one node).
    """
    primary = _fetch_node(session, finding.primary_node_id)
    secondary = _fetch_node(session, finding.secondary_node_id) if finding.secondary_node_id else None
    primary_neigh = _fetch_neighborhood(session, finding.primary_node_id)
    secondary_neigh = (
        _fetch_neighborhood(session, finding.secondary_node_id)
        if finding.secondary_node_id else {"out": [], "in": []}
    )
    primary_windows = _fetch_node_chat_windows(session, finding.primary_node_id)
    secondary_windows = (
        _fetch_node_chat_windows(session, finding.secondary_node_id)
        if finding.secondary_node_id else []
    )

    primary_label = (primary or {}).get("label") or "primary"
    secondary_label = (secondary or {}).get("label") or "secondary"

    parts: List[str] = [
        "## Finding",
        f"- finding_id: `{finding.id}`",
        f"- finding_type: duplicate_node",
        f"- priority: {finding.priority}",
        f"- agent confidence: {finding.confidence}",
        f"- agent reason: {finding.reason}",
        "",
        "## Primary node",
        _format_node_block("primary", primary),
        "Neighborhood:",
        _format_neighborhood_block(primary_neigh),
        "",
        "## Secondary node",
        _format_node_block("secondary", secondary),
        "Neighborhood:",
        _format_neighborhood_block(secondary_neigh),
        "",
        "## Chat-window evidence — PRIMARY",
        _format_chat_windows_block(primary_windows, primary_label),
        "",
        "## Chat-window evidence — SECONDARY",
        _format_chat_windows_block(secondary_windows, secondary_label),
    ]

    task = (
        "Decide whether these two nodes refer to the SAME real-world thing or DISTINCT occurrences "
        "of a recurring/repeated thing.\n\n"
        "READ BOTH CHAT-WINDOW TRANSCRIPTS before deciding. The most expensive mistake here is "
        "merging recurring events (Friday-night dinners over multiple weeks, weekly meetings, "
        "monthly grocery trips, repeated visits to the same coffee shop) into a single node — "
        "that destroys distinct evidence. The label being identical is NOT evidence of sameness; "
        "two windows five weeks apart with different attendees and different specifics are "
        "distinct occurrences, not duplicates.\n\n"
        "Decision rules:\n"
        " - SAME real-world thing: propose op='merge_nodes' with the surviving canonical id and "
        "   args naming the loser. State explicitly in your diagnosis: 'I read both windows and "
        "   they describe the same occurrence (cite specifics).'\n"
        " - DISTINCT occurrences of a recurring pattern: propose op='no_action' and recommend "
        "   the user explicitly mark them part of a series rather than merging.\n"
        " - AMBIGUOUS / not enough evidence: propose op='escalate_user' with a sharp question "
        "   the user can answer in one line. Better to ask than guess.\n\n"
        "Run `kg_query` against `kg_node_evidence` and `unified_log_2026` if you need MORE "
        "context than the pre-fetched windows. Run `pod_search` if you remember a related "
        "conversation that might be in another room. Use the full read-only investigation toolkit. "
        "When in doubt, escalate. We strongly prefer skipping a valid merge over collapsing "
        "recurring events into one."
    )
    return task, "\n".join(parts)


def _brief_state_missing_dates(
    session,
    finding: KGMaintenanceFinding,
) -> Tuple[str, str]:
    """Specialized brief for state_missing_dates findings.

    The investigator's job here is narrow and concrete: PROPOSE start_date
    and/or end_date for the subject node, with confidence + evidence. We
    pre-fetch:
      - the node itself (with current empty/partial dates)
      - dated neighbors (events the user attended, related states with
        bounds, child entities born / created in known years)
      - evidence-message timestamp range (first observation / last
        observation in chat — useful "observed-during" anchor when no
        external dating exists)
      - sibling states for the same primary subject (e.g. for a Residence
        with no start_date, prior Residence states give a "before X"
        anchor; subsequent ones give an "after X" anchor)

    The proposed_action contract the investigator should follow:
      op: 'fill_dates'
      args: { 'start_date': 'YYYY-MM-DD' or '', 'end_date': '...',
              'start_date_prose': '...', 'end_date_prose': '...' }
      confidence: 0.0-1.0 (auto-applied at >= 0.8 by the executor;
                            below that, escalated to the user as a question)
    """
    primary = _fetch_node(session, finding.primary_node_id)
    neigh = _fetch_neighborhood(session, finding.primary_node_id)
    dated_neighbors = _fetch_dated_neighbors(session, finding.primary_node_id)
    evidence_window = _fetch_evidence_messages(session, finding.primary_node_id)
    ev = finding.evidence_json or {}

    parts: List[str] = [
        f"## Finding",
        f"- finding_id: `{finding.id}`",
        f"- finding_type: state_missing_dates",
        f"- priority: {finding.priority}",
        f"- agent reason: {finding.reason}",
        "",
    ]
    if ev:
        parts.append("## Detector context")
        for k, v in ev.items():
            if v in (None, "", []):
                continue
            sval = str(v)
            parts.append(f"- {k}: {sval[:300]}{'…' if len(sval) > 300 else ''}")
        parts.append("")
    parts.append("## Subject node (the State/Event missing dates)")
    parts.append(_format_node_block(primary.get("label") if primary else finding.primary_node_id[:8], primary))
    parts.append("")

    if dated_neighbors:
        parts.append("## Dated neighbors (temporal anchors)")
        parts.append("These connected nodes already have dates; use them to bracket the subject's validity window.")
        for n in dated_neighbors[:15]:
            anchor = []
            if n.get("other_start"):
                anchor.append(f"start={n['other_start']}")
            if n.get("other_end"):
                anchor.append(f"end={n['other_end']}")
            if n.get("other_start_prose"):
                anchor.append(f"start_prose={n['other_start_prose']!r}")
            if n.get("other_end_prose"):
                anchor.append(f"end_prose={n['other_end_prose']!r}")
            parts.append(
                f"- ({n['direction']}) [{n['rel']}] {n['other_label']!r} ({n['other_type']}): {' / '.join(anchor)}"
            )
        parts.append("")
    else:
        parts.append("## Dated neighbors")
        parts.append("(none — no connected node has dates)")
        parts.append("")

    if evidence_window and evidence_window[0].get("first_seen"):
        ew = evidence_window[0]
        parts.append("## Conversation-evidence window")
        parts.append(
            f"This fact has been observed in chat between {ew['first_seen']} and "
            f"{ew['last_seen']} ({ew.get('n_messages', '?')} messages cited)."
        )
        parts.append(
            "Use this as a soft 'observed-during' anchor when no external dating exists. "
            "Note: this is when we LEARNED about the fact, not when the fact became true. "
            "For real-world events (births, weddings, jobs starting) prefer external "
            "dating from neighbors."
        )
        parts.append("")

    parts.append("## Subject's full neighborhood")
    parts.append(_format_neighborhood_block(neigh))

    task = (
        "Find start_date and/or end_date for this State/Event by reasoning over the "
        "dated neighbors, sibling states, and conversation evidence. Set "
        "proposed_action.op='fill_dates' with args = {start_date, end_date, "
        "start_date_prose, end_date_prose} (use ISO YYYY-MM-DD when you have a "
        "specific date; use prose like 'around 2010' or 'late 2020s' when you "
        "have only a fuzzy bound). Set confidence in [0,1]: >= 0.8 will be "
        "auto-applied by the executor; below that the executor escalates to "
        "the user as a clarifying question. Follow date references in the "
        "prose with kg_query (e.g. 'when Katy was 13' → look up Katy's birth); "
        "deriving a range from KG facts is reasoning, not invention. If the "
        "data genuinely doesn't "
        "answer, propose op='escalate_user' with a specific question for the "
        "user (e.g. 'When did Annika start middle school?'). Never invent dates."
    )
    return task, "\n".join(parts)


def _brief_single_node(
    session,
    finding: KGMaintenanceFinding,
    *,
    finding_label: str,
    task_phrase: str,
) -> Tuple[str, str]:
    primary = _fetch_node(session, finding.primary_node_id)
    neigh = _fetch_neighborhood(session, finding.primary_node_id)
    ev = finding.evidence_json or {}

    parts: List[str] = [
        f"## Finding",
        f"- finding_id: `{finding.id}`",
        f"- finding_type: {finding_label}",
        f"- priority: {finding.priority}",
        f"- agent confidence: {finding.confidence}",
        f"- agent reason: {finding.reason}",
        "",
    ]
    if ev:
        parts.append("## Evidence from the finder")
        for k, v in ev.items():
            if v in (None, "", []):
                continue
            sval = str(v)
            parts.append(f"- {k}: {sval[:300]}{'…' if len(sval) > 300 else ''}")
        parts.append("")
    parts.append("## Subject node")
    parts.append(_format_node_block(ev.get("entity_label") or finding.primary_node_id[:8], primary))
    parts.append("Neighborhood:")
    parts.append(_format_neighborhood_block(neigh))
    return task_phrase, "\n".join(parts)


# ----- public entry point ---------------------------------------------------


def build_finding_brief(finding_id: str) -> Optional[Tuple[str, str]]:
    """Return ``(task, information)`` for the investigator, or ``None`` if the
    finding doesn't exist."""
    with get_db_manager().read_session() as session:
        finding = (
            session.query(KGMaintenanceFinding)
            .filter(KGMaintenanceFinding.id == finding_id)
            .first()
        )
        if finding is None:
            logger.warning("build_finding_brief: finding %s not found", finding_id)
            return None

        ftype = (finding.finding_type or "").strip().lower()
        # Pull prior verdicts touching this finding's subject nodes —
        # appended to whichever per-type brief we build below so the
        # investigator can confirm/supersede prior decisions instead of
        # re-deriving them.
        subject_ids = [
            nid for nid in (finding.primary_node_id, finding.secondary_node_id) if nid
        ]
        prior_verdicts_block = _format_prior_verdicts_block(subject_ids)

        if ftype == "wiki_contradiction":
            task, info = _brief_wiki_contradiction(session, finding)
            if prior_verdicts_block:
                info = f"{prior_verdicts_block}\n\n{info}"
            return task, info
        if ftype == "state_missing_dates":
            task, info = _brief_state_missing_dates(session, finding)
            if prior_verdicts_block:
                info = f"{prior_verdicts_block}\n\n{info}"
            return task, info
        if ftype == "duplicate_node":
            # Specialized brief: pre-fetches chat-window transcripts for both
            # candidates so the investigator catches recurring-event traps
            # ("Friday Night Meats" five weeks apart is NOT a duplicate). The
            # cost is a few more SQL reads; the upside is no deterministic
            # merges of repeating things — the user prefers false negatives
            # (skipped valid merges) over false positives (collapsed recurrences).
            task, info = _brief_duplicate_node(session, finding)
            if prior_verdicts_block:
                info = f"{prior_verdicts_block}\n\n{info}"
            return task, info
        if ftype == "duplicate_edge":
            task, info = _brief_node_pair(
                session, finding,
                finding_label="duplicate_edge",
                task_phrase=(
                    "Investigate whether multiple edges between these nodes are duplicates of one "
                    "underlying claim, or genuinely distinct events / observations. "
                    "Recommend delete_edge (specifying which) or no_action."
                ),
            )
            if prior_verdicts_block:
                info = f"{prior_verdicts_block}\n\n{info}"
            return task, info
        if ftype == "orphan_node":
            task, info = _brief_single_node(
                session, finding,
                finding_label="orphan_node",
                task_phrase=(
                    "Investigate this orphan (zero edges) node. Decide whether it is a stranded "
                    "remnant safe to delete, or whether it should be linked to existing nodes "
                    "(recommend escalate_user with a clear suggestion in args)."
                ),
            )
            if prior_verdicts_block:
                info = f"{prior_verdicts_block}\n\n{info}"
            return task, info
        if ftype == "missing_description":
            task, info = _brief_single_node(
                session, finding,
                finding_label="missing_description",
                task_phrase=(
                    "Investigate why this node has no description and propose update_node_field "
                    "with a one-paragraph description grounded in the node's edges + evidence. "
                    "If the node is itself dubious, recommend escalate_user instead."
                ),
            )
            if prior_verdicts_block:
                info = f"{prior_verdicts_block}\n\n{info}"
            return task, info
        if ftype == "type_error":
            task, info = _brief_single_node(
                session, finding,
                finding_label="type_error",
                task_phrase=(
                    "Investigate this type-error finding. Compare node_type vs the actual content "
                    "(label, original_sentence, neighborhood) and recommend update_node_field "
                    "with the correct node_type, or no_action if the finder was wrong."
                ),
            )
            if prior_verdicts_block:
                info = f"{prior_verdicts_block}\n\n{info}"
            return task, info

        # Unknown type: minimal brief.
        task, info = _brief_single_node(
            session, finding,
            finding_label=ftype or "unknown",
            task_phrase=(
                "Investigate this finding. Determine what (if anything) is wrong and recommend a "
                "concrete fix via proposed_action."
            ),
        )
        if prior_verdicts_block:
            info = f"{prior_verdicts_block}\n\n{info}"
        return task, info
