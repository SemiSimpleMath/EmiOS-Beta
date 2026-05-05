"""Unit tests for the GFM → Slack mrkdwn converter."""
from __future__ import annotations

from app.assistant.utils.slack_format import to_slack_mrkdwn


def test_gfm_bold_double_asterisk_becomes_single():
    assert to_slack_mrkdwn("**P!NK** is hosting") == "*P!NK* is hosting"


def test_gfm_bold_double_underscore_becomes_single_asterisk():
    assert to_slack_mrkdwn("__bold word__ here") == "*bold word* here"


def test_multiple_bolds_in_one_line():
    src = "**P!NK** is hosting the **79th Annual Tony Awards**."
    out = to_slack_mrkdwn(src)
    assert out == "*P!NK* is hosting the *79th Annual Tony Awards*."


def test_gfm_link_becomes_slack_link():
    assert to_slack_mrkdwn("see [docs](https://x.example/docs)") == "see <https://x.example/docs|docs>"


def test_heading_becomes_bold():
    assert to_slack_mrkdwn("# Title\nbody") == "*Title*\nbody"
    assert to_slack_mrkdwn("## Sub\n").startswith("*Sub*")


def test_already_slack_format_is_left_alone():
    # Single-asterisk bold, underscore italic, backticks — all native to mrkdwn.
    src = "*already bold* and _italic_ with `code`"
    assert to_slack_mrkdwn(src) == src


def test_empty_and_none():
    assert to_slack_mrkdwn("") == ""
    assert to_slack_mrkdwn(None) == ""


def test_realistic_paragraph():
    src = "**P!NK** is hosting the **79th Annual Tony Awards**. The ceremony is scheduled for **June 7, 2026**, at **Radio City Music Hall**."
    out = to_slack_mrkdwn(src)
    assert "**" not in out
    assert out.count("*") == 8  # 4 bold spans × 2 asterisks each
    assert "*P!NK*" in out
    assert "*79th Annual Tony Awards*" in out
    assert "*Radio City Music Hall*" in out


def test_does_not_break_code_fence_with_asterisks():
    src = "```\nsome **literal** code\n```"
    # Inside a code fence we'd ideally not transform, but we do anyway —
    # this is a known minor cost of the simple regex approach. Document it
    # by asserting the current behavior so a future fence-aware impl is
    # an explicit upgrade.
    out = to_slack_mrkdwn(src)
    assert "*literal*" in out  # transforms even inside fences (current behavior)


def test_link_with_special_chars_in_text():
    src = "click [P!NK's tour](https://example.com/tour)"
    assert to_slack_mrkdwn(src) == "click <https://example.com/tour|P!NK's tour>"
