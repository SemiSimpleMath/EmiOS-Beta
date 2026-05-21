"""Data + actions for the /meals page.

Three responsibilities:
1. Load the latest weekly plan + this week's shopping context for rendering
2. Send the weekly meal plan email to a recipient
3. Mint a delivery.email audit pod recording each send

The route layer (app/routes/meals.py) is a thin wrapper around this.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.assistant.pod_store.contracts import Pod
from app.assistant.pod_store.pod_store import PodStore
from app.assistant.subconscious.meal_email_renderer import (
    consolidate_intention_shopping,
    render_weekly_meal_email,
)
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

# Katy is the canonical Send-To today. The address lives in
# configs/email_allowlist.yaml under "Family"; surfaced here in code so
# the route doesn't have to thread it through. If/when we add more
# recipients, lift this into a small config helper.
KATY_EMAIL = "ksuttorp@yahoo.com"
KATY_DISPLAY_NAME = "Katy"


def load_latest_weekly_plan_pod() -> Optional[Pod]:
    """Most recent plan.weekly_meals pod (across all weeks). Returns None
    if no plan exists yet."""
    store = PodStore()
    results = store.query(kind="plan.weekly_meals", limit=1)
    return results[0] if results else None


def monday_of(d: date) -> date:
    """Monday of the ISO week containing ``d``. ``d.weekday()`` returns
    0 for Monday, so this works without further casing."""
    return d - timedelta(days=d.weekday())


def load_plan_pod_for_week(week_start: str) -> Optional[Pod]:
    """Most recent plan pod whose metadata.week_start_date matches.

    The planner may have run multiple times for the same week; the
    newest produced_at_utc wins (matches PodStore ordering)."""
    store = PodStore()
    # Pull a generous window so we don't miss a plan written a few weeks
    # before the user navigates back to it.
    candidates = store.query(kind="plan.weekly_meals", since="365d", limit=200)
    for p in candidates:
        if str((p.metadata or {}).get("week_start_date") or "") == week_start:
            return p
    return None


def list_existing_plan_week_starts() -> List[str]:
    """Sorted set of every week_start_date that has at least one plan
    pod. Used to drive the prev/next navigation chrome — the arrow is
    live to weeks with plans, otherwise points at the empty state with
    a Generate button."""
    store = PodStore()
    candidates = store.query(kind="plan.weekly_meals", since="365d", limit=500)
    starts = {
        str((p.metadata or {}).get("week_start_date") or "")
        for p in candidates
    }
    return sorted(s for s in starts if s)


def load_recent_intention_shopping_pods(*, days: int = 14) -> List[Pod]:
    """intention.shopping pods from the past `days` — the ad-hoc additions
    daily_meal_proposer mints when a meal needs items outside the weekly
    list."""
    store = PodStore()
    return store.query(kind="intention.shopping", since=f"{days}d", limit=50)


def fetch_weekly_shopping_doc_body() -> Optional[str]:
    """Fetch the agent's weekly shopping list Google Doc body, if it
    exists. Returns None when there's no doc state or the fetch fails."""
    from app.assistant.subconscious.meal_context_builder import load_weekly_doc_state

    state = load_weekly_doc_state()
    doc_id = state.get("doc_id")
    if not doc_id:
        return None

    try:
        from app.assistant.ServiceLocator.service_locator import DI
        from app.assistant.utils.pydantic_classes import ToolMessage

        cls = DI.tool_registry.get_tool_class("get_google_doc")
        if cls is None:
            logger.warning("[meal_page_service] get_google_doc tool unavailable")
            return None
        tool = cls()
        tm = ToolMessage(
            tool_name="get_google_doc",
            tool_data={
                "arguments": {
                    "document_id": doc_id,
                    "include_body": True,
                    "max_chars": 50000,
                },
            },
        )
        result = tool.execute(tm)
        data = getattr(result, "data", None) or {}
        body = data.get("body")
        return str(body).strip() if body else None
    except Exception as e:
        logger.warning("[meal_page_service] fetch weekly doc failed: %s", e)
        return None


def build_shopping_text(*, doc_body: Optional[str], ad_hoc_pods: List[Pod]) -> str:
    """Compose the shopping section: weekly doc body (canonical) plus any
    ad-hoc items added by daily proposer since the doc was last refreshed."""
    sections: List[str] = []
    if doc_body:
        sections.append("From this week's planned list:")
        sections.append(doc_body)

    ad_hoc_items = consolidate_intention_shopping(ad_hoc_pods)
    if ad_hoc_items:
        if sections:
            sections.append("")
        sections.append("Ad-hoc additions (added by daily proposer):")
        for it in ad_hoc_items:
            sections.append(f"- {it}")

    return "\n".join(sections).strip()


