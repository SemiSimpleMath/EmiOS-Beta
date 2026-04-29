"""
Wiki Viewer — Wikipedia/Farzapedia-style renderer for the personal wiki.

Reads markdown from <vault_root>/prose/<Entity>.md where <vault_root> is
~/<AssistantName>Wiki by default (override via EMI_WIKI_DIR env var). Source of
truth stays as markdown — Obsidian-compatible).

Routes:
  GET  /wiki/                 — index (all articles, categories)
  GET  /wiki/random           — random article
  GET  /wiki/search?q=...     — search article bodies
  GET  /wiki/<entity>         — article page
"""
from __future__ import annotations

import random
import re
import os
from pathlib import Path
from typing import Optional

import frontmatter
import markdown
from flask import Blueprint, abort, jsonify, redirect, render_template, request, url_for

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

wiki_viewer_bp = Blueprint("wiki_viewer", __name__)



def _get_assistant_name() -> str:
    """Read the user-configured assistant name (e.g. "Floppy") from
    resources/assistant/assistant_core.json. Falls back to "Emi" if the
    file is missing or unreadable."""
    try:
        from app.assistant.utils.path_utils import get_repo_root
        import json as _json
        path = get_repo_root() / "resources" / "assistant" / "assistant_core.json"
        if not path.exists():
            return "Emi"
        with open(path, "r", encoding="utf-8") as f:
            data = _json.load(f)
        if isinstance(data, dict):
            n = str(data.get("name") or data.get("assistant_name") or "").strip()
            if n:
                return n
    except Exception:
        pass
    return "Emi"


@wiki_viewer_bp.context_processor
def _inject_assistant_name():
    return {"assistant_name": _get_assistant_name()}


# Vault location — source of truth stays as markdown files.
def _wiki_vault_root() -> Path:
    """Per-assistant vault root, e.g. ~/FloppyWiki for assistant 'Floppy'.
    Override with EMI_WIKI_DIR env var (treated as the vault root, not the
    prose dir). Read fresh each call so a renamed assistant is picked up
    without a restart."""
    override = os.environ.get("EMI_WIKI_DIR")
    if override:
        return Path(override)
    return Path.home() / f"{_get_assistant_name()}Wiki"


def _wiki_prose_dir() -> Path:
    """Per-assistant prose dir under the vault root."""
    return _wiki_vault_root() / "prose"

# Optional provenance sidecar: <Entity>.provenance.json with
# {"paragraphs": [{"index": N, "sources": [node_id, ...]}, ...]}
PROVENANCE_SUFFIX = ".provenance.json"


def _slugify(name: str) -> str:
    # Keep mostly-original — our filenames are "Katy.md", "Jukka.md" etc.
    return name.strip()


def _find_article(entity: str) -> Optional[Path]:
    """Case-insensitive filename match."""
    if not _wiki_prose_dir().exists():
        return None
    target = entity.lower().strip()
    for p in _wiki_prose_dir().glob("*.md"):
        if p.stem.lower() == target:
            return p
    return None


def _wikilink_sub(html: str) -> str:
    """Turn [[Name]] and [[Name|display]] wikilinks into /wiki/Name anchors."""
    def repl(m: re.Match) -> str:
        inner = m.group(1)
        if "|" in inner:
            target, display = inner.split("|", 1)
        else:
            target, display = inner, inner
        target = target.strip()
        display = display.strip()
        return f'<a href="/wiki/{target}" class="wikilink">{display}</a>'

    return re.sub(r"\[\[([^\[\]]+?)\]\]", repl, html)


_NODE_MARKER_RE = re.compile(
    r"\{node:([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\}"
)


def _node_marker_sub(html: str) -> str:
    """Convert {node:<uuid>} markers into small clickable badges that link
    to the KG node viewer. Multiple markers at a paragraph end get grouped
    into a single superscript badge group."""
    def repl(m: re.Match) -> str:
        node_id = m.group(1)
        short = node_id[:4]
        return (
            f'<a class="wp-src-badge" href="/kg/node/{node_id}" '
            f'target="_blank" title="KG node {node_id}">{short}</a>'
        )
    return _NODE_MARKER_RE.sub(repl, html)


# Parses a line from the References section like:
#   <li>1. <a href="/kg/node/abc...">Label</a> <em>(State)</em> — <code>abc...</code></li>
# or, pre-markdown:
#   1. [Label](/kg/node/abc...) *(State)* — `abc...`
_REF_SOURCE_RE = re.compile(
    r"^(\d+)\.\s*\[[^\]]+\]\(/kg/node/([0-9a-fA-F-]{8,})\)",
    re.MULTILINE,
)


def _extract_ref_map(md_text: str) -> dict[int, str]:
    """Parse the `## References` section of the markdown to map [N] → node_id."""
    # Only scan after the ## References heading so inline text won't confuse us.
    marker = re.search(r"^##\s+References\s*$", md_text, re.MULTILINE)
    if not marker:
        return {}
    tail = md_text[marker.end():]
    mapping: dict[int, str] = {}
    for m in _REF_SOURCE_RE.finditer(tail):
        try:
            mapping[int(m.group(1))] = m.group(2)
        except (TypeError, ValueError):
            continue
    return mapping


