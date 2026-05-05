"""Render body-only pods to attachable files.

Email pods, transcripts, notes etc. don't have a backing file on disk —
their content lives in ``pod.body``. When such a pod is passed to
``send_email`` as an attachment, this module renders it to a tempfile so
the gmail layer can attach it. The agent never reads the body — render
happens at tool execution time, outside any LLM context. This is how
personal_admin can forward private content while staying in courier mode.

File format choice is kind-driven:
  - ``email`` pods → ``.eml`` (preserves email-ness; mail clients render
    From / Subject / Date as native email)
  - everything else → ``.txt`` with an optional ``Subject:`` header line

Filename: slugified subject/one_liner + last 8 chars of pod_id + ext.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any


_SLUG_RE = re.compile(r"[^a-z0-9._-]+")
_DASH_RE = re.compile(r"-+")
_MAX_SLUG = 80


def render_pod_to_attachment(pod: Any, tempdir: str) -> str:
    """Render one body-only pod to an attachable file in ``tempdir``.

    Returns the absolute path of the rendered file. Caller is responsible
    for cleaning up ``tempdir`` after the email is sent.
    """
    if str(getattr(pod, "kind", "")).strip() == "email":
        return _render_email_eml(pod, tempdir)
    return _render_text_txt(pod, tempdir)


def _render_email_eml(pod: Any, tempdir: str) -> str:
    md = getattr(pod, "metadata", None) or {}
    subject = str(md.get("subject") or "").strip()
    sender_display = str(md.get("sender_display") or "").strip()
    sender_email = str(md.get("sender_email") or "").strip()
    occurred_at = str(md.get("occurred_at_utc") or "").strip()

    if sender_display and sender_email:
        from_line = f'"{sender_display}" <{sender_email}>'
    elif sender_email:
        from_line = sender_email
    elif sender_display:
        from_line = sender_display
    else:
        from_line = "(unknown sender)"

    header_lines = [f"From: {from_line}"]
    if subject:
        header_lines.append(f"Subject: {subject}")
    if occurred_at:
        header_lines.append(f"Date: {occurred_at}")
    header_lines.append("Content-Type: text/plain; charset=utf-8")
    header_lines.append("MIME-Version: 1.0")

    body = str(getattr(pod, "body", "") or "")
    text = "\n".join(header_lines) + "\n\n" + body

    label = subject or str(getattr(pod, "one_liner", "") or "")
    filename = _filename_for(pod, label, ".eml")
    path = os.path.join(tempdir, filename)
    Path(path).write_text(text, encoding="utf-8")
    return path


def _render_text_txt(pod: Any, tempdir: str) -> str:
    one_liner = str(getattr(pod, "one_liner", "") or "").strip()
    body = str(getattr(pod, "body", "") or "")

    if one_liner:
        text = f"Subject: {one_liner}\n\n{body}"
    else:
        text = body

    filename = _filename_for(pod, one_liner, ".txt")
    path = os.path.join(tempdir, filename)
    Path(path).write_text(text, encoding="utf-8")
    return path


def render_pod_as_inline_block(pod: Any) -> str:
    """Render a pod body as a block to paste inline into an email body.

    Used by ``send_email`` when the planner passes ``inline_pod_ids`` —
    the tool fetches each pod's body and appends a delimited block to the
    outgoing email body. Same privacy property as ``render_pod_to_attachment``:
    the agent never reads the body, the read happens at tool execution.

    Email pods get the standard ``----- Forwarded message -----`` block
    that Gmail / Outlook recognize. Other body-only kinds get a simple
    ``---`` separator with the one-liner as a header.
    """
    if str(getattr(pod, "kind", "")).strip() == "email":
        return _email_pod_inline_block(pod)
    return _text_pod_inline_block(pod)


def _email_pod_inline_block(pod: Any) -> str:
    md = getattr(pod, "metadata", None) or {}
    subject = str(md.get("subject") or "").strip()
    sender_display = str(md.get("sender_display") or "").strip()
    sender_email = str(md.get("sender_email") or "").strip()
    occurred_at = str(md.get("occurred_at_utc") or "").strip()

    if sender_display and sender_email:
        from_line = f'"{sender_display}" <{sender_email}>'
    elif sender_email:
        from_line = sender_email
    elif sender_display:
        from_line = sender_display
    else:
        from_line = "(unknown sender)"

    lines = ["----- Forwarded message -----", f"From: {from_line}"]
    if subject:
        lines.append(f"Subject: {subject}")
    if occurred_at:
        lines.append(f"Date: {occurred_at}")
    lines.append("")  # blank between headers and body
    lines.append(str(getattr(pod, "body", "") or ""))
    return "\n".join(lines)


def _text_pod_inline_block(pod: Any) -> str:
    one_liner = str(getattr(pod, "one_liner", "") or "").strip()
    body = str(getattr(pod, "body", "") or "")
    if one_liner:
        return f"---\n{one_liner}\n\n{body}"
    return f"---\n{body}"


def _filename_for(pod: Any, label: str, ext: str) -> str:
    """Slugified label + last 8 chars of pod_id + extension.

    >>> _filename_for(pod, "LinkedIn Job Alerts: AI Architect", ".eml")
    'linkedin-job-alerts-ai-architect-50e9d6dc.eml'
    """
    slug = (label or "").lower()
    slug = _SLUG_RE.sub("-", slug)
    slug = _DASH_RE.sub("-", slug).strip("-_.")
    if len(slug) > _MAX_SLUG:
        slug = slug[:_MAX_SLUG].rstrip("-_.")

    pod_id = str(getattr(pod, "pod_id", "") or "")
    suffix = pod_id[-8:] if len(pod_id) >= 8 else (pod_id or "anon")

    if not slug:
        return f"pod_{suffix}{ext}"
    return f"{slug}-{suffix}{ext}"
