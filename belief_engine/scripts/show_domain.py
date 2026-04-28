"""Quick helper: print all active beliefs for a domain."""
import argparse
import sys

sys.path.insert(0, ".")
from app.assistant.tests.test_setup import initialize_services
initialize_services()

from belief_engine.store.belief_store import BeliefStore

parser = argparse.ArgumentParser()
parser.add_argument("domain")
args = parser.parse_args()

store = BeliefStore()
beliefs = store.list_by_domain(args.domain)
print(f"\n=== {args.domain} — {len(beliefs)} active beliefs ===\n")
for b in beliefs:
    print(f"  [obs={b.observation_count:2d}] {b.belief_key}")
    print(f"          {b.statement[:150]}")
    print()
