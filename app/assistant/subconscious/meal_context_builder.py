"""Build the injected context dict for the meal_proposer agent.

Reuses some helpers from the noticer's context_builder (diet log, calendar,
family roster, KG household digests) and adds meal-specific assembly:
- addressable_concerns: concerns_register filtered to those routed to
  meal_proposer
- inventory_snapshot: grocery inventory (stub for 1a — empty list)
- recipes_house: from resource_meal_proposer_house_rules (just a stub
  reference; the agent reads the actual recipes from house_rules in system
  context)
- dietary_context: KG-derived medical + dietary patterns for Jukka
- fast_food_count_7d: scan diet_log for delivery/restaurant entries
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.assistant.subconscious.context_builder import (
    _build_calendar_today_tomorrow,
    _build_calendar_week_summary,
    _build_diet_log,
    _build_family_roster,
    _build_kg_household_digests,
    _resolve_household_members,
    build_weekly_schedule_block,
    _NO_DATA_FMT,
)
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root
from app.assistant.utils.time_utils import get_local_time

logger = get_logger(__name__)


def build_weekly_meal_planner_context() -> Dict[str, str]:
    """Context for weekly_meal_planner (strategic, full-week).

    Note on the calendar slot: `general_calendar_week` covers the next
    7 days from non-Food calendars (travel, visitors, work, family
    events). The OLD `food_calendar` (already-planned Food calendar
    entries) was dropped — we're planning 7 days INTO THE FUTURE, so
    there's nothing useful in already-planned meals to constrain
    against. Audience signals (e.g. "Marika visits Tuesday") come
    from general_calendar_week.
    """
    now_local = get_local_time()
    household_members = _resolve_household_members()

    return {
        "date_time": now_local.strftime("%Y-%m-%d %H:%M %Z"),
        "day_of_week": now_local.strftime("%A"),
        "week_start_date": _compute_week_start_date(now_local),
        "diet_log_7d": _build_diet_log(),
        "inventory_snapshot": _build_inventory_snapshot(),
        "recipes_house": _build_recipes_house_reference(),
        "dietary_context": _build_dietary_context(household_members),
        "food_beliefs": _build_food_beliefs(agent="weekly_meal_planner"),
        "easy_meals_rotation": _build_easy_meals_rotation(),
        "general_calendar_week": _build_calendar_week_summary(now_local=now_local),
        "family_roster": _build_family_roster(household_members),
        "addressable_concerns": _build_addressable_concerns(),
        "fast_food_count_7d": _build_fast_food_count(),
        "recent_planned_meals": _build_recent_planned_meals(),
        "ralphs_standing_list": _build_ralphs_standing_list(),
        "agent_weekly_list_state": _build_agent_weekly_list_state(),
    }


def build_daily_meal_proposer_context() -> Dict[str, str]:
    """Context for daily_meal_proposer (tactical, 24-48h)."""
    now_local = get_local_time()
    household_members = _resolve_household_members()

    return {
        "date_time": now_local.strftime("%Y-%m-%d %H:%M %Z"),
        "day_of_week": now_local.strftime("%A"),
        "diet_log_7d": _build_diet_log(),
        "inventory_snapshot": _build_inventory_snapshot(),
        "recipes_house": _build_recipes_house_reference(),
        "dietary_context": _build_dietary_context(household_members),
        "food_beliefs": _build_food_beliefs(agent="daily_meal_proposer"),
        "easy_meals_rotation": _build_easy_meals_rotation(),
        "food_calendar": _build_food_calendar(now_local=now_local),
        "general_calendar": _build_calendar_today_tomorrow(now_local=now_local),
        "family_roster": _build_family_roster(household_members),
        "addressable_concerns": _build_addressable_concerns(),
        "fast_food_count_7d": _build_fast_food_count(),
        "latest_weekly_plan": _build_latest_weekly_plan(),
        "ralphs_standing_list": _build_ralphs_standing_list(),
        "weekly_schedule": build_weekly_schedule_block(),
    }


# ── section builders ─────────────────────────────────────────────────────


def _build_inventory_snapshot() -> str:
    """Render current grocery inventory via grocery_inventory.render_inventory_summary.

    State is populated by run_grocery_sync (Phase 1b): the scanner agent
    detects "I did groceries" / "I had the salmon" / "we ran out of X" in
    recent chat and applies the corresponding intention.shopping or
    intention.meal pod's items to inventory, plus daily decay.

    Returns a markdown summary grouped by category with USE-SOON callouts.
    """
    from app.assistant.subconscious.grocery_inventory import render_inventory_summary
    try:
        return render_inventory_summary()
    except Exception as e:
        logger.warning("[meal_context] inventory render failed: %s", e)
        return "(error reading inventory)"


def _build_recipes_house_reference() -> str:
    """The actual recipe list lives in the system_context_items
    (resource_meal_proposer_house_rules). We just point to it here."""
    return "(see resource_meal_proposer_house_rules in your system context for the full recipe vocabulary this family cooks regularly)"


def _build_easy_meals_rotation() -> str:
    """Render the easy-meals cadence/rotation block — go-to dishes flagged DUE to
    come back vs RESTING (served too recently). Source: the easy_meals registry +
    planned history (see subconscious/easy_meals.py)."""
    from app.assistant.subconscious.easy_meals import render_easy_meals_for_planner
    try:
        return render_easy_meals_for_planner()
    except Exception as e:
        logger.warning("[meal_context] easy_meals rotation render failed: %s", e)
        return "(error reading easy-meals rotation)"


# Substring keywords for State node LABELS that strongly suggest dietary
# relevance. Every entry is long enough that substring-matching is safe
# (no false-positives like "eat" matching "great").
_FOOD_LABEL_KEYWORDS = (
    "food", "eating", "diet", "allerg", "cooking", "meal", "snack",
    "drink", "beverage", "bakery", "cuisine", "recipe", "intolerance",
    "vegetarian", "vegan", "fasting", "kitchen", "appetite", "breakfast",
    "lunch", "dinner", "treat",
    # specific items / categories common as State labels
    "chicken", "beef", "salmon", "fruit", "vegetable", "potato",
    "ingredient",
)

# Whole-word keywords for edge SENTENCES. Sentence matching uses word
# boundaries (not substring) — bare "eat" / "ate" / "salt" produced false
# positives against "particiPATEs" / "Salt and Sanctuary". This list is
# longer-form / distinctive food terms.
_FOOD_SENTENCE_WORDS = frozenset({
    # distinctive food/diet vocabulary
    "food", "meal", "meals", "cook", "cooked", "cooking", "cooks",
    "diet", "diets", "drink", "drinks", "drank", "ate", "eats",
    "eaten", "eating", "breakfast", "lunch", "dinner", "snack",
    "snacks", "recipe", "recipes", "ingredient", "ingredients",
    "carb", "carbs", "protein", "intermittent", "lactose", "gluten",
    "gerd", "diabetic", "diabetes", "allergy", "allergic", "allergies",
    "intolerance", "vegan", "vegetarian", "pescatarian",
    # food items (distinctive enough that whole-word matching catches them)
    "vegetable", "vegetables", "fruit", "fruits", "tortilla",
    "tortillas", "pasta", "salmon", "chicken", "potato", "potatoes",
    "cheese", "bread", "cereal", "yogurt", "rice", "beef", "pork",
    "lamb", "soup", "salad", "burrito", "burritos", "frittata",
    "frittatas", "broccoli", "asparagus", "tomato", "tomatoes",
    "coffee", "tea", "beer", "wine", "egg", "eggs", "bacon",
    "milk", "butter", "sauce", "spice", "spices", "sugar", "salt",
    "pancake", "pancakes", "oats", "oatmeal", "burger", "pizza",
    "sandwich", "noodle", "noodles", "leftover", "leftovers",
    "mustard", "ketchup", "mayo", "dressing", "condiment",
    "dessert", "candy", "chocolate", "cookie", "cookies", "cake",
    "sushi", "taco", "tacos", "burrito", "wrap", "bagel", "donut",
    "cup", "noodle", "stir", "fry", "fried", "grilled",
    "fish", "shrimp", "tofu", "garlic", "onion", "pepper",
    "delivery", "restaurant", "takeout",
})


def _sentence_has_food_word(sentence_lower: str) -> bool:
    """Whole-word match: tokenize the sentence, intersect with the food
    vocabulary. Avoids the 'salt' matching 'Salt and Sanctuary' problem
    that substring matching had — Salt and Sanctuary tokenizes to
    {'salt', 'and', 'sanctuary'} and 'salt' IS in the food set, so this
    particular case is still ambiguous. The downstream proposer can
    handle borderline matches; the goal here is to cut the worst noise
    (verb-stem matches like 'eat' in 'great', 'ate' in 'participates')
    not eliminate every borderline case.
    """
    import re
    tokens = re.findall(r"[a-z]+", sentence_lower)
    return any(tok in _FOOD_SENTENCE_WORDS for tok in tokens)


def _per_member_dietary_signals(session, member: str):
    """Return list of (state_label, edge_sentence) tuples for food-relevant
    has_state edges on `member`. Returns None when the member has no KG
    Entity node; empty list when they exist but carry no food signals.
    """
    from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
    from sqlalchemy import func

    n = (
        session.query(Node)
        .filter(func.lower(Node.label) == member.lower())
        .filter(Node.node_type == "Entity")
        .first()
    )
    if n is None:
        return None
    rows = (
        session.query(Edge, Node)
        .join(Node, Node.id == Edge.target_id)
        .filter(Edge.source_id == n.id)
        .filter(Edge.relationship_type == "has_state")
        .all()
    )
    out = []
    for e, tgt in rows:
        label = (tgt.label or "")
        sent = (e.sentence or "")
        lbl_l = label.lower()
        snt_l = sent.lower()
        if (
            any(k in lbl_l for k in _FOOD_LABEL_KEYWORDS)
            or _sentence_has_food_word(snt_l)
        ):
            out.append((label, sent.strip()))
    return out


def _build_dietary_context(household_members: List[str]) -> str:
    """Per-person dietary signals pulled from KG has_state edges.

    For every household member, walks the KG: finds their Entity node, lists
    has_state edges where the target State node's label OR the edge sentence
    carries a food-relevant signal. Renders one block per member.

    Previously (Phase 1a): single-person, Jukka-only, prose-keyword filter
    over his entity card description. That ignored 31 food-relevant State
    edges on Annika (vegetable avoidance etc.), 19 on Peter, 35 on Katy.

    If any one member's signal count exceeds RUNAWAY_THRESHOLD, surface a
    count warning rather than silently truncating (no-list-caps-in-auditor
    -snapshots principle).
    """
    if not household_members:
        return "(no household members resolved — proposer should default to neutral household meals)"

    from app.models.base import get_session

    RUNAWAY_THRESHOLD = 30

    try:
        session = get_session()
        try:
            blocks: list[str] = []
            for member in household_members:
                signals = _per_member_dietary_signals(session, member)
                if signals is None:
                    blocks.append(f"{member}:\n- (no KG entity node for this person)")
                    continue
                if not signals:
                    blocks.append(f"{member}:\n- (no dietary signals captured in KG yet)")
                    continue
                lines = [f"{member}:"]
                for label, sentence in signals[:RUNAWAY_THRESHOLD]:
                    lines.append(f"- {label} — {sentence}")
                if len(signals) > RUNAWAY_THRESHOLD:
                    lines.append(
                        f"- (… plus {len(signals) - RUNAWAY_THRESHOLD} more food-relevant State edges "
                        f"not shown — runaway signal density; consider tightening the keyword filter)"
                    )
                blocks.append("\n".join(lines))
        finally:
            session.close()
        return "Per-person dietary signals (from KG):\n\n" + "\n\n".join(blocks)
    except Exception as e:
        logger.warning("[meal_context] dietary_context build failed: %s", e)
        return "(error reading dietary context)"


# Food-domain belief_key prefixes. belief_updater (in the belief_engine
# pipeline) assigns these slugs at write time — first dot-segment is
# the domain the LLM classified the belief into. Filter is structural:
# match on prefix, not on prose. No regex on statements.
#
# If a new food-relevant prefix shows up in real data, add it here.
# The list grows by observation, not speculation.
_FOOD_BELIEF_KEY_PREFIXES = (
    "food.",
    "meal.",
    "diet.",
    "kitchen.",
    "cooking.",
    "grocery.",
    "health.nutrition.",
    "health.hydration.",
    "health.weight.",
    "health.alcohol.",
    "routine.friday_night_meats.",
    "social.friday_night_meats.",
)


def _build_food_beliefs(agent: str = "meal_planner") -> str:
    """Food-belief lane for the meal agents — belief-engine v2 cutover pilot.

    With subsystem flag `meal_beliefs_v2` ON (the default), beliefs come from
    the v2 store via the context-scoped retrieval API (`beliefs_for_context`):
    ranked by temporal-applicability + recency + frequency + relevance instead
    of belief_key prefix matching, with every surfacing logged (the bandit
    seed). Flip the flag in /dev/subsystems to return to the legacy v1
    two-lane table read (`_build_food_beliefs_v1`).

    A v2 lane failure is LOUD: ERROR log + an explicit failure marker in the
    prompt (never a silent swap to v1 — the flag is the human's switch).
    """
    from app.assistant.utils.subsystem_flags import is_subsystem_enabled

    if not is_subsystem_enabled("meal_beliefs_v2"):
        return _build_food_beliefs_v1()
    try:
        capture: dict = {}
        text = _build_food_beliefs_v2(agent=agent, capture=capture)
    except Exception as e:
        logger.error(
            "[meal_context] belief v2 lane failed (flag meal_beliefs_v2 is ON): %s", e,
        )
        logger.debug("[meal_context] belief v2 lane exception", exc_info=True)
        return (
            "(BELIEF LANE ERROR: belief-engine v2 store unavailable — see server "
            "log. Flip subsystem flag 'meal_beliefs_v2' off to use the legacy lane.)"
        )

    # Shadow-run comparison: record what BOTH lanes produced this run so the
    # tuning session / v1 retirement rests on visible diffs (/beliefs-shadow).
    # Shadowing must never cost the planner its context — failures log ERROR
    # and the v2 text still returns.
    try:
        from belief_engine_v2.shadow import record_shadow_run
        record_shadow_run(
            agent=agent,
            v2_rows=capture.get("rows") or [],
            v1_text=_build_food_beliefs_v1(),
        )
    except Exception as e:
        logger.error("[meal_context] shadow-run recording failed: %s", e)
        logger.debug("[meal_context] shadow recording exception", exc_info=True)
    return text


_MEAL_BELIEFS_QUERY = (
    "food meals cooking dining snacks groceries dietary restrictions "
    "preferences dislikes and eating routines for the user and household"
)
_V2_RECENT_DAYS = 21      # mark beliefs observed this recently as current intent
_V2_K = 40                # candidate-set size (the planner LLM judges relevance)


def _build_food_beliefs_v2(*, agent: str, db_path: Optional[str] = None,
                           embedder=None, now=None,
                           capture: Optional[dict] = None) -> str:
    """Context-scoped v2 lane. `db_path`/`embedder`/`now` are injectable for
    tests; production resolves the live shadow store + app embedder.
    `capture`, when given, receives {'rows': <the surfaced rows>} for the
    shadow-run comparison."""
    from belief_engine_v2.ingest import open_live_store
    from belief_engine_v2.retrieval import beliefs_for_context
    from belief_engine_v2.surfacing import log_surfaced

    if embedder is None:
        from app.assistant.embeddings.embedder import embed_texts
        embedder = lambda t: embed_texts([t])[0]  # noqa: E731
    if db_path is None:
        db_path = str(get_repo_root() / "belief_engine_v2" / "data" / "belief_v2_live.db")
        if not Path(db_path).exists():
            raise FileNotFoundError(f"belief v2 live store missing: {db_path}")
    # Naive local time: belief timestamps are stored naive; retrieval's
    # recency math subtracts directly.
    now = now or get_local_time().replace(tzinfo=None)

    store = open_live_store(db_path, with_verifier=False)   # read path: no LLM
    try:
        rows = beliefs_for_context(
            store, now, agent=agent, query=_MEAL_BELIEFS_QUERY,
            embedder=embedder, k=_V2_K, horizon="day",
        )
        log_surfaced(store.conn, agent=agent, rows=rows)
    finally:
        store.close()
    if capture is not None:
        capture["rows"] = rows

    if not rows:
        return "(belief v2 store has no active beliefs yet)"

    cutoff = (now - timedelta(days=_V2_RECENT_DAYS)).isoformat()
    lines = [
        f"Food & household beliefs (belief-engine v2, context-ranked, top {len(rows)}, "
        f"today is {now.strftime('%A %Y-%m-%d')}). Each item shows when it was last "
        f"observed. Items observed in the last {_V2_RECENT_DAYS} days are current "
        f"intent — let them override older preferences. TIME-SCOPE the transient/"
        f"episodic ones with common sense: they describe a passing situation, so "
        f"judge from the observed date whether it still applies (a stomach bug "
        f"observed 3+ days ago is probably over — plan normal food unless something "
        f"says otherwise; one observed yesterday still governs today's meals).",
    ]
    for r in rows:
        stmt = (r.get("statement_nl") or "").strip()
        if not stmt:
            continue
        last = (r.get("last_observed") or "")
        recent = last >= cutoff
        meta = [k for k in ((r.get("kind") or ""),) if k]
        if last:
            meta.append(f"observed {last[:10]}")
        if r.get("obs_count") and int(r["obs_count"]) > 1:
            meta.append(f"{int(r['obs_count'])}x")
        if recent:
            meta.append("recent")
        suffix = f"  [{', '.join(meta)}]" if meta else ""
        lines.append(f"- {stmt}{suffix}")
    return "\n".join(lines)


def _build_food_beliefs_v1() -> str:
    """LEGACY lane (old belief engine). Pull food-domain beliefs from
    BeliefStore (user_beliefs table), in two lanes.

    Ranking by net_weight alone buries fresh feedback: a brand-new
    "kids won't eat zucchini" has near-zero weight (NULL until the nightly
    recompute) and never clears the established top-N, so the planner never
    sees it. So we add a RECENCY lane — food beliefs backed by a user comment
    in the last RECENT_FEEDBACK_DAYS days — and surface it AHEAD of the
    weight-ranked lane (deduped by belief_key).

    The recency lane keys on recent user_comment EVIDENCE, not last_confirmed:
    the nightly belief pipeline re-confirms beliefs from insights and bumps
    last_confirmed broadly (~64/103 food beliefs in 21d), which would flood the
    lane. A recent user_comment is the precise "the user just told us this" signal.

    Established lane filter is structural: belief_key starts with a food-domain
    prefix belief_engine assigned at write time. Top N=30 shown per lane.
    """
    from app.models.base import get_session
    from sqlalchemy import text as sql_text

    RUNAWAY_THRESHOLD = 30        # established (weight-ranked) lane cap
    RECENT_FEEDBACK_DAYS = 21     # recency lane window
    RECENT_CAP = 25              # recency lane cap (its own runaway guard)
    ACTIVE = ("status IN ('active','high_confidence','medium_confidence','low_confidence') "
              "OR status IS NULL")

    def _is_food(key: str) -> bool:
        return any((key or "").startswith(p) for p in _FOOD_BELIEF_KEY_PREFIXES)

    try:
        cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=RECENT_FEEDBACK_DAYS)).isoformat()
        session = get_session()
        try:
            established_rows = session.execute(sql_text(f"""
                SELECT belief_key, statement, current_net_weight, confidence
                FROM user_beliefs
                WHERE {ACTIVE}
                ORDER BY current_net_weight DESC
                LIMIT 500
            """)).fetchall()
            # Recency lane keyed on recent user-comment evidence — weight-independent,
            # so NULL-weight fresh beliefs are included (the weight-ordered query above
            # can't reach them while there are >500 non-null beliefs).
            # signal_type<>'contradicts': a belief whose only recent comment REFUTED it
            # (e.g. the stale "kids will eat zucchini") must not surface as current
            # intent — its positive counterpart ("kids dislike zucchini") carries that.
            recent_rows = session.execute(sql_text(f"""
                SELECT b.belief_key, b.statement, b.current_net_weight, b.confidence,
                       MAX(COALESCE(e.created_at, e.source_date)) AS last_feedback
                FROM user_beliefs b
                JOIN belief_evidence e ON e.belief_id = b.id
                WHERE ({ACTIVE})
                  AND e.source_type = 'user_comment'
                  AND e.signal_type <> 'contradicts'
                  AND COALESCE(e.created_at, e.source_date, '') >= :cutoff
                GROUP BY b.id
                ORDER BY last_feedback DESC
            """), {"cutoff": cutoff_iso}).fetchall()
        finally:
            session.close()

        established = [
            (r[0], r[1] or "", r[2] if r[2] is not None else 0.0, r[3])
            for r in established_rows if _is_food(r[0] or "")
        ]
        recent = [
            (r[0], r[1] or "", r[2] if r[2] is not None else 0.0, r[3])
            for r in recent_rows if _is_food(r[0] or "")
        ]

        if not established and not recent:
            return "(no food-relevant beliefs in store yet)"

        recent_keys = {row[0] for row in recent}
        lines = []

        if recent:
            lines.append(
                f"Recent user feedback ({RECENT_FEEDBACK_DAYS}d) — the user said these "
                f"directly; treat as current intent and let them override older preferences:"
            )
            for key, stmt, weight, conf in recent[:RECENT_CAP]:
                lines.append(f"- [{key}] (recent feedback, conf={conf or 'n/a'})")
                lines.append(f"  {stmt}")
            if len(recent) > RECENT_CAP:
                lines.append(f"- (… plus {len(recent) - RECENT_CAP} more recent-feedback beliefs not shown)")
            lines.append("")

        established_deduped = [t for t in established if t[0] not in recent_keys]
        lines.append("Established food beliefs (BeliefStore, by net_weight desc):")
        for key, stmt, weight, conf in established_deduped[:RUNAWAY_THRESHOLD]:
            lines.append(f"- [{key}] (net={weight:.1f}, conf={conf or 'n/a'})")
            lines.append(f"  {stmt}")
        if len(established_deduped) > RUNAWAY_THRESHOLD:
            lines.append(
                f"- (… plus {len(established_deduped) - RUNAWAY_THRESHOLD} more food-relevant "
                f"beliefs not shown — runaway signal density; consider tightening filter or "
                f"adding a real domain='food' tag at write time)"
            )
        return "\n".join(lines)
    except Exception as e:
        logger.warning("[meal_context] food_beliefs build failed: %s", e)
        return "(error reading food beliefs)"


def _build_food_calendar(*, now_local: datetime) -> str:
    """Already-planned meals on the Food calendar, next 7 days."""
    try:
        from app.assistant.ServiceLocator.service_locator import DI
        from app.assistant.utils.pydantic_classes import ToolMessage
        cls = DI.tool_registry.get_tool_class("get_calendar_events")
        if cls is None:
            return _NO_DATA_FMT.format(kind="food calendar")
        tool = cls()
        start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=7)
        tm = ToolMessage(
            tool_name="get_calendar_events",
            tool_data={
                "arguments": {
                    "start_date": start.isoformat(),
                    "end_date": end.isoformat(),
                    "calendar_names": ["Food"],
                    "single_events": True,
                },
            },
        )
        result = tool.execute(tm)
    except Exception as e:
        logger.warning("[meal_context] food_calendar fetch failed: %s", e)
        return _NO_DATA_FMT.format(kind="food calendar")

    content = (getattr(result, "content", None) or "").strip()
    if not content:
        return "(Food calendar is empty for the next 7 days — wide open for proposals)"
    return content


def _build_addressable_concerns() -> str:
    """Read concerns_register, filter to concerns where addressable_by
    includes meal_proposer."""
    path = get_repo_root() / "resources" / "subconscious" / "resource_concerns_register.json"
    if not path.is_file():
        return "(no concerns_register yet)"
    try:
        register = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("[meal_context] concerns_register parse failed: %s", e)
        return "(error reading concerns_register)"

    matched: List[Dict[str, Any]] = []
    for bucket in ("active", "addressing"):
        for c in register.get(bucket, []) or []:
            if "meal_proposer" in (c.get("addressable_by") or []):
                matched.append(c)

    if not matched:
        return "(no concerns currently routed to meal_proposer)"

    lines: List[str] = []
    for c in matched:
        # No concern_id in the rendered text — concern_ids are noticer-
        # internal bookkeeping (lifecycle: active/addressing/resolved/
        # dormant). Proposers describe concerns in prose; the noticer
        # matches semantically at next tick. No UUID linkage.
        lines.append(
            f"[{c.get('severity', '?')}/{c.get('horizon', '?')}] "
            f"{c.get('title', '(untitled)')}"
        )
        lines.append(f"  subject: {c.get('subject') or 'household'}")
        lines.append(f"  kind: {c.get('kind', '?')}")
        notes = (c.get("notes") or "").strip()
        if notes:
            lines.append(f"  notes: {notes[:400]}")
        # Also include co-addressable agents so the proposer knows it's
        # not the only one being asked to address this.
        co = [a for a in (c.get("addressable_by") or []) if a != "meal_proposer"]
        if co:
            lines.append(f"  co-addressable: {', '.join(co)}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _build_recent_planned_meals() -> str:
    """Compact summary of the past 4 weeks of weekly meal plan pods.

    Drives variety + cadence rules: the planner reads this to (a)
    avoid repeating a dish within 14 days, and (b) keep the
    fast_food / takeout / dine_out count reasonable across the
    rolling month.

    Renders as: per-week block with one line per non-flex/skip slot
    showing date, window, slot_type, dish. Plus a trailing count
    summary across the whole 4-week window.

    First-run / no plans yet -> a short hint line.
    """
    from collections import Counter
    from app.assistant.pod_store.pod_store import PodStore

    try:
        store = PodStore()
        pods = store.query(kind="plan.weekly_meals", since="28d", limit=8)
    except Exception as e:
        logger.warning("[meal_context] recent_planned_meals fetch failed: %s", e)
        return "(error reading recent plans — proceed without variety/cadence check)"

    if not pods:
        return "(no prior plan pods in the last 4 weeks — first plan, so no variety/cadence history yet)"

    # Sort by week_start ascending (oldest first) for readability.
    def _week_key(p) -> str:
        return str((p.metadata or {}).get("week_start_date") or "")
    pods_sorted = sorted(pods, key=_week_key)

    out: List[str] = []
    type_counts: Counter = Counter()
    for p in pods_sorted:
        meta = p.metadata or {}
        ws = meta.get("week_start_date") or "?"
        slots = meta.get("slots") or []
        out.append(f"### Week of {ws}")
        for s in slots:
            st = (s.get("slot_type") or "").lower()
            if st in {"flex", "skip"}:
                continue
            dish = (s.get("dish") or "").strip()
            window = s.get("meal_window") or "?"
            date = s.get("date") or "?"
            if not dish:
                continue
            out.append(f"- {date} {window}: [{st}] {dish}")
            type_counts[st] += 1
        out.append("")

    out.append("### 4-week totals by slot_type")
    for st in ("home_cook", "fast_food", "takeout", "dine_out", "novelty", "leftover"):
        n = type_counts.get(st, 0)
        out.append(f"- {st}: {n}")

    return "\n".join(out).rstrip()


def _build_fast_food_count() -> str:
    """Scan diet_log for delivery/restaurant entries in last 7 days.

    For Phase 1a, this is approximate — reads the diet_log markdown looking
    for 'delivery' or 'restaurant' tags. Better integration once diet_log
    has structured source labels.
    """
    md_path = get_repo_root() / "resources" / "dayflow_pipeline_outputs" / "resource_diet_log_today_text.md"
    if not md_path.is_file():
        return "0 (no diet log to scan)"
    try:
        text = md_path.read_text(encoding="utf-8").lower()
    except Exception:
        return "0 (error)"

    # Crude but useful for v0
    keywords = ("delivery", "doordash", "uber eats", "restaurant", "fast food", "mcd", "takeout")
    count = sum(1 for k in keywords if k in text)
    return f"{count} (rough scan; structured fast-food tagging is Phase 1b)"


# ── Phase 1c.1: external lists (Ralphs standing + agent's weekly) ─────────


_EXTERNAL_LISTS_REL = "resources/subconscious/resource_meal_proposer_external_lists.json"
_WEEKLY_DOC_STATE_REL = "resources/subconscious/resource_meal_proposer_weekly_doc_state.json"


def _load_external_lists() -> Dict[str, Any]:
    path = get_repo_root() / _EXTERNAL_LISTS_REL
    if not path.is_file():
        return {"lists": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("[meal_context] external_lists parse failed: %s", e)
        return {"lists": []}


def load_weekly_doc_state() -> Dict[str, Any]:
    """Public — also used by meal_persist."""
    path = get_repo_root() / _WEEKLY_DOC_STATE_REL
    if not path.is_file():
        return {"doc_id": None, "doc_title": None, "last_built_week_start": None, "last_edit_at_utc": None}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("[meal_context] weekly_doc_state parse failed: %s", e)
        return {"doc_id": None, "doc_title": None, "last_built_week_start": None, "last_edit_at_utc": None}


def save_weekly_doc_state(state: Dict[str, Any]) -> None:
    path = get_repo_root() / _WEEKLY_DOC_STATE_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def _build_ralphs_standing_list() -> str:
    """Read the Ralphs doc (Jukka's standing list of staples) via
    get_google_doc tool. Read-only — the proposer never edits this list."""
    lists = _load_external_lists().get("lists") or []
    ralphs = next((l for l in lists if (l.get("name") or "").lower() == "ralphs"), None)
    if not ralphs:
        return "(no Ralphs list configured in resource_meal_proposer_external_lists.json)"
    doc_id = ralphs.get("doc_id")
    if not doc_id or "REPLACE_WITH_REAL" in str(doc_id):
        return "(Ralphs doc_id not set in config; placeholder still in place)"

    body = _fetch_google_doc_body(doc_id)
    if body is None:
        return f"(Ralphs doc {doc_id} could not be read — google auth issue?)"

    return (
        "Jukka's standing Ralphs list (READ-ONLY, these items are staples he "
        "always buys — treat them as 'about to be acquired' and DON'T include "
        "them in your weekly shopping list or shopping_run):\n\n"
        f"{body.strip() or '(doc is empty)'}"
    )


def _build_agent_weekly_list_state() -> str:
    """Read the agent's own per-week shopping list (if it exists) via
    get_google_doc tool. Used so weekly_plan ticks can see the current state
    before replacing it, and daily ticks can read what's already there."""
    state = load_weekly_doc_state()
    doc_id = state.get("doc_id")
    last_built = state.get("last_built_week_start")
    if not doc_id:
        return (
            "(No agent weekly list doc yet — this is bootstrap. On the next "
            "weekly_plan tick, set weekly_shopping_list.action='create' and "
            "provide body_markdown + week_start_date; persist will create the "
            "doc and store its id.)"
        )

    body = _fetch_google_doc_body(doc_id)
    if body is None:
        return f"(agent weekly list doc {doc_id} could not be read)"

    return (
        f"Agent's current weekly shopping list doc (doc_id={doc_id}, "
        f"last_built_week_start={last_built}):\n\n"
        f"{body.strip() or '(doc is empty)'}"
    )


def _fetch_google_doc_body(doc_id: str, *, max_chars: int = 8000) -> Optional[str]:
    """Single shared fetcher. Uses the get_google_doc tool via ToolRegistry."""
    try:
        from app.assistant.ServiceLocator.service_locator import DI
        from app.assistant.utils.pydantic_classes import ToolMessage

        cls = DI.tool_registry.get_tool_class("get_google_doc")
        if cls is None:
            logger.warning("[meal_context] get_google_doc tool unavailable")
            return None
        tool = cls()
        tm = ToolMessage(
            tool_name="get_google_doc",
            tool_data={
                "arguments": {
                    "document_id": doc_id,
                    "include_body": True,
                    "max_chars": max_chars,
                },
            },
        )
        result = tool.execute(tm)
        data = getattr(result, "data", None) or {}
        body = data.get("body")
        if body is None:
            return None
        return str(body)
    except Exception as e:
        logger.warning("[meal_context] fetch google doc %s failed: %s", doc_id, e)
        return None


# ── Phase 1c.2: helpers for the weekly/daily split ────────────────────────


def _compute_week_start_date(now_local: datetime) -> str:
    """ISO date of the Monday on or before now_local."""
    days_since_monday = now_local.weekday()  # Monday = 0
    monday = (now_local - timedelta(days=days_since_monday)).date()
    return monday.isoformat()


def _build_latest_weekly_plan() -> str:
    """Read the most recent plan.weekly_meals pod (from weekly_meal_planner).
    The daily proposer reads this to know which slots are anchor/leftover/flex
    for the next 24-48h. Falls back gracefully when no plan exists."""
    try:
        from app.assistant.pod_store.pod_store import PodStore
        store = PodStore()
        since = datetime.now(timezone.utc) - timedelta(days=10)
        results = store.query(kind="plan.weekly_meals", since_utc=since, limit=5)
    except Exception as e:
        logger.warning("[meal_context] weekly plan fetch failed: %s", e)
        return "(no weekly plan available — propose from inventory + recipes + concerns; set fills_weekly_plan_slot=null)"

    if not results:
        return "(no weekly plan pod exists yet — run weekly_meal_planner first; propose from inventory + recipes + concerns; set fills_weekly_plan_slot=null)"

    latest = results[0]
    return f"{latest.one_liner}\n\n{latest.body}"
