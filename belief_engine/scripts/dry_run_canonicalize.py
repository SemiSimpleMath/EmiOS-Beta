"""
Dry-run the belief canonicalization step across all domains.

Shows which belief clusters would be merged without writing anything to the DB.
Run from the repo root:

    python -m belief_engine.scripts.dry_run_canonicalize

Optional: filter to a single domain:
    python -m belief_engine.scripts.dry_run_canonicalize --domain routine
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Any, Optional

# Ensure app imports resolve.
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def _run(domain: Optional[str] = None, threshold: Optional[float] = None) -> None:
    from belief_engine.store.belief_store import BeliefStore
    from belief_engine.pipeline.steps.canonicalize_belief_set import (
        CanonicalizeBeliefSetStep,
        _find_duplicate_clusters,
        MERGE_THRESHOLD,
    )

    effective_threshold = threshold if threshold is not None else MERGE_THRESHOLD
    if threshold is not None:
        # Patch threshold for this run only.
        import belief_engine.pipeline.steps.canonicalize_belief_set as _mod
        _mod.MERGE_THRESHOLD = threshold

    store = BeliefStore()

    domains_to_check = [domain] if domain else [
        "routine", "health", "general", "food", "sleep", "communication", "work",
    ]

    total_clusters = 0
    total_beliefs_affected = 0

    for d in domains_to_check:
        all_beliefs = store.list_by_domain(d)
        if not all_beliefs:
            print(f"\n[{d}] No active beliefs.")
            continue

        print(f"\n{'='*60}")
        print(f"Domain: {d}  ({len(all_beliefs)} active beliefs, threshold={effective_threshold:.2f})")
        print(f"{'='*60}")

        clusters = _find_duplicate_clusters(store, d)

        if not clusters:
            print("  ✓ No near-duplicate clusters found.")
            continue

        total_clusters += len(clusters)
        for i, cluster in enumerate(clusters, 1):
            total_beliefs_affected += len(cluster)
            print(f"\n  Cluster {i} — {len(cluster)} beliefs would be consolidated:")
            for b in cluster:
                print(f"    [{b.confidence:6s}] obs={b.observation_count:3d}  key={b.belief_key}")
                # Wrap statement at 90 chars for readability
                words = b.statement.split()
                line = "             "
                for word in words:
                    if len(line) + len(word) + 1 > 102:
                        print(line)
                        line = "             " + word
                    else:
                        line += " " + word
                if line.strip():
                    print(line)

    print(f"\n{'='*60}")
    print(f"SUMMARY: {total_clusters} cluster(s) across {len(domains_to_check)} domain(s)")
    print(f"         {total_beliefs_affected} beliefs involved")
    print(f"         Estimated merges: {total_clusters} surviving, "
          f"{total_beliefs_affected - total_clusters} deprecated")
    print(f"{'='*60}")

    if threshold is not None:
        # Restore original threshold.
        import belief_engine.pipeline.steps.canonicalize_belief_set as _mod
        _mod.MERGE_THRESHOLD = MERGE_THRESHOLD


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dry-run belief canonicalization.")
    parser.add_argument("--domain", default=None, help="Single domain to check (default: all)")
    parser.add_argument(
        "--threshold", type=float, default=None,
        help=f"Similarity threshold override (default: {0.85})"
    )
    args = parser.parse_args()
    _run(domain=args.domain, threshold=args.threshold)
