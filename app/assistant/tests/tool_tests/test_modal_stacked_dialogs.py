"""Stacked dialogs and priced-option refs in modal perception (2026-09-09).

Two bugs found on a DoorDash run hid refs the accessibility tree already had:

1. Both dialog pickers took the FIRST dialog in tree order. With a cart drawer
   (dialog 1) under an item-customization modal (dialog 2), the planner was
   shown the drawer's quantity buttons and never a single option of the modal.
2. web_modal_scan matched refs using its DISPLAY text, which for any label it
   deemed "generic" carried an appended "(context)" suffix — and its generic
   test was a substring check, so every priced option ("... +$1.30") tripped it
   and lost its ref. Matching now uses the raw label (match_text).
"""
from __future__ import annotations

import app.assistant.tests.test_setup  # noqa: F401

from app.assistant.lib.tools.playwright_snapshot_utils import (
    _extract_dialog_subtree,
    summarize_actionable_snapshot,
)
from app.assistant.lib.tools.web_modal_scan.web_modal_scan import _match_refs_by_proximity

_STACKED = """\
- generic [ref=e1]:
  - dialog "Your cart" [ref=e10]:
    - button "Close" [ref=e11]
    - button "add one to cart" [ref=e12]
    - button "NEXT" [ref=e13]
  - dialog "Sausage Egg McMuffin Meal" [ref=e20]:
    - button "Close Sausage Egg McMuffin Meal" [ref=e21]
    - generic [ref=e22] [cursor=pointer]:
      - radio "Small Coke 150 cal" [ref=e23]
    - generic [ref=e24] [cursor=pointer]:
      - radio "Medium Iced Vanilla Coffee 200 cal +$1.30" [ref=e25]
    - button "Update item" [ref=e26]
"""


def test_dialog_subtree_takes_the_topmost_dialog():
    subtree = _extract_dialog_subtree(_STACKED)
    assert subtree is not None
    assert subtree.splitlines()[0].strip().startswith('- dialog "Sausage Egg McMuffin Meal"')
    assert "e25" in subtree
    assert "NEXT" not in subtree


def test_summary_lists_the_modal_options_not_the_drawer():
    summary = summarize_actionable_snapshot(_STACKED, max_elements=50)
    refs = {el["ref"] for el in summary["actionable_elements"]}
    assert {"e23", "e25", "e26"} <= refs
    assert "e13" not in refs


def test_large_modal_keeps_its_buttons_within_the_cap():
    # 100 radios + 2 buttons, cap 40: the save button must survive, and the
    # budget the reserve does not use must go back to radios.
    lines = ['- dialog "Big Mac Meal" [ref=e1]:', '  - button "Close" [ref=e2]']
    for i in range(100):
        lines.append(f'  - radio "Drink {i} +$0.{i:02d}" [ref=r{i}]')
    lines.append('  - button "Add to cart - $16.29" [ref=e3]')
    summary = summarize_actionable_snapshot("\n".join(lines), max_elements=40)
    rows = summary["actionable_elements"]
    refs = {el["ref"] for el in rows}
    assert {"e2", "e3"} <= refs
    assert len(rows) == 40
    assert sum(1 for el in rows if el["role"] == "radio") == 38


def test_priced_option_keeps_its_ref_when_display_text_has_context():
    elements = [
        # What the JS emits for a priced radio it once mislabeled as generic.
        {"role": "radio",
         "text": "Medium Iced Vanilla Coffee 200 cal +$1.30 (Medium Iced Vanilla Coffee)",
         "match_text": "Medium Iced Vanilla Coffee 200 cal +$1.30"},
        {"role": "radio", "text": "Small Coke 150 cal", "match_text": "Small Coke 150 cal"},
    ]
    _match_refs_by_proximity(elements, _STACKED)
    assert elements[0]["ref"] == "e25"
    assert elements[1]["ref"] == "e23"
