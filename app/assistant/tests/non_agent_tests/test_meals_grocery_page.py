"""The /meals grocery list editor (2026-11-14): its form posts and its rendering, on a scratch database."""
from __future__ import annotations

from pathlib import Path

import pytest
from flask import Flask, render_template

from app.assistant.subconscious import grocery_inventory, grocery_lists
from belief_engine.intake.store import sqlite_file

WEEK = "2026-11-16"
REPO = Path(__file__).resolve().parents[4]


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    connect = sqlite_file(tmp_path / "grocery.db")
    monkeypatch.setattr(grocery_lists, "_connect", connect)
    grocery_lists.save_planned(WEEK, items=[
        {"name": "milk", "quantity": "1 gallon", "category": "Dairy & Eggs", "reason": "staple"},
        {"name": "salmon fillet", "quantity": "1 lb", "category": "Meat & Seafood", "reason": "Monday's salmon"},
    ], skipped_staples=[{"name": "cookies", "reason": "bought three weeks running"}], connect=connect)
    return connect


@pytest.fixture
def client():
    from app.routes.meals import meals_bp
    app = Flask(__name__, template_folder=str(REPO / "app" / "templates"), static_folder=str(REPO / "app" / "static"))
    app.register_blueprint(meals_bp)
    return app.test_client()


def _ids():
    return {i["name"]: i["id"] for i in grocery_lists.get(WEEK)["items"]}


def test_edit_posts_change_the_list_and_return_to_the_week(scratch, client):
    resp = client.post("/meals/grocery/remove", data={"week_start": WEEK, "item_id": _ids()["milk"]})
    assert resp.status_code == 302 and resp.headers["Location"].endswith(f"/meals?week={WEEK}#grocery")
    client.post("/meals/grocery/quantity",
                data={"week_start": WEEK, "item_id": _ids()["salmon fillet"], "quantity": "2 lb"})
    client.post("/meals/grocery/add", data={"week_start": WEEK, "name": "cookies", "reason": "added back by the owner"})
    gl = grocery_lists.get(WEEK)
    assert [(i["name"], i["quantity"]) for i in gl["items"]] == [("salmon fillet", "2 lb"), ("cookies", "")]
    assert [e["change"] for e in gl["edits"]] == ["removed", "changed", "added"]


def test_approve_takes_the_local_order_time(scratch, client):
    from app.assistant.utils.time_utils import utc_to_local
    client.post("/meals/grocery/approve", data={"week_start": WEEK, "order_at": "2026-11-16T10:30"})
    gl = grocery_lists.get(WEEK)
    assert gl["status"] == "approved"
    assert utc_to_local(gl["order_at"]).strftime("%Y-%m-%d %H:%M") == "2026-11-16 10:30"


def test_a_refused_change_is_a_400_with_the_reason(scratch, client):
    client.post("/meals/grocery/approve", data={"week_start": WEEK, "order_at": "2026-11-16T10:30"})
    resp = client.post("/meals/grocery/remove", data={"week_start": WEEK, "item_id": _ids()["milk"]})
    assert resp.status_code == 400 and b"approved" in resp.data


def test_unknown_action_and_missing_week(scratch, client):
    assert client.post("/meals/grocery/explode", data={"week_start": WEEK}).status_code == 404
    assert client.post("/meals/grocery/skip", data={}).status_code == 400


def test_the_editor_renders_rows_have_it_and_the_default_order_time(scratch, client, monkeypatch):
    from app.assistant.subconscious import meal_page_service
    from app.assistant.utils import resource_files
    inventory = {"items": [{"name": "milk", "acquired_at_utc": "2026-11-10T00:00:00+00:00", "consumed_at_utc": None}],
                 "history": []}
    monkeypatch.setattr(grocery_inventory, "load_inventory", lambda: inventory)
    monkeypatch.setattr(resource_files, "read", lambda rel: {"default_order": {"weekday": "Monday", "time": "10:30"}})
    grocery = meal_page_service.build_grocery_view(WEEK)
    assert grocery["order_at_input"] == "2026-11-16T10:30"
    assert [(i["name"], i["have_it"]) for i in grocery["rows"]] == [("milk", True), ("salmon fillet", False)]

    nav = {"viewing_week_iso": WEEK, "prev_week_iso": "2026-11-09", "next_week_iso": "2026-11-23",
           "prev_has_plan": False, "next_has_plan": False, "is_current_week": True, "today_week_iso": WEEK}
    plan = {"pod_id": "pod1", "week_start_date": WEEK, "slots": [], "produced_at_utc": "", "theme": ""}
    with client.application.test_request_context():
        html = render_template("meals.html", assistant_name="Assistant", static_v="1", viewing_week=WEEK, nav=nav,
                               plan=plan, plan_comments_by_scope={}, plan_comments_section=[], grocery=grocery,
                               shopping_text="", ad_hoc_count=0)
    assert 'value="2026-11-16T10:30"' in html
    assert "have it" in html and "bought three weeks running" in html and "Add back" in html
    assert 'action="/meals/grocery/approve"' in html
