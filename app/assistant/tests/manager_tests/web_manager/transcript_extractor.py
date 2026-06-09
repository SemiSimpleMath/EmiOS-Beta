"""Extract a clean, FULL transcript of selected agents' rendered user.j2 prompts + outputs from a
run's agents log (logs/emi_logs_<pid>_agents.log). The raw log interleaves every agent, tool call,
critic, and warning; this pulls just the web::planner and web::web_summary blocks, in order, with
the prompts intact (the log is not truncated), so the planner <-> summarizer interaction is legible.

Run standalone on the newest log:  .venv/Scripts/python.exe -m app.assistant.tests.manager_tests.web_manager.transcript_extractor
Or it is called automatically at the end of web_manager_test.py.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Sequence

_MARK = re.compile(r" - (MODEL_USER|MODEL_SYSTEM|MODEL_OUTPUT) - ")
_LABEL = {"MODEL_USER": "USER (rendered user.j2)", "MODEL_SYSTEM": "SYSTEM", "MODEL_OUTPUT": "OUTPUT"}


def _agent_of(line: str):
    m = re.search(r"(?:USER|SYSTEM) PROMPT (\S+)", line) or re.search(r"LLM RESULT for (\S+)", line)
    return m.group(1) if m else None


def extract(log_path: str, out_path: str,
            agents: Sequence[str] = ("web::planner", "web::web_summary"),
            include_system_once: bool = True) -> int:
    lines = Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines()
    starts = [i for i, l in enumerate(lines) if _MARK.search(l)]
    starts.append(len(lines))

    out = [f"# Transcript — {', '.join(agents)}", f"# source: {os.path.basename(log_path)}", ""]
    seen_system: set = set()
    n = 0
    for k in range(len(starts) - 1):
        i, j = starts[k], starts[k + 1]
        mtype = _MARK.search(lines[i]).group(1)
        agent = _agent_of(lines[i])
        if agent not in agents:
            continue
        if mtype == "MODEL_SYSTEM":
            if not include_system_once or agent in seen_system:
                continue
            seen_system.add(agent)
        body = "\n".join(lines[i + 1:j]).strip()
        n += 1
        out.append("=" * 78)
        out.append(f"[{n}] {agent} — {_LABEL[mtype]}")
        out.append("=" * 78)
        out.append(body)
        out.append("")
    Path(out_path).write_text("\n".join(out), encoding="utf-8")
    return n


def _newest_log() -> Path:
    return max(Path("logs").glob("emi_logs_*_agents.log"), key=lambda p: p.stat().st_mtime)


def extract_for_current_run(out_path: str | None = None) -> str:
    """Call at the end of a harness run: prefer this process's own log, else the newest."""
    own = Path(f"logs/emi_logs_{os.getpid()}_agents.log")
    log = own if own.exists() else _newest_log()
    out_path = out_path or str(Path(__file__).parent / "last_run_transcript.md")
    n = extract(str(log), out_path)
    print(f"📝 transcript: {n} planner/summarizer blocks -> {out_path} (from {log.name})")
    return out_path


if __name__ == "__main__":
    import sys
    log = sys.argv[1] if len(sys.argv) > 1 else str(_newest_log())
    out = sys.argv[2] if len(sys.argv) > 2 else str(Path(__file__).parent / "last_run_transcript.md")
    print(f"wrote {extract(log, out)} blocks -> {out} (from {os.path.basename(log)})")
