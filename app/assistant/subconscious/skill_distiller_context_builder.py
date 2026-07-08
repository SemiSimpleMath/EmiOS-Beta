"""Build the injected context dict for skill_distiller.

Gathers the past 7 days of subconscious activity: every intention.*
pod minted, every plan.weekly_schedule pod (with conflict resolutions),
every user-facing ticket answered, chat clusters mentioning experiences,
and references to the canonical rule files for the distiller to know
what already exists.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root
from app.assistant.utils.time_utils import get_local_time

logger = get_logger(__name__)


# Intention variants (two-axis model: kind="intention" + governed variant
# tag). The distiller reads EVERYTHING intention — items and #run_summary
# pods alike — so one kind query covers what used to be seven.
_INTENTION_VARIANTS = ("meal", "wellness", "romantic", "shopping")


def build_skill_distiller_context() -> Dict[str, str]:
    now_local = get_local_time()
    now_utc = datetime.now(timezone.utc)
    window_end = now_utc.date()
    window_start = window_end - timedelta(days=7)
    window_label = f"{window_start.isoformat()}_to_{window_end.isoformat()}"

    return {
        "date_time": now_local.strftime("%Y-%m-%d %H:%M %Z"),
        "day_of_week": now_local.strftime("%A"),
        "week_window_label": window_label,
        "recent_intentions_summary": _build_recent_intentions_summary(),
        "recent_arbiter_decisions": _build_recent_arbiter_decisions(),
        "recent_user_signals": _build_recent_user_signals(),
        "recent_outcome_chat": _build_recent_outcome_chat(),
        "current_priority_rules": _build_current_priority_rules(),
        "current_proposer_house_rules_index": _build_house_rules_index(),
        "prior_distillations": _build_prior_distillations(),
    }


def _build_recent_intentions_summary() -> str:
    """Group all intention pods from the past 7 days by variant (run
    summaries bucket separately under '<variant> run_summary')."""
    try:
        from app.assistant.pod_store.pod_store import PodStore
        store = PodStore()
        pods: List[Any] = store.query(kind="intention", since="7d", limit=600)
    except Exception as e:
        logger.warning("[skill_distiller_context] intention fetch failed: %s", e)
        return "(no intention pods readable)"

    if not pods:
        return "(no intention pods in the last 7 days)"

    grouped: Dict[str, List[Any]] = {}
    for pod in pods:
        tags = set(pod.tags or [])
        domain = next((v for v in _INTENTION_VARIANTS if v in tags), "other")
        if "run_summary" in tags:
            domain = f"{domain} run_summary"
        grouped.setdefault(domain, []).append(pod)

    lines: List[str] = [f"# {len(pods)} intention pods in the last 7 days, grouped by variant", ""]
    for domain in sorted(grouped.keys()):
        bucket = grouped[domain]
        lines.append(f"## intention #{domain} ({len(bucket)})")
        for pod in bucket[:30]:  # cap per-domain to avoid runaway
            meta = pod.metadata or {}
            head_bits: List[str] = []
            date_val = meta.get("date") or ""
            if date_val:
                head_bits.append(f"[{date_val}]")
            kind_inside = meta.get("kind")
            if kind_inside:
                head_bits.append(kind_inside)
            actors = meta.get("actors") or []
            if actors:
                head_bits.append(f"actors={','.join(actors)}")
            head = " ".join(head_bits)
            one_liner = (pod.one_liner or "").replace("\n", " ")[:200]
            lines.append(f"- {pod.pod_id} {head}")
            lines.append(f"  {one_liner}")
            if meta.get("source"):
                lines.append(f"  source: {meta['source']}  confidence: {meta.get('confidence', '?')}  novelty: {meta.get('novelty', '?')}")
        if len(bucket) > 30:
            lines.append(f"  ... ({len(bucket) - 30} more truncated)")
        lines.append("")
    return "\n".join(lines).rstrip()


def _build_recent_arbiter_decisions() -> str:
    """Every plan.weekly_schedule pod in the window — surface conflict
    resolutions + which intentions were anchored vs displaced."""
    try:
        from app.assistant.pod_store.pod_store import PodStore
        store = PodStore()
        results = store.query(kind="plan", tags=["weekly_schedule"], since="7d", limit=10)
    except Exception as e:
        logger.warning("[skill_distiller_context] arbiter fetch failed: %s", e)
        return "(no arbiter decisions readable)"

    if not results:
        return "(no plan.weekly_schedule pods in the last 7 days — arbiter hasn't run or no conflicts)"

    lines: List[str] = [f"# {len(results)} arbiter run(s) in the last 7 days", ""]
    for pod in results:
        meta = pod.metadata or {}
        lines.append(f"## {pod.pod_id} — week of {meta.get('week_start_date', '?')}")
        sched = meta.get("schedule") or []
        anchored = [s for s in sched if s.get("is_anchor")]
        lines.append(f"  schedule: {len(sched)} items ({len(anchored)} anchored)")

        resolved = meta.get("conflicts_resolved") or []
        if resolved:
            lines.append(f"  conflicts_resolved: {len(resolved)}")
            for c in resolved:
                lines.append(f"    • {c.get('conflict_summary', '')[:200]}")
                lines.append(f"      chose: {c.get('chosen_pod_id', '?')}")
                if c.get("displaced_pod_ids"):
                    lines.append(f"      displaced: {', '.join(c['displaced_pod_ids'])}")
                if c.get("priority_rule_applied"):
                    lines.append(f"      rule_applied: {c['priority_rule_applied']}")
                if c.get("reasoning"):
                    lines.append(f"      reasoning: {c['reasoning'][:300]}")

        punted = meta.get("conflicts_for_user") or []
        if punted:
            lines.append(f"  conflicts_for_user: {len(punted)}")
            for c in punted:
                lines.append(f"    • {c.get('conflict_summary', '')[:200]}")
                if c.get("reasoning_for_punt"):
                    lines.append(f"      punted: {c['reasoning_for_punt']}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _build_recent_user_signals() -> str:
    """v0 stub. Future: pull ticket answers, manual edits to rule files,
    user-driven concern resolutions. For now, reports "no signal" so the
    distiller knows this layer isn't fully wired."""
    return (
        "(v0: user-signal aggregation not yet wired — ticket answers and "
        "rule-file edits will appear here once the ticket-answer log + "
        "git-blame-on-rule-files plumbing lands. Distill from intentions + "
        "arbiter + chat for now.)"
    )