def build_page_view_model(week_start: Optional[str] = None) -> Dict[str, Any]:
    """Everything the /meals template needs to render.

    Selection precedence for which week to display:
      1. Explicit ``week_start`` query param, if given.
      2. Latest plan pod that exists (so first-time visitors land on
         real content), if any.
      3. Monday of today (empty state with a Generate button).
    """
    from app.assistant.subconscious.feedback_service import fetch_comments_for

    today = date.today()
    if week_start:
        viewing_week = _parse_week_start(week_start) or monday_of(today)
    else:
        latest = load_latest_weekly_plan_pod()
        if latest is not None:
            viewing_week = (
                _parse_week_start(str((latest.metadata or {}).get("week_start_date") or ""))
                or monday_of(today)
            )
        else:
            viewing_week = monday_of(today)

    viewing_week_iso = viewing_week.isoformat()
    plan_pod = load_plan_pod_for_week(viewing_week_iso)

    doc_body = fetch_weekly_shopping_doc_body()
    ad_hoc_pods = load_recent_intention_shopping_pods()
    shopping_text = build_shopping_text(doc_body=doc_body, ad_hoc_pods=ad_hoc_pods)

    plan_view: Optional[Dict[str, Any]] = None
    plan_comments: list = []
    if plan_pod is not None:
        meta = plan_pod.metadata or {}
        plan_view = {
            "pod_id": plan_pod.pod_id,
            "week_start_date": meta.get("week_start_date") or "",
            "slots": list(meta.get("slots") or []),
            "produced_at_utc": meta.get("produced_at_utc") or "",
            "theme": _extract_theme(plan_pod.body or ""),
        }
        plan_comments = fetch_comments_for(plan_pod.pod_id, limit=20)

    plan_comments_by_scope: Dict[str, list] = {}
    plan_comments_section: list = []
    for c in plan_comments:
        scope = c.get("target_scope")
        if scope:
            plan_comments_by_scope.setdefault(str(scope), []).append(c)
        else:
            plan_comments_section.append(c)

    nav = _build_week_nav(viewing_week, today=today)

    return {
        "plan": plan_view,
        "plan_comments_by_scope": plan_comments_by_scope,
        "plan_comments_section": plan_comments_section,
        "shopping_text": shopping_text,
        "weekly_doc_present": bool(doc_body),
        "ad_hoc_count": len(consolidate_intention_shopping(ad_hoc_pods)),
        "recipient_email": KATY_EMAIL,
        "recipient_name": KATY_DISPLAY_NAME,
        "viewing_week": viewing_week_iso,
        "nav": nav,
    }


def _parse_week_start(s: str) -> Optional[date]:
    try:
        d = datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    # Snap to Monday so a misuse like ?week=2026-05-26 still resolves
    # to the same week the planner would have minted under.
    return monday_of(d)


def _build_week_nav(viewing_week: date, *, today: date) -> Dict[str, Any]:
    """Compute the prev/next week ISO dates plus convenience flags for
    rendering navigation in the template."""
    prev_week = viewing_week - timedelta(days=7)
    next_week = viewing_week + timedelta(days=7)
    today_week = monday_of(today)
    existing_weeks = set(list_existing_plan_week_starts())
    return {
        "viewing_week_iso": viewing_week.isoformat(),
        "prev_week_iso": prev_week.isoformat(),
        "next_week_iso": next_week.isoformat(),
        "today_week_iso": today_week.isoformat(),
        "is_current_week": viewing_week == today_week,
        "prev_has_plan": prev_week.isoformat() in existing_weeks,
        "next_has_plan": next_week.isoformat() in existing_weeks,
    }


