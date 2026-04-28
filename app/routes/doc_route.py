from __future__ import annotations

import json
from pathlib import Path

from flask import Blueprint, jsonify, abort, request

from app.assistant.utils.path_utils import get_repo_root
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

doc_bp = Blueprint("doc", __name__)

_DOCS_ROOT = "docs"


def _docs_dir() -> Path:
    return Path(get_repo_root()) / _DOCS_ROOT


def _first_heading(path: Path) -> str:
    """Return the text of the first # heading after frontmatter, or ''."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception as e:
        logger.debug("_first_heading: could not read %s: %s", path, e)
        return ""
    in_fm = lines and lines[0].strip() == "---"
    fm_end = 0
    if in_fm:
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                fm_end = i + 1
                break
    for line in lines[fm_end:]:
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return ""


def _read_doc_frontmatter(path: Path) -> dict:
    """Extract YAML frontmatter from a markdown file."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception as e:
        logger.debug("_read_doc_frontmatter: could not read %s: %s", path, e)
        return {}
    if not lines or lines[0].strip() != "---":
        return {}
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}
    try:
        import yaml
        return yaml.safe_load("\n".join(lines[1:end])) or {}
    except Exception as e:
        logger.debug("_read_doc_frontmatter: YAML parse error in %s: %s", path, e)
        return {}