def _build_recent_outcome_chat() -> str:
    """Chat clusters in the last 7 days that mention experiences with
    proposed items. v0: scan chat_cluster pods for keywords related to
    meals/wellness/romantic experiences."""
    try:
        from app.assistant.pod_store.pod_store import PodStore
        store = PodStore()
        clusters = store.query(kind="chat_cluster", since="7d", limit=30)
    except Exception as e:
        logger.warning("[skill_distiller_context] chat cluster fetch failed: %s", e)
        return "(no chat clusters readable)"

    if not clusters:
        return "(no chat clusters in the last 7 days)"

    # Keywords that suggest experience-feedback. Conservative filter — we
    # want signal about HOW things landed, not just any mention.
    keywords = (
        "delicious", "tasty", "loved", "love", "liked", "great", "good",
        "yum", "ate", "had ", "tried",
        "boring", "bland", "didn't like", "wasn't great", "skipped",
        "too much", "not enough",
        "tired", "rested", "slept", "workout", "ran", "walked", "yoga",
        "date night", "movie", "dinner out",
        "actually", "turns out", "ended up", "instead",
    )
    keep: List[Any] = []
    for pod in clusters:
        text = ((pod.one_liner or "") + " " + (pod.body or "")).lower()
        if any(k in text for k in keywords):
            keep.append(pod)

    if not keep:
        return "(no chat clusters this week mentioned experience-feedback keywords)"

    lines: List[str] = [f"# {len(keep)} chat clusters with potential experience-feedback signal", ""]
    for pod in keep[:30]:
        snippet = (pod.one_liner or "").replace("\n", " ")[:200]
        lines.append(f"- {pod.pod_id}: {snippet}")
    return "\n".join(lines)


def _build_current_priority_rules() -> str:
    path = get_repo_root() / "resources" / "instructions" / "resource_scheduler_priority_rules.md"
    if not path.is_file():
        return "(no priority_rules file — distiller can propose new priority rules freely)"
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception as e:
        logger.warning("[skill_distiller_context] priority_rules read failed: %s", e)
        return "(error reading priority_rules)"


def _build_house_rules_index() -> str:
    """List the house_rules files that exist + their top-level section
    headings. Distiller uses this to know where to propose additions."""
    files = [
        "resource_meal_proposer_house_rules.md",
        "resource_wellness_proposer_house_rules.md",
        "resource_romantic_proposer_house_rules.md",
        "resource_subconscious_noticer_house_rules.md",
        "resource_scheduler_priority_rules.md",
    ]
    lines: List[str] = []
    for fname in files:
        path = get_repo_root() / "resources" / "instructions" / fname
        if not path.is_file():
            lines.append(f"## {fname} — (not present)")
            continue
        lines.append(f"## {fname}")
        try:
            text = path.read_text(encoding="utf-8")
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("##") and not stripped.startswith("###"):
                    lines.append(f"  - {stripped}")
        except Exception:
            lines.append("  - (error reading sections)")
    return "\n".join(lines)


def _build_prior_distillations() -> str:
    path = get_repo_root() / "resources" / "subconscious" / "resource_learned_skills_proposed.md"
    if not path.is_file():
        return "(no prior distillations — this is the first run)"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except Exception as e:
        logger.warning("[skill_distiller_context] prior distillations read failed: %s", e)
        return "(error reading prior distillations)"
    if not text:
        return "(prior distillations file exists but is empty)"
    # Truncate to last ~3000 chars to keep context bounded
    if len(text) > 3000:
        text = "[earlier distillations truncated]\n\n" + text[-3000:]
    return text
