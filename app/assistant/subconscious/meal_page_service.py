"""Data + actions for the /meals page.

Three responsibilities:
1. Load the latest weekly plan + this week's shopping context for rendering
2. Send the weekly meal plan email to a recipient
3. Mint a delivery.email audit pod recording each send

The route layer (app/routes/meals.py) is a thin wrapper around this.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
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


def build_page_view_model() -> Dict[str, Any]:
    """Everything the /meals template needs to render: the latest plan
    pod's structured slots, the consolidated shopping text, the target
    recipient, AND any feedback comments attached to the plan pod (so
    the user can see their past comments inline)."""
    from app.assistant.subconscious.feedback_service import fetch_comments_for

    plan_pod = load_latest_weekly_plan_pod()
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
            "anchors": list(meta.get("anchor_meals") or []),
            "slots": list(meta.get("slots") or []),
            "produced_at_utc": meta.get("produced_at_utc") or "",
            "theme": _extract_theme(plan_pod.body or ""),
            "addressed_concern_ids": list(meta.get("addressed_concern_ids") or []),
        }
        plan_comments = fetch_comments_for(plan_pod.pod_id, limit=20)

    # Group plan comments by target_scope (date) so per-day comment lists
    # render alongside each day in the grid. Comments with scope=None
    # appear at the section-level.
    plan_comments_by_scope: Dict[str, list] = {}
    plan_comments_section: list = []
    for c in plan_comments:
        scope = c.get("target_scope")
        if scope:
            plan_comments_by_scope.setdefault(str(scope), []).append(c)
        else:
            plan_comments_section.append(c)

    return {
        "plan": plan_view,
        "plan_comments_by_scope": plan_comments_by_scope,
        "plan_comments_section": plan_comments_section,
        "shopping_text": shopping_text,
        "weekly_doc_present": bool(doc_body),
        "ad_hoc_count": len(consolidate_intention_shopping(ad_hoc_pods)),
        "recipient_email": KATY_EMAIL,
        "recipient_name": KATY_DISPLAY_NAME,
    }


def send_meal_plan_email(
    *,
    to: Optional[str] = None,
    recipient_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Send the latest weekly meal plan email to `to` (defaults to Katy).
    Mints a delivery.email audit pod on success.

    Returns a dict with status + details for the route layer to JSON-ify.
    """
    recipient = (to or KATY_EMAIL).strip()
    name = (recipient_name or KATY_DISPLAY_NAME).strip() or recipient

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