def _inline_ref_sub(html: str, ref_map: dict[int, str]) -> str:
    """Wrap every `[N]` token in the rendered HTML with a link to the
    corresponding KG node, skipping tokens that are already inside an anchor
    (e.g. the numbered entries in the References list itself)."""
    if not ref_map:
        return html

    # Match [N] NOT already inside an <a> tag. We do this by splitting on
    # anchor boundaries and only rewriting in the segments that are pure prose.
    parts = re.split(r"(<a\b[^>]*>.*?</a>)", html, flags=re.DOTALL | re.IGNORECASE)
    token_re = re.compile(r"\[(\d+)\]")

    def repl(m: re.Match) -> str:
        n = int(m.group(1))
        nid = ref_map.get(n)
        if not nid:
            return m.group(0)
        return (
            f'<a class="wp-inline-ref" href="/kg/node/{nid}" '
            f'target="_blank" title="KG node {nid}">[{n}]</a>'
        )

    rewritten: list[str] = []
    for i, part in enumerate(parts):
        if i % 2 == 1:  # the <a>...</a> segments — leave alone
            rewritten.append(part)
        else:
            rewritten.append(token_re.sub(repl, part))
    return "".join(rewritten)


def _extract_toc_and_sections(md_text: str) -> tuple[list[dict], str]:
    """Extract H2 sections → TOC; return TOC entries + the markdown unchanged
    (markdown's TOC extension will inject anchors on its own)."""
    toc = []
    for m in re.finditer(r"^##\s+(.+?)$", md_text, flags=re.MULTILINE):
        heading = m.group(1).strip()
        # Plain ASCII id — lowercase, non-word→hyphen
        slug = re.sub(r"[^\w]+", "-", heading.lower()).strip("-")
        toc.append({"level": 2, "heading": heading, "slug": slug})
    return toc, md_text


_LEADING_H1_RE = re.compile(r"\A\s*#[^\S\n]+[^\n]*\n+", re.MULTILINE)


def _strip_leading_h1(md_text: str) -> str:
    """Strip a leading ``# Title`` line from the markdown. The wiki template
    renders its own ``<h1 class="wp-title">`` from the article's filename, so
    keeping the markdown's H1 produces a duplicate title on the page."""
    return _LEADING_H1_RE.sub("", md_text, count=1)


def _render_markdown_to_html(md_text: str) -> str:
    """Convert markdown to HTML, expand wikilinks, add heading anchors."""
    md = markdown.Markdown(
        extensions=["extra", "toc", "sane_lists", "tables"],
        extension_configs={"toc": {"permalink": False, "slugify": lambda value, sep: re.sub(r"[^\w]+", sep, value.lower()).strip(sep)}},
    )
    ref_map = _extract_ref_map(md_text)
    md_text = _strip_leading_h1(md_text)
    html = md.convert(md_text)
    html = _wikilink_sub(html)
    html = _node_marker_sub(html)
    html = _inline_ref_sub(html, ref_map)
    return html


def _list_articles() -> list[dict]:
    """Scan the wiki vault for articles + extract category for nav."""
    articles = []
    if not _wiki_prose_dir().exists():
        return articles
    for p in sorted(_wiki_prose_dir().glob("*.md")):
        try:
            post = frontmatter.load(p)
            meta = post.metadata or {}
            articles.append({
                "name": p.stem,
                "category": meta.get("category") or "Other",
                "auto_generated": bool(meta.get("auto_generated")),
                "relationship_count": meta.get("relationship_count"),
                "event_count": meta.get("event_count"),
            })
        except Exception:
            articles.append({"name": p.stem, "category": "Other"})
    return articles


