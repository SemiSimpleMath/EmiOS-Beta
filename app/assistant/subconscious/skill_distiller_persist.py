"""Persist skill_distiller output.

Appends each weekly run's proposals (and skipped reasons + free-form
thinking) to `resources/subconscious/resource_learned_skills_proposed.md`.
The user reviews this file and decides what to move into the canonical
rule files. No auto-apply.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_resources_dir

logger = get_logger(__name__)


def apply_skill_distiller_output(output: Dict[str, Any]) -> Dict[str, Any]:
    """Append a markdown block to resource_learned_skills_proposed.md."""
    week_window_label = output.get("week_window_label") or "unknown_window"
    proposals = output.get("proposals") or []
    skipped = output.get("skipped_reasons") or []
    free_form = (output.get("free_form_thinking") or "").strip()
    now_utc_iso = datetime.now(timezone.utc).isoformat()

    block = _render_block(
        week_window_label=week_window_label,
        proposals=proposals,
        skipped=skipped,
        free_form=free_form,
        now_utc_iso=now_utc_iso,
    )

    path = get_resources_dir() / "subconscious" / "resource_learned_skills_proposed.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        header = (
            "# Learned skills (proposed)\n\n"
            "skill_distiller appends to this file weekly. Each block "
            "below is a candidate set of rule additions for the household's "
            "canonical rule files. Review proposals, accept by copying lines "
            "into the target_file noted, or reject by deleting / leaving here.\n\n"
        )
        path.write_text(header + block, encoding="utf-8")
    else:
        # Append (read-modify-write to ensure consistent encoding)
        existing = path.read_text(encoding="utf-8")
        path.write_text(existing.rstrip() + "\n\n" + block, encoding="utf-8")

    return {
        "proposals_count": len(proposals),
        "skipped_count": len(skipped),
        "appended_to": str(path),
        "appended_at_utc": now_utc_iso,
    }


def _render_block(
    *,
    week_window_label: str,
    proposals: List[Dict[str, Any]],
    skipped: List[str],
    free_form: str,
    now_utc_iso: str,
) -> str:
    out: List[str] = []
    out.append(f"## Distillation — week {week_window_label}")
    out.append("")
    out.append(f"_Run at {now_utc_iso}_")
    out.append("")

    if proposals:
        out.append(f"### Proposals ({len(proposals)})")
        out.append("")
        for i, p in enumerate(proposals, 1):
            kind = p.get("proposal_kind") or "?"
            target = p.get("target_file") or "?"
            section = p.get("target_section") or ""
            novelty = p.get("novelty") or "?"
            confidence = p.get("confidence") or "?"
            rule = (p.get("rule_text") or "").strip()
            why = (p.get("why_now") or "").strip()
            related = (p.get("related_existing_rule") or "").strip()
            evidence = p.get("evidence") or []

            out.append(f"#### {i}. [{kind}] → `{target}`" + (f" / `{section}`" if section else ""))
            out.append(f"_{novelty}, confidence={confidence}_")
            out.append("")
            out.append("**Proposed addition:**")
            out.append("")
            out.append("```markdown")
            out.append(rule)
            out.append("```")
            out.append("")
            if why:
                out.append(f"**Why now:** {why}")
                out.append("")
            if related:
                out.append(f"**Touches existing rule:** {related}")
                out.append("")
            if evidence:
                out.append("**Evidence:**")
                for e in evidence:
                    src = e.get("source_kind") or "?"
                    ref = e.get("ref") or "?"
                    snip = (e.get("snippet") or "").replace("\n", " ").strip()
                    out.append(f"- {src} {ref}: {snip}")
                out.append("")
    else:
        out.append("### Proposals")
        out.append("")
        out.append("_No proposals this week._")
        out.append("")

    if skipped:
        out.append("### Hypotheses considered but skipped")
        out.append("")
        for s in skipped:
            out.append(f"- {s}")
        out.append("")

    if free_form:
        out.append("### Distiller's thinking")
        out.append("")
        out.append(free_form)
        out.append("")

    out.append("---")
    out.append("")
    return "\n".join(out)
