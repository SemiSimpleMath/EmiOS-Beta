"""
Tests for PlaywrightPageStateNode._format_modal_snapshot_card.

Verifies that when a modal is open, the snapshot card is focused on modal
elements only, with background suppressed.
"""

from app.assistant.control_nodes.playwright_page_state_node import PlaywrightPageStateNode


def _make_modal_state(*, bbox=None, focus=None, scroll=None):
    return {
        "modal_open": True,
        "modal_bbox": bbox or {"left": 100, "top": 50, "width": 600, "height": 400},
        "focus_point": focus or {"x": 400, "y": 250},
        "modal_scroll": scroll or {
            "scrollable": True,
            "scrollTop": 120,
            "scrollHeight": 800,
            "clientHeight": 400,
            "maxScrollTop": 400,
            "atTop": False,
            "atBottom": False,
            "progress": 0.3,
        },
    }


def _make_form_state(*, selectable_refs=None, buttons=None, controls=None):
    return {
        "has_form": True,
        "controls": controls or [{"type": "radio"}, {"type": "button"}, {"type": "checkbox"}],
        "selectable_refs": selectable_refs or [
            {"ref": "e42", "role": "radio", "text": "Large Fries"},
            {"ref": "e43", "role": "radio", "text": "Medium Fries"},
        ],
        "buttons": buttons or [
            {"label": "Add to Cart", "disabled": False, "visible": True},
            {"label": "Cancel", "disabled": False, "visible": True},
        ],
        "selected_items": ["radio side: Large Fries"],
        "unselected_items": ["radio side: Medium Fries"],
    }


def _make_snapshot(*, snapshot_id="snap_123", url="https://example.com", title="Test Page"):
    return {
        "snapshot_id": snapshot_id,
        "url": url,
        "title": title,
        "actionable_elements": [
            {"ref": "e1", "role": "link", "text": "Home"},
            {"ref": "e2", "role": "link", "text": "About"},
            {"ref": "e42", "role": "radio", "text": "Large Fries"},
            {"ref": "e43", "role": "radio", "text": "Medium Fries"},
            {"ref": "e99", "role": "link", "text": "Footer Link"},
        ],
    }


class TestModalSnapshotCard:
    def test_contains_modal_focused_header(self):
        result = PlaywrightPageStateNode._format_modal_snapshot_card(
            modal_state=_make_modal_state(),
            form_state=_make_form_state(),
            latest_snapshot=_make_snapshot(),
        )
        assert "MODAL FOCUSED" in result

    def test_background_suppressed(self):
        result = PlaywrightPageStateNode._format_modal_snapshot_card(
            modal_state=_make_modal_state(),
            form_state=_make_form_state(),
            latest_snapshot=_make_snapshot(),
        )
        assert "background_suppressed: true" in result

    def test_modal_open_true(self):
        result = PlaywrightPageStateNode._format_modal_snapshot_card(
            modal_state=_make_modal_state(),
            form_state=_make_form_state(),
            latest_snapshot=_make_snapshot(),
        )
        assert "modal_open: true" in result

    def test_contains_snapshot_id(self):
        result = PlaywrightPageStateNode._format_modal_snapshot_card(
            modal_state=_make_modal_state(),
            form_state=_make_form_state(),
            latest_snapshot=_make_snapshot(snapshot_id="snap_abc"),
        )
        assert "snap_abc" in result

    def test_contains_bbox(self):
        result = PlaywrightPageStateNode._format_modal_snapshot_card(
            modal_state=_make_modal_state(bbox={"left": 100, "top": 50, "width": 600, "height": 400}),
            form_state=_make_form_state(),
            latest_snapshot=_make_snapshot(),
        )
        assert "left=100" in result
        assert "width=600" in result

    def test_contains_focus_point(self):
        result = PlaywrightPageStateNode._format_modal_snapshot_card(
            modal_state=_make_modal_state(focus={"x": 400, "y": 250}),
            form_state=_make_form_state(),
            latest_snapshot=_make_snapshot(),
        )
        assert "(400, 250)" in result

    def test_scrollable_info(self):
        result = PlaywrightPageStateNode._format_modal_snapshot_card(
            modal_state=_make_modal_state(),
            form_state=_make_form_state(),
            latest_snapshot=_make_snapshot(),
        )
        assert "modal_scrollable: true" in result
        assert "modal_scroll_progress: 0.3" in result

    def test_non_scrollable_modal(self):
        scroll = {
            "scrollable": False,
            "scrollTop": 0,
            "scrollHeight": 300,
            "clientHeight": 300,
            "maxScrollTop": 0,
            "atTop": True,
            "atBottom": True,
            "progress": 1.0,
        }
        result = PlaywrightPageStateNode._format_modal_snapshot_card(
            modal_state=_make_modal_state(scroll=scroll),
            form_state=_make_form_state(),
            latest_snapshot=_make_snapshot(),
        )
        assert "modal_scrollable: false" in result
        assert "modal_scroll_progress" not in result

    def test_shows_only_modal_elements(self):
        """Modal refs (e42, e43) should appear; background refs (e1, e2, e99) should NOT."""
        result = PlaywrightPageStateNode._format_modal_snapshot_card(
            modal_state=_make_modal_state(),
            form_state=_make_form_state(),
            latest_snapshot=_make_snapshot(),
        )
        assert "e42" in result
        assert "e43" in result
        # Background elements should be suppressed
        assert "e1 " not in result  # space after to avoid matching e1 inside e100 etc
        assert "e2 " not in result
        assert "e99" not in result

    def test_shows_buttons(self):
        result = PlaywrightPageStateNode._format_modal_snapshot_card(
            modal_state=_make_modal_state(),
            form_state=_make_form_state(),
            latest_snapshot=_make_snapshot(),
        )
        assert "Add to Cart" in result
        assert "Cancel" in result

    def test_shows_control_count(self):
        result = PlaywrightPageStateNode._format_modal_snapshot_card(
            modal_state=_make_modal_state(),
            form_state=_make_form_state(controls=[{"type": "radio"}] * 5),
            latest_snapshot=_make_snapshot(),
        )
        assert "modal_controls: 5" in result

    def test_empty_form_state(self):
        """Should still produce a valid card even with no form controls."""
        result = PlaywrightPageStateNode._format_modal_snapshot_card(
            modal_state=_make_modal_state(),
            form_state={},
            latest_snapshot=_make_snapshot(),
        )
        assert "MODAL FOCUSED" in result
        assert "background_suppressed: true" in result

    def test_preserves_url_and_title(self):
        result = PlaywrightPageStateNode._format_modal_snapshot_card(
            modal_state=_make_modal_state(),
            form_state=_make_form_state(),
            latest_snapshot=_make_snapshot(url="https://doordash.com/menu", title="DoorDash - Menu"),
        )
        assert "https://doordash.com/menu" in result
        assert "DoorDash - Menu" in result
