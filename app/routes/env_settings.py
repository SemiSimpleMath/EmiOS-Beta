"""Central env-var settings UI (`/settings/env`) + write endpoints.

Renders the unified env-var registry (EnvRegistryService) grouped by owner then
feature, with inline value/secret editors and an "add variable" form. Account
entries link to the existing /emi-accounts configure flow. The same entry list is
reusable inline on feature pages via templates/_env_entries.html.
"""
from __future__ import annotations

from typing import Any

from flask import Blueprint, redirect, render_template, request, url_for

from app.assistant.ServiceLocator.service_locator import DI

env_settings_bp = Blueprint("env_settings", __name__)

_OWNER_ORDER = ["user", "assistant"]


def enrich_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach per-entry display state: `account` for kind=account, else `state`."""
    reg = DI.env_registry
    accounts_by_id = {a["id"]: a for a in reg.accounts()}
    out: list[dict[str, Any]] = []
    for e in entries:
        item = dict(e)
        if e.get("kind") == "account":
            item["account"] = accounts_by_id.get(e.get("name"))
        else:
            item["state"] = reg.value_state(e.get("name"))
        out.append(item)
    return out


@env_settings_bp.app_template_global("env_for_feature")
def env_for_feature(feature: str) -> list[dict[str, Any]]:
    """Jinja global: enriched registry entries for a feature tag, so any feature
    page can embed an inline editable section via _env_entries.html."""
    return enrich_entries(DI.env_registry.for_feature(feature))


def _grouped() -> list[dict[str, Any]]:
    reg = DI.env_registry
    by_owner: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for item in enrich_entries(reg.entries()):
        owner = str(item.get("owner") or "user")
        feature = str(item.get("feature") or "general")
        by_owner.setdefault(owner, {}).setdefault(feature, []).append(item)
    owners = _OWNER_ORDER + [o for o in by_owner if o not in _OWNER_ORDER]
    groups: list[dict[str, Any]] = []
    for owner in owners:
        if owner not in by_owner:
            continue
        feats = [{"feature": f, "entries": items} for f, items in sorted(by_owner[owner].items())]
        groups.append({"owner": owner, "features": feats})
    return groups


@env_settings_bp.route("/settings/env", methods=["GET"])
def env_settings_page():
    return render_template("settings_env.html", groups=_grouped(), flash=request.args.get("msg"))


@env_settings_bp.route("/settings/env/set", methods=["POST"])
def env_settings_set():
    name = (request.form.get("name") or "").strip()
    value = request.form.get("value") or ""
    try:
        DI.env_registry.set_value(name, value)
        msg = f"Saved {name}"
    except Exception as e:  # surface the real error to the page
        msg = f"Error: {e}"
    return redirect(url_for("env_settings.env_settings_page", msg=msg))


@env_settings_bp.route("/settings/env/add", methods=["POST"])
def env_settings_add():
    f = request.form
    try:
        added = DI.env_registry.add_user_entry(
            name=f.get("name"),
            owner=f.get("owner") or "user",
            feature=f.get("feature") or "general",
            label=f.get("label") or "",
            hint=f.get("hint") or "",
            secret=bool(f.get("secret")),
        )
        msg = f"Added {added['name']}"
    except Exception as e:
        msg = f"Error: {e}"
    return redirect(url_for("env_settings.env_settings_page", msg=msg))