def list_docs() -> list[dict]:
    """
    Return all user-facing docs as a list of dicts with doc_id, name, doc_type, updated_at.

    Sources (in order):
    1. docs/registry.json — explicit entries (named/promoted docs, gdoc links)
    2. docs/drafts/*.md   — auto-discovered user drafts
    """
    docs_dir = _docs_dir()
    results: list[dict] = []
    seen: set[str] = set()

    # 1. Registry (explicit named docs, includes gdoc entries)
    registry_path = docs_dir / "registry.json"
    if registry_path.is_file():
        try:
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            for entry in registry.get("docs") or []:
                if not isinstance(entry, dict):
                    continue
                doc_id = str(entry.get("id") or "").strip()
                if not doc_id or doc_id in seen:
                    continue
                seen.add(doc_id)
                results.append({
                    "doc_id": doc_id,
                    "name": str(entry.get("name") or doc_id),
                    "doc_type": str(entry.get("doc_type") or "md"),
                    "updated_at": str(entry.get("updated_at") or ""),
                })
        except Exception as e:
            logger.error("Failed reading docs registry: %s", e)
            logger.debug("docs registry list exception", exc_info=True)

    # 2. Filesystem scan — docs/drafts/*.md not already registered
    drafts_dir = docs_dir / "drafts"
    if drafts_dir.is_dir():
        draft_files = sorted(drafts_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        for draft in draft_files:
            doc_id = draft.stem
            if doc_id in seen:
                continue
            seen.add(doc_id)
            fm = _read_doc_frontmatter(draft)
            name = _first_heading(draft) or doc_id
            results.append({
                "doc_id": doc_id,
                "name": name,
                "doc_type": "md",
                "updated_at": str(fm.get("updated_at") or ""),
            })

    return results


def _find_doc(name: str) -> tuple[Path | None, str, str]:
    """
    Search for a doc markdown file by name or ID.

    Searches in order:
    1. docs/registry.json — matched by id, name, or alias (case-insensitive)
    2. docs/<name>.md — direct file match
    3. Fuzzy: docs/<file>.md where filename contains the query

    Returns (path, doc_id, doc_type) or (None, "", "md").
    """
    query = name.strip().lower()
    if not query:
        return None, "", "md"

    docs_dir = _docs_dir()

    # 1. Registry lookup
    registry_path = docs_dir / "registry.json"
    if registry_path.is_file():
        try:
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            for entry in registry.get("docs") or []:
                if not isinstance(entry, dict):
                    continue
                doc_id = str(entry.get("id") or "").strip()
                entry_name = str(entry.get("name") or "").strip().lower()
                doc_type = str(entry.get("doc_type") or "md").strip().lower()
                aliases = [str(a).strip().lower() for a in (entry.get("aliases") or []) if a]

                if query in {doc_id.lower(), entry_name} or query in aliases:
                    file_path = str(entry.get("file") or "").strip()
                    if file_path:
                        candidate = Path(get_repo_root()) / file_path
                        if candidate.is_file():
                            return candidate, doc_id, doc_type
                    # Fallback: docs/<doc_id>.md
                    fallback = docs_dir / f"{doc_id}.md"
                    if fallback.is_file():
                        return fallback, doc_id, doc_type
        except Exception as e:
            logger.error("Failed reading docs registry: %s", e)
            logger.debug("docs registry load exception", exc_info=True)

    # 2. Direct filename match (docs/ then docs/drafts/)
    for candidate_dir in [docs_dir, docs_dir / "drafts"]:
        direct = candidate_dir / f"{query}.md"
        if direct.is_file():
            return direct, query, "md"
        sanitised = query.replace(" ", "_")
        direct2 = candidate_dir / f"{sanitised}.md"
        if direct2.is_file():
            return direct2, sanitised, "md"

    # 3. Fuzzy match in docs/ and docs/drafts/
    search_dirs = [docs_dir]
    drafts_dir = docs_dir / "drafts"
    if drafts_dir.is_dir():
        search_dirs.append(drafts_dir)
    for search_dir in search_dirs:
        if not search_dir.is_dir():
            continue
        for child in sorted(search_dir.iterdir()):
            if not child.is_file() or child.suffix != ".md":
                continue
            stem = child.stem.lower()
            if query in stem or stem in query:
                return child, child.stem, "md"
            # Also match against the first `# Heading` in the file (skip frontmatter)
            try:
                lines = child.read_text(encoding="utf-8").splitlines()
                # Skip YAML frontmatter block (--- ... ---)
                start = 0
                if lines and lines[0].strip() == "---":
                    for k in range(1, len(lines)):
                        if lines[k].strip() == "---":
                            start = k + 1
                            break
                for line in lines[start:]:
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("#"):
                        title = line.lstrip("#").strip().lower()
                        if query in title or title in query:
                            return child, child.stem, "md"
                    break
            except Exception:
                pass

    return None, "", "md"


@doc_bp.route("/doc/list", methods=["GET"])
def list_docs_route():
    """
    List available user docs (drafts + registered named docs).

    Returns:
        { "docs": [ { "doc_id": "...", "name": "...", "doc_type": "...", "updated_at": "..." }, ... ] }
    """
    return jsonify({"docs": list_docs()})


@doc_bp.route("/doc/save", methods=["POST"])
def save_doc_draft():
    """
    Save a doc markdown draft to disk.

    Accepts: { "markdown": "...", "draft_id": "<optional>", "doc_type": "md" }
    Returns: { "draft_id": "<id>", "path": "<rel path>" }
    """
    import re as _re
    import uuid as _uuid
    from datetime import datetime, timezone

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        abort(400, "Request body must be a JSON object")

    markdown = str(payload.get("markdown") or "").strip()
    if not markdown:
        abort(400, "markdown field is required and must be non-empty")

    raw_id = str(payload.get("draft_id") or "").strip()
    if raw_id:
        safe_id = "".join(c for c in raw_id if c.isalnum() or c in "-_")
    else:
        safe_id = "doc_draft_" + _uuid.uuid4().hex[:10]

    repo_root = Path(get_repo_root())
    out_dir = repo_root / _DOCS_ROOT / "drafts"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{safe_id}.md"

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if markdown.startswith("---"):
        markdown = _re.sub(
            r"^(---\n(?:.*\n)*?)(?:updated_at:.*\n)?",
            lambda m: m.group(1) + f"updated_at: {ts}\n",
            markdown,
            count=1,
        )
    else:
        markdown = f"---\nupdated_at: {ts}\n---\n\n" + markdown

    try:
        out_path.write_text(markdown, encoding="utf-8")
        logger.info("Saved doc draft draft_id=%s to %s", safe_id, out_path)
    except Exception as e:
        logger.error("Failed saving doc draft draft_id=%s: %s", safe_id, e)
        logger.debug("doc draft save exception", exc_info=True)
        abort(500, "Failed to save draft")

    rel_path = str(out_path.relative_to(repo_root))
    return jsonify({"draft_id": safe_id, "path": rel_path})


@doc_bp.route("/doc/load", methods=["GET"])
def load_doc():
    """
    Load an existing doc by name or ID.

    Query params:
        name — doc name, ID, or alias to search for

    Returns:
        { "doc_id": "...", "doc_markdown": "...", "doc_type": "md" | "gdoc" }
    """
    name = str(request.args.get("name") or "").strip()
    if not name:
        abort(400, "name query parameter is required")

    doc_path, doc_id, doc_type = _find_doc(name)
    if doc_path is None:
        return jsonify({"error": f"No doc found for '{name}'"}), 404

    try:
        doc_markdown = doc_path.read_text(encoding="utf-8")
    except Exception as e:
        logger.error("Failed reading doc for name=%s path=%s: %s", name, doc_path, e)
        logger.debug("doc load read exception", exc_info=True)
        abort(500, "Failed to read doc")

    logger.info("load_doc: found doc_id=%s path=%s doc_type=%s", doc_id, doc_path, doc_type)
    return jsonify({
        "doc_id": doc_id,
        "doc_markdown": doc_markdown,
        "doc_type": doc_type,
    })


@doc_bp.route("/doc/load-gdoc", methods=["GET"])
def load_gdoc():
    """
    Load an existing Google Doc from Drive by name search or document ID.

    Query params:
        name — substring to search for in Google Drive doc titles
        id   — Google Docs document ID (takes priority over name)

    Returns:
        { "doc_id": "...", "title": "...", "doc_markdown": "...", "doc_type": "gdoc", "doc_url": "..." }
    """
    doc_id = str(request.args.get("id") or "").strip()
    name = str(request.args.get("name") or "").strip()

    if not doc_id and not name:
        abort(400, "Either 'name' or 'id' query parameter is required")

    try:
        from app.assistant.lib.core_tools.google_docs_tool.google_docs_client import GoogleDocsClient

        client = GoogleDocsClient(readonly=True)

        if not doc_id:
            hits = client.find_documents(name_contains=name, max_results=5)
            if not hits:
                return jsonify({"error": f"No Google Doc found matching '{name}'"}), 404
            doc_id = str(hits[0].get("id") or "").strip()
            if not doc_id:
                return jsonify({"error": "Search returned a result with no document ID"}), 500

        doc = client.get_document(doc_id)
        title = str(doc.get("title") or "Untitled")
        plain_text = client.extract_plain_text(doc)

        drive_meta: dict = {}
        try:
            drive_meta = client.get_document_metadata(doc_id)
        except Exception as e:
            logger.warning("load_gdoc: could not fetch Drive metadata for %s: %s", doc_id, e)

        doc_url = drive_meta.get("webViewLink") or f"https://docs.google.com/document/d/{doc_id}/edit"

        # Seed the doc editor session + unified_log doc_store.
        session_id = ""
        try:
            from app.assistant.ServiceLocator.service_locator import DI
            from app.assistant.room_session_manager.services.doc_creation_session_service import DocCreationSessionService
            from app.assistant.lib.doc_utils.doc_store import save_doc_draft
            svc = DocCreationSessionService(blackboard=DI.global_blackboard)
            binding = svc.activate_room_binding(
                room_id="doc_editor", surface="ui", context_id="main",
                initiated_by="doc_route", doc_type="gdoc", doc_name=title,
            )
            session_id = str(binding.get("session_id") or "").strip()
            if session_id:
                save_doc_draft(
                    session_id,
                    doc_markdown=plain_text,
                    doc_type="gdoc",
                    doc_name=title,
                    google_doc_id=doc_id,
                    caller="doc_route.load_gdoc",
                )
                logger.info("load_gdoc: seeded doc_store session %s with %d chars", session_id, len(plain_text))
        except Exception as e:
            logger.warning("load_gdoc: failed to seed doc editor session: %s", e)
            logger.debug("load_gdoc seed exception", exc_info=True)

        logger.info("load_gdoc: doc_id=%s title=%r chars=%d", doc_id, title, len(plain_text))
        return jsonify({
            "doc_id": doc_id,
            "title": title,
            "doc_markdown": plain_text,
            "doc_type": "gdoc",
            "doc_url": doc_url,
            "session_id": session_id,
        })

    except (FileNotFoundError, PermissionError) as e:
        logger.error("load_gdoc OAuth/permission error: %s", e)
        logger.debug("load_gdoc auth exception details", exc_info=True)
        oauth_url = "/oauth/google/start?redirect_to=google_oauth_settings"
        return jsonify({
            "error": str(e),
            "needs_oauth": True,
            "oauth_url": oauth_url,
        }), 401
    except Exception as e:
        logger.error("load_gdoc failed: %s", e)
        logger.debug("load_gdoc exception details", exc_info=True)
        return jsonify({"error": f"Failed to load Google Doc: {e}"}), 500


@doc_bp.route("/doc/push-gdoc", methods=["POST"])
def push_to_gdoc():
    """
    Push a markdown document to Google Docs (create new or update existing).

    Accepts: { "markdown": "...", "doc_id": "<optional Google Doc ID>", "title": "<optional>" }
    Returns: { "doc_id": "...", "doc_url": "..." }
    """
    import uuid as _uuid

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        abort(400, "Request body must be a JSON object")

    markdown = str(payload.get("markdown") or "").strip()
    if not markdown:
        abort(400, "markdown field is required and must be non-empty")

    existing_doc_id = str(payload.get("doc_id") or "").strip() or None
    title = str(payload.get("title") or "").strip() or f"Document {_uuid.uuid4().hex[:8]}"
    make_public = bool(payload.get("make_public", False))
    account_id = str(payload.get("account_id") or "").strip() or None

    try:
        from app.assistant.lib.core_tools.google_docs_tool.google_docs_client import GoogleDocsClient
        from googleapiclient.discovery import build as _build
        from googleapiclient.errors import HttpError as _HttpError
        client = GoogleDocsClient(readonly=False, account_id=account_id)
        drive_svc = _build("drive", "v3", credentials=client._creds)

        def _set_public(gid: str) -> None:
            drive_svc.permissions().create(
                fileId=gid,
                body={"type": "anyone", "role": "reader"},
            ).execute()

        if existing_doc_id:
            gdoc_id = None
            try:
                doc = client.get_document(existing_doc_id)
                gdoc_id = existing_doc_id
            except _HttpError as e:
                if e.resp.status != 404:
                    raise
                gdoc_id = None

            if gdoc_id:
                current_text = client.extract_plain_text(doc)
                if current_text.strip():
                    client.replace_all_text(gdoc_id, find=current_text, replace_with=markdown, match_case=False)
                else:
                    client.append_text(gdoc_id, markdown)
                meta = client.get_document_metadata(gdoc_id)
                doc_url = meta.get("webViewLink") or ""
                doc_id = gdoc_id
            else:
                file_meta = {"name": title, "mimeType": "application/vnd.google-apps.document"}
                created = drive_svc.files().create(body=file_meta, fields="id,webViewLink").execute()
                doc_id = created.get("id") or ""
                doc_url = created.get("webViewLink") or ""
                if doc_id:
                    client.append_text(doc_id, markdown)
        else:
            file_meta = {"name": title, "mimeType": "application/vnd.google-apps.document"}
            created = drive_svc.files().create(body=file_meta, fields="id,webViewLink").execute()
            doc_id = created.get("id") or ""
            doc_url = created.get("webViewLink") or ""
            if doc_id:
                client.append_text(doc_id, markdown)

        is_public = False
        if make_public and doc_id:
            _set_public(doc_id)
            is_public = True
            logger.info("push_to_gdoc: set public doc_id=%s", doc_id)

        logger.info("push_to_gdoc: doc_id=%s doc_url=%s account=%s is_public=%s", doc_id, doc_url, client.principal_email, is_public)
        return jsonify({"doc_id": doc_id, "doc_url": doc_url, "account_email": client.principal_email, "account_id": client.account_id, "is_public": is_public})

    except (FileNotFoundError, PermissionError) as e:
        logger.error("Failed pushing to Google Doc: %s", e)
        logger.debug("push-gdoc exception", exc_info=True)
        oauth_url = "/oauth/google/start?redirect_to=google_oauth_settings"
        return jsonify({
            "error": str(e),
            "needs_oauth": True,
            "oauth_url": oauth_url,
        }), 401
    except Exception as e:
        logger.error("Failed pushing to Google Doc: %s", e)
        logger.debug("push-gdoc exception", exc_info=True)
        abort(500, f"Failed to push to Google Doc: {e}")