def _group_by_category(articles: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for a in articles:
        groups.setdefault(a["category"], []).append(a)
    for k in groups:
        groups[k].sort(key=lambda x: x["name"].lower())
    return dict(sorted(groups.items()))


@wiki_viewer_bp.route("/wiki/")
def wiki_index():
    articles = _list_articles()
    return render_template(
        "wiki_viewer.html",
        is_index=True,
        article=None,
        article_html=None,
        toc=[],
        categories=_group_by_category(articles),
        articles=articles,
        current_name=None,
    )


@wiki_viewer_bp.route("/wiki/random")
def wiki_random():
    articles = _list_articles()
    if not articles:
        abort(404, "No articles in vault")
    pick = random.choice(articles)
    return redirect(url_for("wiki_viewer.wiki_article", entity=pick["name"]))


@wiki_viewer_bp.route("/wiki/search")
def wiki_search():
    q = (request.args.get("q") or "").strip()
    results = []
    if q and _wiki_prose_dir().exists():
        ql = q.lower()
        for p in _wiki_prose_dir().glob("*.md"):
            try:
                text = p.read_text(encoding="utf-8")
                if ql in text.lower():
                    # Find a snippet
                    idx = text.lower().find(ql)
                    start = max(0, idx - 80)
                    end = min(len(text), idx + len(q) + 80)
                    snippet = text[start:end].replace("\n", " ")
                    results.append({"name": p.stem, "snippet": snippet})
            except Exception:
                continue
    articles = _list_articles()
    return render_template(
        "wiki_viewer.html",
        is_index=False,
        is_search=True,
        search_query=q,
        search_results=results,
        article=None,
        article_html=None,
        toc=[],
        categories=_group_by_category(articles),
        articles=articles,
        current_name=None,
    )


def _render_stub(entity: str):
    """Render a 'This page is a stub' placeholder when the article doesn't
    exist in the vault yet. Reuses the normal article template so the
    surrounding nav/sidebar stay consistent."""
    articles = _list_articles()
    stub_body = (
        f'<p><em>This page is a stub and is not yet populated.</em></p>'
        f'<p>No article has been generated for <strong>{entity}</strong>. '
        f'The wiki grows as pages are generated for the entities that other '
        f'pages link to.</p>'
    )
    return render_template(
        "wiki_viewer.html",
        is_index=False,
        article={
            "name": entity,
            "meta": {},
            "category": "Stub",
            "kg_node_id": None,
            "generated_at": None,
            "auto_generated": False,
            "is_stub": True,
        },
        article_html=stub_body,
        toc=[],
        categories=_group_by_category(articles),
        articles=articles,
        current_name=entity,
    )


@wiki_viewer_bp.route("/wiki/<entity>")
def wiki_article(entity: str):
    path = _find_article(_slugify(entity))
    if path is None:
        return _render_stub(entity)

    try:
        post = frontmatter.load(path)
        md_text = post.content
        meta = post.metadata or {}
    except Exception as exc:
        abort(500, f"Failed to parse {path.name}: {exc}")

    toc, _ = _extract_toc_and_sections(md_text)
    article_html = _render_markdown_to_html(md_text)

    articles = _list_articles()

    gen_at = meta.get("generated_at")
    gen_at_str = str(gen_at)[:10] if gen_at is not None else None

    return render_template(
        "wiki_viewer.html",
        is_index=False,
        article={
            "name": path.stem,
            "meta": meta,
            "category": meta.get("category") or "Other",
            "kg_node_id": meta.get("kg_node_id"),
            "generated_at": gen_at_str,
            "auto_generated": meta.get("auto_generated"),
        },
        article_html=article_html,
        toc=toc,
        categories=_group_by_category(articles),
        articles=articles,
        current_name=path.stem,
    )


@wiki_viewer_bp.route("/wiki/<entity>/regenerate", methods=["POST"])
def wiki_article_regenerate(entity: str):
    """Force a fresh rough → prose → consistency-critic pass for one article.

    Mirrors what the nightly refresh does for a single page; useful when an
    article has visibly drifted from the KG and you want it rewritten now.
    """
    path = _find_article(_slugify(entity))
    if path is None:
        return jsonify({"ok": False, "error": "Article not found"}), 404

    label = path.stem
    vault_path = _wiki_vault_root()

    try:
        from app.assistant.wiki_generator.page_writer import generate_prose_page_tagged
        from app.assistant.wiki_generator.wiki_writer import regenerate_entity_page
        from app.assistant.wiki_generator.consistency_critic import run_consistency_critic
    except Exception as exc:
        return jsonify({"ok": False, "error": f"wiki_generator import failed: {exc!s}"}), 500

    try:
        # Step 1: refresh the rough page from the current KG neighborhood.
        regenerate_entity_page(label=label, vault_path=vault_path)
        # Step 2: stitch the prose page (sections + lead). Returns None on
        # any silent precondition failure (missing taxonomy, no bullets, all
        # sections empty). Surface that to the UI instead of letting the
        # caller think the regen succeeded.
        prose_path = generate_prose_page_tagged(entity_label=label, vault_path=vault_path)
        if prose_path is None:
            return jsonify({
                "ok": False,
                "error": (
                    "Prose generation produced no output. "
                    "Common causes: missing resource_wiki_sections.json taxonomy, "
                    "no biographical bullets in the KG neighborhood, or every "
                    "section call returned empty. Check the server log for details."
                ),
            }), 500
        # Step 3: run the consistency critic; auto-investigate any new findings.
        crit = run_consistency_critic(entity_label=label, vault_path=vault_path)
    except Exception as exc:
        logger.exception("Regenerate failed for %s", label)
        return jsonify({"ok": False, "error": str(exc)}), 500

    findings_count = (crit or {}).get("findings_count", 0)
    saved_findings = (crit or {}).get("saved_findings", []) or []
    return jsonify({
        "ok": True,
        "label": label,
        "findings_count": findings_count,
        "saved_findings": saved_findings,
    })
