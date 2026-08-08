"""system_audit.inbox — resolution ingestion (the return half of the mailbox).

Claude Code sessions edit case files in data/claude_audit_inbox/: set
`status: resolved` (or `status: dismissed`) in the frontmatter and append a
`## Resolution` section. This ingest folds those edits back into the register,
closing the loop — which is what arms regression detection for the future.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import utc_now

logger = get_logger(__name__)

INBOX_DIR = Path("data/claude_audit_inbox")
_FM_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)


def _frontmatter_fields(text: str) -> dict:
    m = _FM_RE.match(text)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        k, _, v = line.partition(":")
        if _:
            out[k.strip()] = v.strip()
    return out


def ingest() -> int:
    """Fold Claude-edited case files back into the register. Returns cases closed."""
    from app.assistant.system_audit import case_store

    if not INBOX_DIR.exists():
        return 0
    live = {c["id"]: c for c in case_store.list_cases(
        statuses=["awaiting_claude", "investigated", "regressed"], limit=200)}
    closed = 0
    for path in sorted(INBOX_DIR.glob("case_*.md")):
        text = path.read_text(encoding="utf-8")
        fm = _frontmatter_fields(text)
        cid = fm.get("case_id") or path.stem.replace("case_", "")
        target = str(fm.get("status") or "").strip().lower()
        if target not in ("resolved", "dismissed") or cid not in live:
            continue
        res_idx = text.find("## Resolution")
        notes = text[res_idx:].strip() if res_idx >= 0 else ""
        commits = re.findall(r"\b[0-9a-f]{8,40}\b", notes)[:10]
        case_store.transition(cid, target, resolution={
            "disposition": target, "commits": commits,
            "notes": notes[:4000], "resolved_at": utc_now().isoformat(),
        })
        closed += 1
        logger.info("[inbox] case %s ingested as %s (%d commit ref(s))", cid, target, len(commits))
    return closed