def generate_plan_for_week(week_start: str) -> Dict[str, Any]:
    """Run the weekly meal planning chain for ``week_start`` and persist.

    Returns ``{status: ok|error, week_start, plan_pod_id?, message?}``.
    Wraps the same code path the CLI runner uses so the button on the
    /meals empty state produces an identical pod.
    """
    parsed = _parse_week_start(week_start)
    if parsed is None:
        return {"status": "error", "message": f"Invalid week_start: {week_start!r}"}
    week_iso = parsed.isoformat()

    try:
        from app.assistant.ServiceLocator.service_locator import DI, ServiceLocator
        from app.assistant.subconscious.meal_context_builder import (
            build_weekly_meal_planner_context,
        )
        from app.assistant.subconscious.meal_persist import (
            apply_weekly_meal_planner_output,
        )
        from app.assistant.utils.pydantic_classes import (
            Message,
            ScopeContext,
            ScopeResourcePolicy,
        )
    except Exception as e:
        logger.exception("[meal_page_service] generate import failed")
        return {"status": "error", "message": f"import failed: {type(e).__name__}: {e}"}

    if not hasattr(DI, "mam_instance_manager") or DI.mam_instance_manager is None:
        from app.assistant.manager_runtime.mam_instance_manager import (
            MAMInstanceManager,
        )
        ServiceLocator.register(
            "mam_instance_manager",
            MAMInstanceManager(resource_manager=getattr(DI, "resource_manager", None)),
        )

    seed = build_weekly_meal_planner_context()
    seed["week_start_date"] = week_iso
    seed["horizon_days"] = "7"

    distiller = DI.agent_factory.create_agent("meal_context_distiller")
    if distiller is None:
        return {"status": "error", "message": "meal_context_distiller unavailable"}
    distiller_scope = ScopeContext(
        scope_id="subconscious::meal_context_distiller",
        owner_id="system",
        actor_id="meal_page_service",
        surface="internal",
        resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
    )
    try:
        distiller_result = distiller.action_handler(
            Message(agent_input=seed, scope_context=distiller_scope)
        )
    except Exception as e:
        logger.exception("[meal_page_service] distiller raised")
        return {"status": "error", "message": f"distiller raised: {type(e).__name__}: {e}"}
    meal_context: Dict[str, Any] = (
        distiller_result.data
        if isinstance(getattr(distiller_result, "data", None), dict)
        else {}
    )

    manager = DI.multi_agent_manager_factory.create_manager(
        "weekly_meal_planning_manager"
    )
    if manager is None:
        return {"status": "error", "message": "weekly_meal_planning_manager unavailable"}
    mgr_scope = ScopeContext(
        scope_id="subconscious::weekly_meal_planning_manager",
        owner_id="system",
        actor_id="meal_page_service",
        surface="internal",
        resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
    )
    manager_context: Dict[str, Any] = {
        "date_time": seed.get("date_time"),
        "day_of_week": seed.get("day_of_week"),
        "week_start_date": week_iso,
        "meal_context": meal_context,
        "recent_planned_meals": seed.get("recent_planned_meals"),
        "inventory_snapshot": seed.get("inventory_snapshot"),
        "ralphs_standing_list": seed.get("ralphs_standing_list"),
        "agent_weekly_list_state": seed.get("agent_weekly_list_state"),
    }
    bb = manager.blackboard
    for k, v in manager_context.items():
        bb.update_state_value(k, v)
    try:
        DI.manager_invoker.invoke(
            manager, Message(agent_input=manager_context, scope_context=mgr_scope)
        )
    except Exception as e:
        logger.exception("[meal_page_service] manager raised")
        return {"status": "error", "message": f"manager raised: {type(e).__name__}: {e}"}

    output: Dict[str, Any] = {}
    try:
        for m in bb.get_messages():
            if str(getattr(m, "sender", "") or "") == "weekly_meal_planner":
                data = getattr(m, "data", None) or {}
                if isinstance(data, dict) and data:
                    output = data
                    break
    except Exception as e:
        logger.warning("[meal_page_service] blackboard read failed: %s", e)

    if not output:
        return {"status": "error", "message": "planner produced no output"}

    summary = apply_weekly_meal_planner_output(output)
    plan_pod_id = summary.get("plan_pod_id") or ""
    return {
        "status": "ok",
        "week_start": week_iso,
        "plan_pod_id": plan_pod_id,
        "summary": summary,
    }


