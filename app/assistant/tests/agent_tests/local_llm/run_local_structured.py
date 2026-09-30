"""Proof of concept: the agents' structured output on a LOCAL model, schema enforced by the server.

Writes nothing but the usual llm_call_log telemetry rows (provider "local", or the hosted
provider for `--model hosted`).

Three checks, each against a model served by Ollama (or any OpenAI-compatible local server) through
app.services.llm_client.LocalLLM:

  coverage  Every AgentForm in the chosen agent namespaces is sent as the enforced schema with a
            "fill this form" prompt. Shape only: proves the server's grammar handles the real
            Pydantic forms (nested models, Literals, Optionals, lists) and the reply validates.
  dedup     belief_engine::belief_dedup re-judges recorded decisions from a replay store with its
            real prompt and form; the local verdict is compared with the one the hosted model made.
  tagger    belief_engine::belief_tagger tags 15 live beliefs; compared with the stored (hosted)
            tags. Nothing is written.

Every agent created here is switched to the local provider; nothing else in the app changes.

    .venv\\Scripts\\python.exe -m app.assistant.tests.agent_tests.local_llm.run_local_structured coverage --model gemma4:12b
    .venv\\Scripts\\python.exe -m app.assistant.tests.agent_tests.local_llm.run_local_structured dedup --model qwen3.5:9b --limit 20
    .venv\\Scripts\\python.exe -m app.assistant.tests.agent_tests.local_llm.run_local_structured tagger --model gemma4:12b
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sqlite3
import sys
import time
from collections import Counter

os.environ.setdefault("LOCAL_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
# Must match the server (the proof runs used OLLAMA_CONTEXT_LENGTH=32768): a cut prompt then raises.
os.environ.setdefault("LOCAL_LLM_CONTEXT_TOKENS", "32768")
if "--effort" in sys.argv:
    os.environ["LOCAL_LLM_REASONING_EFFORT"] = sys.argv[sys.argv.index("--effort") + 1]

import app.assistant.tests.test_setup  # noqa: E402,F401  bootstraps DI
from app.assistant.ServiceLocator.service_locator import DI  # noqa: E402
from app.assistant.utils.path_utils import get_repo_root  # noqa: E402

COVERAGE_NAMESPACES = ("dayflow_orchestrator", "belief_engine", "master_room", "work_emi_team",
                       "emi_team", "personal_admin", "subconscious", "ticket_builder")


def route_agents_to_local(model: str) -> None:
    """Every agent the factory creates from here on runs on the local model. `--model hosted`
    leaves the agents on their configured hosted models: the baseline for the same sample."""
    if model == "hosted":
        return
    factory = DI.agent_factory
    original = factory.create_agent

    def create_agent(*args, **kwargs):
        agent = original(*args, **kwargs)
        if agent is not None:
            agent.llm_params = {"llm_provider": "local", "engine": model, "temperature": 0.1, "timeout": 900,
                                "max_tokens": 8192}
        return agent
    factory.create_agent = create_agent


def coverage(model: str) -> int:
    from app.services.llm_client import LocalLLM
    llm = LocalLLM(engine=model)
    root = get_repo_root() / "app" / "assistant" / "agents"
    forms = []
    for ns in COVERAGE_NAMESPACES:
        for path in sorted((root / ns).rglob("agent_form.py")):
            mod = ".".join(path.relative_to(get_repo_root()).with_suffix("").parts)
            form = getattr(importlib.import_module(mod), "AgentForm", None)
            if form is not None:
                forms.append((str(path.parent.relative_to(root)).replace("\\", "/"), form))
    tally, rows = Counter(), []
    for name, form in forms:
        messages = [
            {"role": "system", "content": "You fill in the structured output forms of a personal assistant's "
                                          "internal agents."},
            {"role": "user", "content": f"This is the output form of the agent '{name}'. Produce one realistic, "
                                        "internally consistent example of its output."},
        ]
        t = time.monotonic()
        try:
            llm.structured_output(messages, response_format=form, engine=model, timeout=600, max_tokens=8192)
            status = "valid"
        except Exception as e:
            status = f"FAIL {str(e)[:160]}"
        secs = time.monotonic() - t
        tally["valid" if status == "valid" else "fail"] += 1
        rows.append((name, status, secs))
        print(f"{secs:6.1f}s  {status[:180]:<60}  {name}", flush=True)
    print(f"\ncoverage {model}: {tally['valid']}/{len(forms)} forms valid; "
          f"median {sorted(r[2] for r in rows)[len(rows) // 2]:.1f}s per form")
    return 0


def dedup(model: str, limit: int, store: str) -> int:
    from app.assistant.embeddings.embedder import embed_texts
    from app.assistant.tests.agent_tests.belief_dedup.run_propagation import store_before
    from belief_engine.intake import agents
    from belief_engine.intake.rank import ordered
    route_agents_to_local(model)
    base = get_repo_root() / store
    c = sqlite3.connect(f"{(base / 'belief_replay.db').resolve().as_uri()}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    # A spread over the four verdicts, oldest first within each, so the sample is not all `new`.
    decisions = []
    for v in ("same", "contradicts", "refines", "new"):
        decisions += [dict(r) for r in c.execute("SELECT * FROM decisions WHERE verdict=? ORDER BY id LIMIT ?",
                                                 (v, max(1, limit // 4)))]
    scope_ctx = agents.scope()
    held_cache: dict = {}
    tally, agree, secs = Counter(), 0, []
    for d in decisions:
        day = d["day"]
        held = held_cache.setdefault(day, store_before(c, day))
        extract = json.loads((base / "extract" / f"{day}.json").read_text(encoding="utf-8"))
        atom = next(a for a in extract["atoms"] if a["statement"] == d["statement"])
        vec = embed_texts([atom["statement"]])[0]
        t = time.monotonic()
        try:
            v = agents.dedup(atom, day, ordered(held, vec), scope_ctx)
            local = v["verdict"] + (f" -> {v['target']}" if v.get("target") else "")
            same = v["verdict"] == d["verdict"] and (v.get("target") or None) == (d["target"] or None)
        except Exception as e:
            local, same = f"FAIL {str(e)[:120]}", False
        secs.append(time.monotonic() - t)
        hosted = d["verdict"] + (f" -> {d['target']}" if d["target"] else "")
        agree += same
        tally[f"{d['verdict']} -> {local.split(' ')[0]}"] += 1
        print(f"{secs[-1]:6.1f}s  {'AGREE' if same else 'DIFF '}  hosted {hosted:<22} local {local:<22}  "
              f"{atom['statement'][:90]}", flush=True)
    print(f"\ndedup {model}: {agree}/{len(decisions)} agree with the hosted verdict (verdict and target); "
          f"median {sorted(secs)[len(secs) // 2]:.1f}s per call; {dict(tally)}")
    return 0


def tagger(model: str) -> int:
    from app.assistant.utils.pydantic_classes import Message
    from belief_engine.db.paths import belief_db_path
    from belief_engine.tagging import _vocab, sanitize
    route_agents_to_local(model)
    c = sqlite3.connect(f"file:{belief_db_path()}?mode=ro", uri=True)
    rows = c.execute("SELECT id, statement FROM belief_intake_beliefs WHERE status='active' "
                     "ORDER BY CAST(SUBSTR(id, 2) AS INTEGER) LIMIT 15").fetchall()
    stored = {}
    for bid, tag in c.execute("SELECT belief_id, tag FROM belief_tags WHERE belief_id GLOB 'B[0-9]*'"):
        stored.setdefault(bid, set()).add(tag)
    batch = "\n".join(f"[b{k + 1}] (domain: none) {stmt}" for k, (_, stmt) in enumerate(rows))
    vocab = "\n".join(f"- {t}: {d}" for t, d in _vocab().items())
    agent = DI.agent_factory.create_agent("belief_engine::belief_tagger")
    t = time.monotonic()
    data = agent.action_handler(Message(agent_input={"task": batch, "information": vocab}, task=batch)).data or {}
    secs = time.monotonic() - t
    got = {str(a.get("id")): set(sanitize(a.get("tags") or [])) for a in data.get("assignments") or []}
    jacc = []
    for k, (bid, stmt) in enumerate(rows, 1):
        local, hosted = got.get(f"b{k}", set()), stored.get(bid, set())
        j = len(local & hosted) / len(local | hosted) if (local | hosted) else 1.0
        jacc.append(j)
        print(f"{bid:5} J={j:.2f}  hosted {sorted(hosted)}  local {sorted(local)}  | {stmt[:70]}")
    print(f"\ntagger {model}: {len(got)}/{len(rows)} beliefs answered in {secs:.1f}s; "
          f"mean tag agreement (Jaccard) {sum(jacc) / len(jacc):.2f}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("check", choices=["coverage", "dedup", "tagger"])
    p.add_argument("--model", default="gemma4:12b")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--store", default="scratch/belief_replay")
    p.add_argument("--effort", help="reasoning effort for thinking models: none | low | medium | high")
    a = p.parse_args()
    if a.check == "coverage":
        return coverage(a.model)
    if a.check == "dedup":
        return dedup(a.model, a.limit, a.store)
    return tagger(a.model)


if __name__ == "__main__":
    sys.exit(main())
