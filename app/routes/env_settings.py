"""Central env-var settings UI (`/settings/env`) + write endpoints.

Renders the unified env-var registry (EnvRegistryService) grouped by owner then
feature, with inline value/secret editors and an "add variable" form. Account
entries link to the richer /settings/accounts configure flow. The same entry list is
reusable inline on feature pages via templates/_env_entries.html.
"""
from __future__ import annotations

from typing import Any

from flask import Blueprint, redirect, render_template, request, url_for

from app.assistant.ServiceLocator.service_locator import DI

env_settings_bp = Blueprint("env_settings", __name__)


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


@env_settings_bp.route("/settings/env", methods=["GET"])
def env_settings_page():
    # Source of truth is the real .env file (masked), not the curated registry —
    # registry entries only overlay labels. See EnvRegistryService.env_overview.
    return render_template("settings_env.html", groups=DI.env_registry.env_overview(),
                           flash=request.args.get("msg"))


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
    name = (f.get("name") or "").strip()
    value = f.get("value") or ""
    try:
        if not name:
            raise ValueError("a variable name is required")
        # Record display metadata once (label/feature/owner), then write the value
        # to .env so the var actually appears (source of truth = .env).
        if DI.env_registry.get(name) is None:
            DI.env_registry.add_user_entry(
                name=name,
                owner=f.get("owner") or "user",
                feature=f.get("feature") or "general",
                label=f.get("label") or "",
                hint=f.get("hint") or "",
                secret=True,
            )
        DI.env_registry.set_value(name, value)
        msg = f"Saved {name}"
    except Exception as e:
        msg = f"Error: {e}"
    return redirect(url_for("env_settings.env_settings_page", msg=msg))