def send_meal_plan_email(
    *,
    to: Optional[str] = None,
    recipient_name: Optional[str] = None,
    week_start: Optional[str] = None,
) -> Dict[str, Any]:
    """Send a weekly meal plan email to `to` (defaults to Katy).

    When ``week_start`` is given, sends the plan for that week (so the
    "Send to Katy" button on /meals respects whatever week the user
    has navigated to). When omitted, falls back to the latest plan.
    Mints a delivery.email audit pod on success.
    """
    recipient = (to or KATY_EMAIL).strip()
    name = (recipient_name or KATY_DISPLAY_NAME).strip() or recipient

    if week_start:
        parsed = _parse_week_start(week_start)
        plan_pod = load_plan_pod_for_week(parsed.isoformat()) if parsed else None
        if plan_pod is None:
            return {
                "status": "no_plan",
                "message": f"No weekly meal plan exists for week of {week_start}.",
            }
    else:
        plan_pod = load_latest_weekly_plan_pod()
        if plan_pod is None:
            return {
                "status": "no_plan",
                "message": "No weekly meal plan exists yet. Run the weekly planner first.",
            }

    doc_body = fetch_weekly_shopping_doc_body()
    ad_hoc_pods = load_recent_intention_shopping_pods()
    shopping_text = build_shopping_text(doc_body=doc_body, ad_hoc_pods=ad_hoc_pods)

    subject, body = render_weekly_meal_email(plan_pod, shopping_text=shopping_text)

    send_result = _invoke_send_email_tool(to=recipient, subject=subject, body=body)
    if not send_result.get("ok"):
        return {
            "status": "send_failed",
            "message": send_result.get("message") or "Email send failed.",
            "details": send_result,
        }

    delivery_pod_id = _mint_delivery_email_pod(
        to=recipient,
        recipient_name=name,
        subject=subject,
        body=body,
        plan_pod_id=plan_pod.pod_id,
        message_id=send_result.get("message_id") or "",
    )

    return {
        "status": "ok",
        "message": f"Sent weekly meal plan to {name} <{recipient}>.",
        "subject": subject,
        "recipient": recipient,
        "recipient_name": name,
        "plan_pod_id": plan_pod.pod_id,
        "delivery_pod_id": delivery_pod_id,
        "gmail_message_id": send_result.get("message_id") or "",
    }


def _invoke_send_email_tool(*, to: str, subject: str, body: str) -> Dict[str, Any]:
    """Call the SendEmail tool's execute() directly. The recipient is
    already allowlisted (configs/email_allowlist.yaml), and this is a
    user-clicked UI action, so we bypass the approval ticket layer."""
    try:
        from app.assistant.ServiceLocator.service_locator import DI
        from app.assistant.utils.pydantic_classes import ToolMessage

        cls = DI.tool_registry.get_tool_class("send_email")
        if cls is None:
            return {"ok": False, "message": "send_email tool not registered."}
        tool = cls()
        tm = ToolMessage(
            tool_name="send_email",
            tool_data={"arguments": {"to": to, "subject": subject, "body": body}},
        )
        result = tool.execute(tm)
        # SendEmail returns either a ToolResult (success) or a ToolError-shaped
        # message via make_tool_error (failure). Distinguish via result_type.
        result_type = getattr(result, "result_type", None) or ""
        if result_type == "send_email":
            data = getattr(result, "data", None) or {}
            return {
                "ok": True,
                "message": getattr(result, "content", "") or "sent",
                "message_id": data.get("message_id") or "",
            }
        # Treat anything else as an error
        content = getattr(result, "content", "") or ""
        data = getattr(result, "data", None) or {}
        return {
            "ok": False,
            "message": content or "send_email returned an error.",
            "error_data": data,
        }
    except Exception as e:
        logger.exception("[meal_page_service] send_email invocation raised")
        return {"ok": False, "message": f"send_email raised: {type(e).__name__}: {e}"}


def _mint_delivery_email_pod(
    *,
    to: str,
    recipient_name: str,
    subject: str,
    body: str,
    plan_pod_id: str,
    message_id: str,
) -> Optional[str]:
    """Audit record: a delivery.email pod for each send. Body holds the
    exact email body that was delivered (so future-us can answer 'what
    did Katy actually receive on <date>?')."""
    try:
        pod_id = f"datapod:delivery.email:{uuid.uuid4().hex[:24]}"
        now_utc_iso = datetime.now(timezone.utc).isoformat()
        pod = Pod(
            pod_id=pod_id,
            kind="delivery.email",
            tags=["delivery", "email", "meal_plan"],
            one_liner=f"Emailed weekly meal plan to {recipient_name} <{to}>",
            body=(
                f"# Delivery — {now_utc_iso}\n\n"
                f"**To:** {recipient_name} <{to}>\n"
                f"**Subject:** {subject}\n"
                f"**Source plan pod:** {plan_pod_id}\n"
                f"**Gmail message id:** {message_id or '(none)'}\n\n"
                "---\n\n"
                f"{body}"
            ),
            source_refs=[],
            for_agents=[],
            scope_id=None,
            created_by="meal_page_service",
            metadata={
                "delivered_at_utc": now_utc_iso,
                "to": to,
                "recipient_name": recipient_name,
                "subject": subject,
                "source_plan_pod_id": plan_pod_id,
                "gmail_message_id": message_id,
                "via_route": "/meals/send-to-katy",
            },
        )
        PodStore().put(pod)
        return pod_id
    except Exception as e:
        logger.warning("[meal_page_service] mint delivery.email failed: %s", e)
        return None


def _extract_theme(body: str) -> str:
    marker = "**Theme:**"
    for line in body.splitlines():
        line = line.strip()
        if line.startswith(marker):
            return line[len(marker):].strip()
    return ""
