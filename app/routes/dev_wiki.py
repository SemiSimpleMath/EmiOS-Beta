"""Local renderer for the EmiOS dev wiki (the docs/ directory).

Mirrors how /wiki/ serves EmiPedia from the user's vault: same markdown
extensions, same in-app reading experience, no GitHub round-trip.
Reads files from the on-disk ``docs/`` tree at request time so edits
show up on refresh.

Routes:
  GET  /dev-wiki/               → renders docs/INDEX.md
  GET  /dev-wiki/<path:doc>     → renders docs/<doc> (must end in .md)

Security: requested paths are resolved relative to the docs root and
rejected if they escape it. Personal-scratch subdirs (audits/, drafts/)
are excluded since they're gitignored and may contain in-flight notes.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import markdown
from flask import Blueprint, abort, redirect, render_template, url_for

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

dev_wiki_bp = Blueprint("dev_wiki", __name__)

DOCS_ROOT = Path(__file__).resolve().parent.parent.parent / "docs"
INDEX_DOC = "INDEX.md"
EXCLUDED_SUBDIRS = {"audits", "drafts", "archived"}
GITHUB_DOCS_BASE = "https://github.com/SemiSimpleMath/EmiOS-Beta/blob/release-v0.1/docs/"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_markdown(md_text: str) -> str:
    """Markdown → HTML using the same extensions as wiki_viewer's EmiPedia
    renderer, so behavior matches what users see for personal pages."""
    md = markdown.Markdown(
        extensions=["extra", "toc", "sane_lists", "tables", "fenced_code"],
        extension_configs={
            "toc": {
                "permalink": False,
                "slugify": lambda value, sep: re.sub(r"[^\w]+", sep, value.lower()).strip(sep),
            },
        },
    )
    return md.convert(md_text)


_MD_LINK_RE = re.compile(r'<a\s+([^>]*?)href="([^"#]+\.md)(#[^"]*)?"', re.IGNORECASE)


def _rewrite_internal_md_links(html: str, current_doc_path: Path) -> str:
    """Rewrite ``href="some/other.md"`` → ``href="/dev-wiki/some/other.md"``.

    Treats relative paths as relative to the *current doc's* directory so
    links like ``../architecture/11_WIKI_GENERATOR.md`` from
    ``welcome/MENTAL_MODEL.md`` resolve to the right /dev-wiki/ URL.
    Absolute http(s) and anchor-only links are left alone (the
    regex's ``[^"#]+\\.md`` requirement handles the latter implicitly).
    """
    current_dir = current_doc_path.parent

    def _sub(match: re.Match) -> str:
        attrs_before = match.group(1)
        target = match.group(2)
        anchor = match.group(3) or ""
        if target.startswith(("http://", "https://", "/")):
            return match.group(0)
        try:
            resolved = (current_dir / target).resolve()
            rel = resolved.relative_to(DOCS_ROOT.resolve())
        except (ValueError, OSError):
            return match.group(0)
        url = url_for("dev_wiki.dev_wiki_view", doc=rel.as_posix())
        return f'<a {attrs_before}href="{url}{anchor}"'

    return _MD_LINK_RE.sub(_sub, html)


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def _safe_resolve(rel_path: str) -> Optional[Path]:
    """Resolve ``rel_path`` (relative to DOCS_ROOT) and refuse anything that
    escapes DOCS_ROOT, isn't a .md file, doesn't exist, or lives under an
    excluded subdir."""
    if not rel_path or not rel_path.endswith(".md"):
        return None
    try:
        candidate = (DOCS_ROOT / rel_path).resolve()
        candidate.relative_to(DOCS_ROOT.resolve())
    except (ValueError, OSError):
        return None
    if not candidate.exists() or not candidate.is_file():
        return None
    parts = candidate.relative_to(DOCS_ROOT.resolve()).parts
    if parts and parts[0] in EXCLUDED_SUBDIRS:
        return None
    return candidate


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@dev_wiki_bp.route("/dev-wiki/", methods=["GET"])
def dev_wiki_root():
    return redirect(url_for("dev_wiki.dev_wiki_view", doc=INDEX_DOC))


@dev_wiki_bp.route("/dev-wiki/<path:doc>", methods=["GET"])
def dev_wiki_view(doc: str):
    path = _safe_resolve(doc)
    if path is None:
        abort(404)

    md_text = path.read_text(encoding="utf-8")
    html = _render_markdown(md_text)
    html = _rewrite_internal_md_links(html, path)

    rel = path.relative_to(DOCS_ROOT.resolve()).as_posix()
    is_index = rel == INDEX_DOC
    breadcrumb = []
    if not is_index:
        breadcrumb.append({
            "label": "Index",
            "href": url_for("dev_wiki.dev_wiki_view", doc=INDEX_DOC),
        })
        crumbs = rel.split("/")
        for i, crumb in enumerate(crumbs[:-1]):
            breadcrumb.append({"label": crumb, "href": None})
        breadcrumb.append({"label": crumbs[-1], "href": None})

    return render_template(
        "dev_wiki.html",
        body_html=html,
        rel_path=rel,
        is_index=is_index,
        breadcrumb=breadcrumb,
        index_url=url_for("dev_wiki.dev_wiki_view", doc=INDEX_DOC),
        github_url=GITHUB_DOCS_BASE + rel,
    )
