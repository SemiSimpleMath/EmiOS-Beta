"""
Deterministic tool pre-filter.

Maps task text onto tool metadata (domain + actions) via a small keyword table,
then returns the tool names whose contract metadata matches. Intended to run
BEFORE the LLM-based `shared::tool_narrower` so the narrower either sees a much
smaller candidate list or is skipped entirely.

Only tools that have the new metadata shape (domain + actions) participate in
filtering. Tools without metadata are returned as-is (pass-through), so tools
that haven't been migrated yet aren't accidentally dropped.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

# --- Task-keyword to domain map ---
# A matching keyword in the task text adds its domain to the inferred set.
_DOMAIN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "email": (
        "email", "emails", "e-mail", "e-mails", "mail", "inbox",
        "gmail", "sender", "subject", "unread", "message from",
        "messages from", "reply", "replied", "drafts",
    ),
    "calendar": (
        "calendar", "meeting", "meetings", "appointment", "appointments",
        "event", "events", "schedule ", " schedule", "schedul",  # "scheduled", "scheduling"
        "agenda", "rsvp",
    ),
    "todo": (
        "todo", "to-do", "to do", "task list", "tasks list",
        "my tasks", "mark.*done", "checkbox", "checklist",
    ),
    "reminder": (
        "remind", "reminder", "reminders", "alert me", "ping me",
        "alarm", "nudge",
    ),
    "doc": (
        "google doc", "google docs", "gdoc", "google document",
        "document draft", "doc ", " doc.",
    ),
    "daily_summary": (
        "daily summary", "daily briefing", "daily_summary",
        "morning briefing", "morning brief",
    ),
    "user_interaction": (
        "ask the user", "ask user", "clarify", "confirm with",
        "let the user know", "notify",
    ),
    "kg": (
        "knowledge graph", " kg ", "kg.", "entity graph",
    ),
    "web": (
        "search the web", "web search", "google it", "look up online",
        "scrape", "find information about",
        "summarize this link", "summarize the link", "summarize this page",
        "peek at this link", "peek at the link",
    ),
    "weather": (
        "weather", "forecast", "temperature outside", "raining",
        "snow", "cold outside", "hot outside", "will it rain",
    ),
    "news": (
        "news", "headlines", "news article", "top stories",
        "latest news",
    ),
    "smart_home": (
        "nest", "thermostat", "lights", "light ", "ring camera",
        "smart home", "turn on the", "turn off the", "dim the",
        "brighten", "camera", "doorbell", "siren",
    ),
    "browser": (
        "use playwright", "with playwright", "open the browser",
        "navigate to", "click on", "fill in the form", "log in to",
        "check out", "go to ", "on that page", "in the browser",
        "modal", "scroll down", "scroll up", "snapshot",
    ),
}

# --- Task-keyword to action map ---
# Action phrases are captured per-domain-blind and intersected with each tool's
# declared actions. A tool is kept when (at least one) tool action intersects.
_ACTION_KEYWORDS: dict[str, tuple[str, ...]] = {
    "read": ("read", "show", "show me", "list", "what are", "what's on", "any ", "do i have"),
    "fetch": ("fetch", "get", "pull", "grab"),
    "search": ("search", "find", "look for", "locate"),
    "stats": ("stats", "how many", "count of", "total ", "aggregate", "summary of"),
    "summarize": ("summarize", "summary", "summarise"),
    "triage": ("important", "triage", "priority", "urgent"),
    "create": ("create", "add ", "make a", "set up", "set a", "new ", "schedule a", "schedule an", "book "),
    "update": ("update", "change", "modify", "edit", "rename"),
    "complete": ("mark done", "mark complete", "finished", "done with"),
    "delete": ("delete", "remove", "get rid of", "drop"),
    "trash": ("trash", "throw away", "move to bin"),
    "cancel": ("cancel", "call off"),
    "send": ("send", "email to", "reply to"),
    "compose": ("compose", "draft an email", "draft a message"),
    "remind": ("remind me", "remind", "set a reminder"),
    "notify": ("notify", "alert "),
    "ask": ("ask user", "ask the user"),
    "confirm": ("confirm",),
    "edit": ("edit",),
    "attach": ("attach",),
    "push": ("push to ", "save as gdoc"),
    "control": ("turn on", "turn off", "switch on", "switch off", "dim ", "brighten"),
    "set": ("set to", "set the"),
    "peek": ("peek at",),
    "extract": ("extract",),
    "navigate": ("navigate to", "go to ", "open url"),
    "click": ("click on", "click the"),
    "fill": ("fill in", "fill the", "type into"),
    "scroll": ("scroll down", "scroll up"),
    "browse": ("browse to", "browse the"),
}


def _find_any(text: str, phrases: Iterable[str]) -> bool:
    for p in phrases:
        # Treat short alphanumeric as word boundaries; longer phrases as substring.
        if " " in p or "-" in p or "." in p:
            if p in text:
                return True
        else:
            if re.search(rf"\b{re.escape(p)}\b", text):
                return True
    return False


def extract_domains(task_text: str) -> set[str]:
    text = task_text.lower()
    domains: set[str] = set()
    for domain, kws in _DOMAIN_KEYWORDS.items():
        if _find_any(text, kws):
            domains.add(domain)
    return domains


def extract_actions(task_text: str) -> set[str]:
    text = task_text.lower()
    actions: set[str] = set()
    for action, kws in _ACTION_KEYWORDS.items():
        if _find_any(text, kws):
            actions.add(action)
    return actions


def filter_candidates(
    *,
    task_text: str,
    candidate_tools: list[dict[str, Any]],
    tool_registry: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    Narrow a candidate tool list deterministically using tool metadata.

    Returns (filtered_candidates, trace) where trace is diagnostic info:
      {
        "inferred_domains": [...],
        "inferred_actions": [...],
        "kept_count": int,
        "dropped_count": int,
        "unmigrated_count": int,  # tools without domain/actions metadata — kept
      }

    If no domains are inferred, returns the original candidate list unchanged
    (no confident signal → defer to the LLM narrower).
    """
    domains = extract_domains(task_text)
    actions = extract_actions(task_text)

    if not domains:
        return candidate_tools, {
            "inferred_domains": [],
            "inferred_actions": sorted(actions),
            "kept_count": len(candidate_tools),
            "dropped_count": 0,
            "unmigrated_count": 0,
            "short_circuited": False,
            "reason": "no domain inferred — falling through to narrower",
        }

    kept: list[dict[str, Any]] = []
    unmigrated = 0
    dropped = 0
    for cand in candidate_tools:
        tool_name = str(cand.get("name") or "").strip()
        if not tool_name:
            continue

        contract = tool_registry.get_tool_contract(tool_name) if tool_registry else None
        meta = contract.get("metadata", {}) if isinstance(contract, dict) else {}
        tool_domain = meta.get("domain")
        tool_actions = meta.get("actions") if isinstance(meta.get("actions"), list) else []

        # Managers and unmigrated tools pass through (we don't want to drop them
        # accidentally). They still go to the LLM narrower.
        is_manager = tool_name.endswith("_manager")
        if is_manager:
            kept.append(cand)
            continue
        if not tool_domain or not tool_actions:
            unmigrated += 1
            kept.append(cand)
            continue

        # Keep iff domain matches OR actions intersect. Never require both —
        # action keyword tables can't enumerate every verb synonym, so demanding
        # both false-drops tools like get_email_stats when the task says
        # "statistics" (no keyword match) but the domain is clearly email.
        domain_match = tool_domain in domains
        action_match = bool(actions) and bool(set(tool_actions) & actions)
        if not (domain_match or action_match):
            dropped += 1
            continue

        kept.append(cand)

    return kept, {
        "inferred_domains": sorted(domains),
        "inferred_actions": sorted(actions),
        "kept_count": len(kept),
        "dropped_count": dropped,
        "unmigrated_count": unmigrated,
        "short_circuited": False,
        "reason": f"{len(kept)}/{len(candidate_tools)} pass metadata filter",
    }
