"""Expose EmiOS's developer skills to the Claude Code CLI.

EmiOS keeps its skills in ``skills/<name>/SKILL.md``. Claude Code only discovers
skills under ``.claude/skills/<name>/SKILL.md``. Rather than copy the files —
two sources of truth, guaranteed to drift — this links the developer-facing
ones into place. Claude Code reads SKILL.md through a symlink or a Windows
directory junction, so a link is all it takes.

Only the skills about *changing EmiOS's code* are linked. The rest of
``skills/`` is runtime doctrine for the project's own agents (how to drive DoorDash,
how to post to Reddit, which lights are where) and has no business in a coding
session's menu.

``.claude/`` is gitignored, so the links are local to each clone — run this
once after cloning, and again if you add a new ``extending-emi-*`` skill.

    .venv\\Scripts\\python.exe scripts/link_claude_skills.py
    .venv\\Scripts\\python.exe scripts/link_claude_skills.py --list
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = REPO_ROOT / "skills"
TARGET_DIR = REPO_ROOT / ".claude" / "skills"

# Every skill whose name starts with this is about extending EmiOS, so new ones
# are picked up without editing this file.
DEV_SKILL_PREFIX = "extending-emi-"

# Developer skills that sit outside the naming convention: one is about
# changing EmiOS's UI templates, the other about finding your way around the
# codebase when the task is to fix something rather than add something.
DEV_SKILL_EXTRAS = ("emi-ui-templating", "diagnosing-emi")


def developer_skills() -> list[Path]:
    """Skill directories that belong in a coding session, sorted by name."""
    if not SOURCE_DIR.is_dir():
        sys.exit(f"No skills directory at {SOURCE_DIR}")
    picked = [
        d for d in sorted(SOURCE_DIR.iterdir())
        if d.is_dir()
        and (d / "SKILL.md").is_file()
        and (d.name.startswith(DEV_SKILL_PREFIX) or d.name in DEV_SKILL_EXTRAS)
    ]
    return picked


def _link(source: Path, target: Path) -> str:
    """Point target at source. Returns the mechanism used."""
    if sys.platform == "win32":
        # Junctions need no administrator rights, unlike symlinks.
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(target), str(source)],
            check=True, capture_output=True, text=True,
        )
        return "junction"
    os.symlink(source, target, target_is_directory=True)
    return "symlink"


def _unlink(target: Path) -> None:
    """Remove an existing link or directory at target."""
    if target.is_symlink():
        target.unlink()
    elif target.is_dir():
        # A junction reports as a directory; rmdir removes the link, not the
        # target's contents. shutil.rmtree would follow it and delete the real
        # skill, so never use that here.
        os.rmdir(target)
    elif target.exists():
        target.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--list", action="store_true",
        help="show what would be linked and exit",
    )
    args = parser.parse_args()

    skills = developer_skills()
    if not skills:
        sys.exit(f"No developer skills found under {SOURCE_DIR}")

    if args.list:
        print(f"{len(skills)} developer skills in {SOURCE_DIR}:")
        for s in skills:
            print(f"  {s.name}")
        return 0

    TARGET_DIR.mkdir(parents=True, exist_ok=True)

    wanted = {s.name for s in skills}
    linked, refreshed, removed = [], [], []

    # Drop links whose source is gone or is no longer a developer skill. Only
    # touch entries this script would have created; anything else is left alone.
    for existing in TARGET_DIR.iterdir():
        if existing.name not in wanted and (
            existing.name.startswith(DEV_SKILL_PREFIX)
            or existing.name in DEV_SKILL_EXTRAS
        ):
            _unlink(existing)
            removed.append(existing.name)

    for source in skills:
        target = TARGET_DIR / source.name
        if target.exists() or target.is_symlink():
            _unlink(target)
            refreshed.append(source.name)
        else:
            linked.append(source.name)
        mechanism = _link(source, target)

    print(f"Linked {len(skills)} developer skills into {TARGET_DIR} ({mechanism}s)")
    for name in sorted(linked):
        print(f"  + {name}")
    for name in sorted(refreshed):
        print(f"  = {name} (refreshed)")
    for name in sorted(removed):
        print(f"  - {name} (no longer a developer skill)")

    missing = shutil.which("claude") is None
    if missing:
        print("\nNote: the `claude` CLI is not on PATH, so nothing will read these yet.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
