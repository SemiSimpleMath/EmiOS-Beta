"""Unit tests for ``send_email.pod_render``.

Pure tests — no DI, no DB. Exercises the rendering rules:
  - email pod → .eml with From / Subject / Date headers
  - non-email body-only pod → .txt with optional Subject line
  - filename slugification edge cases (special chars, length, empty)
"""
from __future__ import annotations

import os
import tempfile
from types import SimpleNamespace

import pytest

from app.assistant.lib.tools.send_email.pod_render import (
    _filename_for,
    render_pod_as_inline_block,
    render_pod_to_attachment,
)


def _make_pod(**kwargs):
    """Lightweight stand-in for the Pod pydantic model.

    pod_render only reads ``kind``, ``pod_id``, ``one_liner``, ``body``,
    ``metadata`` via getattr — a SimpleNamespace is enough.
    """
    defaults = {
        "kind": "email",
        "pod_id": "datapod:email:50e9d6dc5d22321824802fb2",
        "one_liner": "",
        "body": "",
        "metadata": {},
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_email_pod_renders_to_eml_with_headers(tmp_path):
    pod = _make_pod(
        kind="email",
        pod_id="datapod:email:50e9d6dc5d22321824802fb2",
        one_liner="LinkedIn Job Alerts: AI Architect at Kaygen",
        body="Your job alert for machine learning specialist.\nNew jobs match your alert.",
        metadata={
            "subject": "LinkedIn Job Alerts: AI Architect at Kaygen, Inc.: up to $115/hour",
            "sender_display": "LinkedIn Job Alerts",
            "sender_email": "jobalerts-noreply@linkedin.com",
            "occurred_at_utc": "2026-05-05T18:05:38",
        },
    )

    path = render_pod_to_attachment(pod, str(tmp_path))

    assert path.endswith(".eml")
    assert os.path.isfile(path)
    text = open(path, encoding="utf-8").read()
    assert 'From: "LinkedIn Job Alerts" <jobalerts-noreply@linkedin.com>' in text
    assert "Subject: LinkedIn Job Alerts: AI Architect at Kaygen, Inc.: up to $115/hour" in text
    assert "Date: 2026-05-05T18:05:38" in text
    assert "Content-Type: text/plain; charset=utf-8" in text
    # Body separated by blank line.
    assert text.endswith(
        "Your job alert for machine learning specialist.\nNew jobs match your alert."
    )


def test_email_pod_handles_missing_metadata(tmp_path):
    pod = _make_pod(
        kind="email",
        pod_id="datapod:email:abcdef0123456789",
        body="Hello.",
        metadata={},
    )
    path = render_pod_to_attachment(pod, str(tmp_path))
    text = open(path, encoding="utf-8").read()
    assert "From: (unknown sender)" in text
    # No Subject / Date headers when missing — but Content-Type always present.
    assert "Subject:" not in text
    assert "Date:" not in text
    assert "Content-Type: text/plain; charset=utf-8" in text
    assert text.endswith("Hello.")


def test_text_pod_renders_to_txt(tmp_path):
    pod = _make_pod(
        kind="chat_cluster",
        pod_id="datapod:chat_cluster:1234567890ab",
        one_liner="Discussion about dinner plans",
        body="A: where should we eat?\nB: pizza place",
        metadata={},
    )
    path = render_pod_to_attachment(pod, str(tmp_path))
    assert path.endswith(".txt")
    text = open(path, encoding="utf-8").read()
    assert text.startswith("Subject: Discussion about dinner plans\n\n")
    assert text.endswith("A: where should we eat?\nB: pizza place")


def test_text_pod_no_one_liner_omits_subject(tmp_path):
    pod = _make_pod(
        kind="text",
        pod_id="datapod:text:0000111122223333",
        one_liner="",
        body="just the body",
        metadata={},
    )
    path = render_pod_to_attachment(pod, str(tmp_path))
    text = open(path, encoding="utf-8").read()
    assert text == "just the body"


# ---------- filename slugification ----------------------------------


def test_filename_slugifies_subject_with_specials():
    pod = _make_pod(pod_id="datapod:email:abcdef0150e9d6dc")
    name = _filename_for(pod, "LinkedIn Job Alerts: AI Architect at Kaygen, Inc.", ".eml")
    # Lowercase, special chars → "-", suffix is last 8 of pod_id.
    assert name == "linkedin-job-alerts-ai-architect-at-kaygen-inc-50e9d6dc.eml"


def test_filename_caps_long_label():
    pod = _make_pod(pod_id="datapod:email:abcdef0150e9d6dc")
    long = "A" * 200
    name = _filename_for(pod, long, ".txt")
    # Slug capped at 80, then -<suffix>.<ext>; underscores/dashes trimmed.
    assert len(name) <= 80 + 1 + 8 + 4
    assert name.endswith("-50e9d6dc.txt")


def test_filename_empty_label_falls_back_to_pod_prefix():
    pod = _make_pod(pod_id="datapod:email:abcdef0150e9d6dc")
    name = _filename_for(pod, "", ".eml")
    assert name == "pod_50e9d6dc.eml"


def test_filename_handles_short_pod_id():
    pod = _make_pod(pod_id="abc")
    name = _filename_for(pod, "hello world", ".txt")
    assert name == "hello-world-abc.txt"


def test_filename_collapses_repeated_dashes():
    pod = _make_pod(pod_id="datapod:email:abcdef0150e9d6dc")
    name = _filename_for(pod, "a   b -- c !!! d", ".txt")
    # Repeated whitespace + special chars → single dashes.
    assert name == "a-b-c-d-50e9d6dc.txt"


# ---------- inline-block rendering ---------------------------------


def test_email_pod_inline_block_uses_forwarded_message_format():
    pod = _make_pod(
        kind="email",
        pod_id="datapod:email:50e9d6dc5d22321824802fb2",
        body="Your job alert for machine learning specialist.",
        metadata={
            "subject": "AI Architect at Kaygen",
            "sender_display": "LinkedIn Job Alerts",
            "sender_email": "jobalerts-noreply@linkedin.com",
            "occurred_at_utc": "2026-05-05T18:05:38",
        },
    )
    block = render_pod_as_inline_block(pod)
    # Standard Gmail / Outlook recognized header.
    assert block.startswith("----- Forwarded message -----\n")
    assert 'From: "LinkedIn Job Alerts" <jobalerts-noreply@linkedin.com>' in block
    assert "Subject: AI Architect at Kaygen" in block
    assert "Date: 2026-05-05T18:05:38" in block
    # Body separated by a blank line and ends with body content.
    assert block.endswith(
        "\n\nYour job alert for machine learning specialist."
    )


def test_email_pod_inline_block_omits_missing_headers():
    pod = _make_pod(
        kind="email",
        pod_id="datapod:email:abcdef0123456789",
        body="hi",
        metadata={"sender_email": "x@y.com"},
    )
    block = render_pod_as_inline_block(pod)
    assert "From: x@y.com" in block
    assert "Subject:" not in block
    assert "Date:" not in block


def test_text_pod_inline_block_uses_dash_separator():
    pod = _make_pod(
        kind="chat_cluster",
        pod_id="datapod:chat_cluster:1234567890ab",
        one_liner="Discussion about dinner",
        body="A: pizza?\nB: yes",
        metadata={},
    )
    block = render_pod_as_inline_block(pod)
    assert block == "---\nDiscussion about dinner\n\nA: pizza?\nB: yes"


def test_text_pod_inline_block_no_one_liner_just_body():
    pod = _make_pod(
        kind="text",
        pod_id="datapod:text:0000111122223333",
        one_liner="",
        body="just the body",
        metadata={},
    )
    block = render_pod_as_inline_block(pod)
    assert block == "---\njust the body"
