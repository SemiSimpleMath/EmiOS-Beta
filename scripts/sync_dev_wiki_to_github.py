"""Mirror docs/ to the EmiOS-Beta GitHub wiki repo.

The dev wiki source-of-truth is the docs/ directory in the main repo,
served locally at /dev-wiki/ by app/routes/dev_wiki.py. This script
publishes the same content to https://github.com/SemiSimpleMath/EmiOS-Beta/wiki
so it's browseable on github.com without the app running.

Preconditions:
  1. The wiki repo must already exist on github.com. GitHub creates the
     `.wiki.git` repo lazily when the first wiki page is created in the
     UI. If git ls-remote returns 404, this script prints clear
     instructions and exits without changes.
  2. You must have push access (you, as repo owner, do).

What it does:
  - Clones (or pulls) the wiki repo into a sibling working directory.
  - Walks docs/, copying every .md file EXCEPT those in audits/, drafts/,
    archived/ (personal scratch — gitignored locally for a reason).
  - Renames docs/INDEX.md to Home.md (GitHub wiki home convention).
  - Rewrites markdown links pointing to INDEX.md → Home (with no .md
    extension, since GitHub wiki drops it).
  - Generates a _Sidebar.md grouped by top-level directory so every wiki
    page has a navigation bar.
  - git add + commit + push (only if there are changes).

Source-of-truth: docs/. Manual edits to the GitHub wiki get clobbered
on the next sync run. Don't edit the wiki UI; edit docs/ files.

Default mode is dry-run — pass --commit --push to actually mutate.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple


REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_ROOT = REPO_ROOT / "docs"
DEFAULT_WIKI_URL = "https://github.com/SemiSimpleMath/EmiOS-Beta.wiki.git"
DEFAULT_WIKI_CLONE = REPO_ROOT.parent / "EmiOS-Beta.wiki"

# Personal scratch — gitignored locally; never publish.
EXCLUDED_SUBDIRS = frozenset({"audits", "drafts", "archived"})


# ----------------------------------------------------------------------------
# Path / link transformations
# ----------------------------------------------------------------------------


def transform_path(rel_path: str) -> str:
    """docs/<rel_path> → wiki path. INDEX.md at the root becomes Home.md."""
    if rel_path == "INDEX.md":
        return "Home.md"
    return rel_path


# Match markdown links: [text](target.md) or [text](target.md#anchor).
# Skip absolute http(s) and protocol-relative.
_MD_LINK_RE = re.compile(r'(\[[^\]]*\])\(([^)\s]+\.md)(#[^)]*)?\)')


def transform_link(match: re.Match, current_doc_dir: str) -> str:
    """Rewrite a markdown link target to its wiki equivalent.

    GitHub wiki:
      - Drops the .md extension in URLs
      - Resolves subdirectory paths
      - Treats Home.md as the wiki root

    current_doc_dir is the source-relative dir of the file being processed
    (used for resolving relative paths against the docs root).
    """
    text = match.group(1)
    target = match.group(2)
    anchor = match.group(3) or ""

    if target.startswith(("http://", "https://", "//", "/")):
        return match.group(0)

    # Resolve relative-to-current-doc path against docs root.
    cur = Path(current_doc_dir) if current_doc_dir else Path()
    try:
        resolved_rel = (cur / target).as_posix()
        # Normalize: split into parts, drop "." and resolve ".."
        parts: List[str] = []
        for part in Path(resolved_rel).parts:
            if part == "..":
                if parts:
                    parts.pop()
                continue
            if part == "." or part == "":
                continue
            parts.append(part)
        resolved = "/".join(parts)
    except Exception:
        resolved = target

    transformed = transform_path(resolved)
    # GitHub wiki drops the .md extension in URLs.
    if transformed.endswith(".md"):
        transformed = transformed[:-3]
    # Home page is the wiki root — no path needed.
    if transformed == "Home":
        transformed = "Home"

    return f"{text}({transformed}{anchor})"


def rewrite_links(content: str, current_doc_rel: str) -> str:
    """Apply transform_link to every markdown link in content."""
    current_dir = Path(current_doc_rel).parent.as_posix()
    return _MD_LINK_RE.sub(lambda m: transform_link(m, current_dir), content)


# ----------------------------------------------------------------------------
# File walk
# ----------------------------------------------------------------------------


def collect_docs() -> List[Tuple[str, str, Path]]:
    """Return [(source_rel, wiki_rel, source_path), ...] for every .md to publish.

    source_rel: path relative to docs/, e.g. 'architecture/11_WIKI_GENERATOR.md'
    wiki_rel:   path it should land at in the wiki repo, after transform
    """
    out: List[Tuple[str, str, Path]] = []
    for path in sorted(DOCS_ROOT.rglob("*.md")):
        rel = path.relative_to(DOCS_ROOT).as_posix()
        parts = Path(rel).parts
        if parts and parts[0] in EXCLUDED_SUBDIRS:
            continue
        wiki_rel = transform_path(rel)
        out.append((rel, wiki_rel, path))
    return out


# ----------------------------------------------------------------------------
# Sidebar generation
# ----------------------------------------------------------------------------


def generate_sidebar(entries: List[Tuple[str, str, Path]]) -> str:
    """Build a _Sidebar.md grouped by top-level docs directory.

    Each entry is rendered as a markdown link to the wiki path; GitHub wiki
    handles the .md-extension stripping and subdir routing.
    """
    grouped: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    home_entry: List[Tuple[str, str]] = []
    for source_rel, wiki_rel, _ in entries:
        if wiki_rel == "Home.md":
            home_entry.append(("Home", "Home"))
            continue
        parts = Path(source_rel).parts
        if len(parts) == 1:
            group = "Top level"
        else:
            group = parts[0]
        title = Path(wiki_rel).stem.replace("_", " ").replace("-", " ")
        # GitHub wiki link target drops .md
        target = wiki_rel[:-3] if wiki_rel.endswith(".md") else wiki_rel
        grouped[group].append((title, target))

    lines: List[str] = []
    if home_entry:
        for title, target in home_entry:
            lines.append(f"### [{title}]({target})")
            lines.append("")

    # Order: 'Top level' first, then 'welcome', then 'architecture', then
    # 'recipes', then everything else alphabetically.
    order_priority = {"Top level": 0, "welcome": 1, "architecture": 2, "recipes": 3}
    sorted_groups = sorted(
        grouped.keys(),
        key=lambda g: (order_priority.get(g, 99), g),
    )
    for group in sorted_groups:
        lines.append(f"**{group.replace('_', ' ').title()}**")
        for title, target in sorted(grouped[group]):
            lines.append(f"- [{title}]({target})")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ----------------------------------------------------------------------------
# Git operations
# ----------------------------------------------------------------------------


def _run(cmd: List[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, check=check, capture_output=True, text=True)


def ensure_wiki_clone(wiki_url: str, clone_dir: Path) -> bool:
    """Clone or update the wiki repo. Returns True if ready, False if 404."""
    if clone_dir.exists():
        # Pull latest.
        try:
            _run(["git", "fetch", "origin"], clone_dir)
            _run(["git", "reset", "--hard", "origin/master"], clone_dir, check=False)
        except subprocess.CalledProcessError as e:
            print(f"warn: fetch/reset failed in {clone_dir}: {e.stderr}", file=sys.stderr)
        return True

    # Clone fresh.
    print(f"Cloning {wiki_url} -> {clone_dir}")
    proc = subprocess.run(
        ["git", "clone", wiki_url, str(clone_dir)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        if "Repository not found" in (proc.stderr or "") or "not found" in (proc.stderr or "").lower():
            print()
            print("=" * 70, file=sys.stderr)
            print("ERROR: GitHub wiki repo doesn't exist yet.", file=sys.stderr)
            print(file=sys.stderr)
            print("To create it:", file=sys.stderr)
            print("  1. Go to https://github.com/SemiSimpleMath/EmiOS-Beta/wiki", file=sys.stderr)
            print("  2. Click 'Create the first page'", file=sys.stderr)
            print("  3. Enter ANY title (e.g. 'Home') and content (e.g. 'placeholder')", file=sys.stderr)
            print("  4. Save the page - this materializes the .wiki.git repo", file=sys.stderr)
            print("  5. Re-run this script - it will overwrite that placeholder Home page", file=sys.stderr)
            print("=" * 70, file=sys.stderr)
            return False
        print(f"error: git clone failed: {proc.stderr}", file=sys.stderr)
        return False
    return True


def diff_and_apply(
    entries: List[Tuple[str, str, Path]],
    sidebar_md: str,
    wiki_dir: Path,
    commit: bool,
) -> Tuple[List[str], List[str], List[str]]:
    """Sync content into wiki_dir.

    Returns (added, changed, removed) lists of wiki_rel paths describing
    what diverged from the wiki repo's current state.
    """
    added: List[str] = []
    changed: List[str] = []
    removed: List[str] = []

    # Map of wiki_rel -> new content
    target_content: Dict[str, str] = {}
    for source_rel, wiki_rel, source_path in entries:
        body = source_path.read_text(encoding="utf-8")
        rewritten = rewrite_links(body, source_rel)
        # Add a small footer noting the source file so wiki readers can find
        # the canonical version in the main repo if they want to PR a fix.
        if not rewritten.endswith("\n"):
            rewritten += "\n"
        rewritten += (
            f"\n---\n"
            f"_Source: [`docs/{source_rel}`]"
            f"(https://github.com/SemiSimpleMath/EmiOS-Beta/blob/release-v0.1/docs/{source_rel}) "
            f"in the main repo. Manual edits to this wiki page are overwritten by the next sync._\n"
        )
        target_content[wiki_rel] = rewritten
    target_content["_Sidebar.md"] = sidebar_md

    # Identify removed pages: any .md in wiki_dir that we won't write this run
    # AND that we previously published. We can't perfectly identify which
    # pages were "ours" without a manifest, so we conservatively only remove
    # files that have the source-footer marker — leaving any hand-authored
    # wiki pages alone.
    SOURCE_FOOTER_MARKER = "_Source: [`docs/"
    existing_md = sorted(p for p in wiki_dir.rglob("*.md") if p.is_file() and ".git" not in p.parts)
    for p in existing_md:
        rel = p.relative_to(wiki_dir).as_posix()
        if rel in target_content:
            continue
        if rel == "_Sidebar.md":
            removed.append(rel)
            if commit:
                p.unlink()
            continue
        try:
            content = p.read_text(encoding="utf-8")
        except Exception:
            continue
        if SOURCE_FOOTER_MARKER in content:
            removed.append(rel)
            if commit:
                p.unlink()

    # Write each target.
    for wiki_rel, content in target_content.items():
        target = wiki_dir / wiki_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            current = target.read_text(encoding="utf-8")
            if current == content:
                continue
            changed.append(wiki_rel)
        else:
            added.append(wiki_rel)
        if commit:
            target.write_text(content, encoding="utf-8")

    return added, changed, removed


def git_commit_and_push(wiki_dir: Path, message: str) -> bool:
    """Stage, commit, push. Returns True if a commit was made."""
    status = _run(["git", "status", "--porcelain"], wiki_dir)
    if not status.stdout.strip():
        return False
    _run(["git", "add", "-A"], wiki_dir)
    _run(["git", "commit", "-m", message], wiki_dir)
    push = subprocess.run(
        ["git", "push", "origin", "HEAD"],
        cwd=wiki_dir, capture_output=True, text=True,
    )
    if push.returncode != 0:
        print(f"error: git push failed: {push.stderr}", file=sys.stderr)
        return False
    return True


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wiki-url", default=DEFAULT_WIKI_URL,
                        help=f"Wiki repo URL (default {DEFAULT_WIKI_URL})")
    parser.add_argument("--clone-dir", type=Path, default=DEFAULT_WIKI_CLONE,
                        help=f"Local clone path (default {DEFAULT_WIKI_CLONE})")
    parser.add_argument("--commit", action="store_true",
                        help="Actually write files into the clone (default dry-run).")
    parser.add_argument("--push", action="store_true",
                        help="git add+commit+push the result (implies --commit).")
    parser.add_argument("--message", default="sync from docs/ via tools/sync_dev_wiki_to_github.py")
    args = parser.parse_args()

    if args.push:
        args.commit = True

    if not DOCS_ROOT.is_dir():
        print(f"error: docs root not found at {DOCS_ROOT}", file=sys.stderr)
        return 2

    if not ensure_wiki_clone(args.wiki_url, args.clone_dir):
        return 3

    entries = collect_docs()
    sidebar_md = generate_sidebar(entries)

    print(f"Source: {DOCS_ROOT}")
    print(f"Wiki:   {args.clone_dir}")
    print(f"Pages:  {len(entries)} (excludes {sorted(EXCLUDED_SUBDIRS)})")
    print()

    added, changed, removed = diff_and_apply(entries, sidebar_md, args.clone_dir, commit=args.commit)

    print(f"Added:    {len(added)}")
    for r in added:
        print(f"  + {r}")
    print(f"Changed:  {len(changed)}")
    for r in changed:
        print(f"  ~ {r}")
    print(f"Removed:  {len(removed)}")
    for r in removed:
        print(f"  - {r}")
    print()

    if not (added or changed or removed):
        print("No changes — wiki already up-to-date.")
        return 0

    if not args.commit:
        print("DRY RUN — pass --commit to write files into the clone, or --push to also push.")
        return 0

    if args.push:
        pushed = git_commit_and_push(args.clone_dir, args.message)
        if pushed:
            print(f"Pushed commit to {args.wiki_url}.")
        else:
            print("No commit made (status was clean).")
    else:
        print(f"Files updated in {args.clone_dir}. Pass --push to publish.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
