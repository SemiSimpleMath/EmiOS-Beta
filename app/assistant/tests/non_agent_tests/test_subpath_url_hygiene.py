"""Links and fetches must survive a mount prefix (2026-09-11).

EmiOS documents EMI_PROXY_SUBPATH as a supported deployment. Under it, Flask
sets WSGI SCRIPT_NAME, url_for() prefixes correctly -- but a LITERAL
`href="/chat_bot"` in a template is plain text that Jinja renders verbatim, and
`fetch("/api/routines")` in JS never passes through url_for at all.

On this host that did not 404 cleanly. The tailnet root proxied to a DIFFERENT
service (the homepage dashboard), so:
  - "back to chat" silently landed on another app
  - every API call returned that app's HTML instead of JSON, so settings pages
    rendered empty tabs and saves reported "Failed to save ..." while the
    server-side endpoint was provably fine

Emi.js monkey-patches window.fetch to auto-prefix, which fixes the JS half --
but only for pages that LOAD Emi.js, which is almost none of them.

T-62 previously concluded this was a single window.location.replace() call site.
That measured one rendered page (/chat_bot, which happens to use url_for) and
generalised. These tests exist so the real scope stays visible.

BASELINE-LOCKED: the debt is large and is not being paid down here. The tests
fail on NEW violations, and on baseline entries that have been fixed but not
removed.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
TEMPLATES = REPO / "app" / "templates"
JS_DIR = REPO / "app" / "static" / "js"

# A literal root-absolute href/action in template SOURCE. url_for(...) and
# {{ request.script_root }} both start with "{", so they never match.
_TPL_ABS = re.compile(r'(?:href|action)\s*=\s*"(/[a-zA-Z][^"]*)"')

# Root-absolute fetch target, any quote style.
_JS_ABS = re.compile(r"""fetch\(\s*[`'"](/[a-zA-Z][^`'"]*)[`'"]""")


def _templates_with_literal_abs_urls() -> dict[str, int]:
    out: dict[str, int] = {}
    for f in sorted(TEMPLATES.glob("*.html")):
        hits = _TPL_ABS.findall(f.read_text(encoding="utf-8", errors="ignore"))
        if hits:
            out[f.name] = len(hits)
    return out


def _js_with_abs_fetch() -> dict[str, int]:
    out: dict[str, int] = {}
    for f in sorted(JS_DIR.rglob("*.js")):
        hits = _JS_ABS.findall(f.read_text(encoding="utf-8", errors="ignore"))
        if hits:
            out[f.relative_to(JS_DIR).as_posix()] = len(hits)
    return out


# Counts as of 2026-09-11 (HEAD 7b665d30). Lower them as they are fixed.
_TPL_BASELINE_TOTAL = 138  # href= AND action=; an earlier pass counted only href= and said 129
# 100 until 2026-09-16, when emi_code.js was rewritten as an xterm.js terminal
# and lost one root-absolute fetch. Tightened in the same commit, per the rule
# this file states: pay the debt down, never let the number drift up.
_JS_BASELINE_TOTAL = 99


def test_no_new_literal_absolute_urls_in_templates():
    total = sum(_templates_with_literal_abs_urls().values())
    assert total <= _TPL_BASELINE_TOTAL, (
        f"{total} literal root-absolute href/action in templates, baseline is "
        f"{_TPL_BASELINE_TOTAL}. A new one was added. Use "
        '{{ request.script_root }}/path or url_for(...) so the link survives '
        "EMI_PROXY_SUBPATH."
    )


def test_no_new_absolute_fetches_in_js():
    total = sum(_js_with_abs_fetch().values())
    assert total <= _JS_BASELINE_TOTAL, (
        f"{total} root-absolute fetch() targets in JS, baseline is "
        f"{_JS_BASELINE_TOTAL}. A new one was added. Prefix with "
        "window.SCRIPT_NAME (see api_keys_settings.js) or load Emi.js, which "
        "wraps window.fetch."
    )


def test_baselines_are_not_stale():
    """If the debt was paid down, tighten the baseline in the same commit."""
    tpl = sum(_templates_with_literal_abs_urls().values())
    js = sum(_js_with_abs_fetch().values())
    assert tpl == _TPL_BASELINE_TOTAL, (
        f"templates now {tpl} vs baseline {_TPL_BASELINE_TOTAL} — update "
        "_TPL_BASELINE_TOTAL so the guard keeps biting."
    )
    assert js == _JS_BASELINE_TOTAL, (
        f"js now {js} vs baseline {_JS_BASELINE_TOTAL} — update _JS_BASELINE_TOTAL."
    )


def test_the_reference_fix_still_demonstrates_the_pattern():
    """api_keys_settings is the worked example; keep it correct so it can be copied."""
    tpl = (TEMPLATES / "api_keys_settings.html").read_text(encoding="utf-8")
    assert "{{ request.script_root }}/chat_bot" in tpl
    assert "window.SCRIPT_NAME = {{ request.script_root | tojson }}" in tpl
    js = (JS_DIR / "api_keys_settings.js").read_text(encoding="utf-8")
    assert "window.SCRIPT_NAME" in js


def test_emi_js_still_wraps_fetch():
    """The auto-prefixing wrapper is load-bearing for pages that do load it."""
    emi = (JS_DIR / "Emi.js").read_text(encoding="utf-8")
    assert "window.fetch" in emi and "SCRIPT_NAME" in emi
