"""
Scripted decay scenarios. Run each in its own sandbox.

Run all:
    PYTHONPATH=. python -m belief_engine.sandbox.scenarios

Run one:
    PYTHONPATH=. python -m belief_engine.sandbox.scenarios <scenario_name>
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from belief_engine.sandbox.sandbox import BeliefSandbox


def _expect(condition: bool, message: str) -> None:
    marker = "PASS" if condition else "FAIL"
    print(f"  [{marker}] {message}")
    if not condition:
        raise AssertionError(message)


# ──────────────────────────────────────────────────────────────────────
# Scenarios
# ──────────────────────────────────────────────────────────────────────

def scenario_temporary_31d_deprecated():
    """A temporary belief unconfirmed for 31 days should be deprecated."""
    print("\n=== scenario: temporary @ 31d -> deprecated ===")
    with BeliefSandbox() as sb:
        sb.seed_belief(
            key="routine.coffee",
            scope="temporary",
            last_confirmed=sb.days_ago(31),
            domain="routine",
        )
        sb.print_state(domain="routine", label="before")
        result = sb.run_decay(domain="routine", now_utc=sb.now())
        sb.print_state(domain="routine", label="after")
        _expect("routine.coffee" in result["temporary_deprecated"],
                "routine.coffee was deprecated")
        _expect(result["chronic_contested"] == [], "no chronics contested")

        # Audit row written
        evs = sb.list_evidence(belief_key="routine.coffee")
        _expect(any(e["source_type"] == "deprecation" for e in evs),
                "deprecation evidence row recorded")


def scenario_temporary_29d_unchanged():
    """A temporary belief unconfirmed for 29 days should NOT be deprecated."""
    print("\n=== scenario: temporary @ 29d -> unchanged ===")
    with BeliefSandbox() as sb:
        sb.seed_belief(
            key="routine.coffee",
            scope="temporary",
            last_confirmed=sb.days_ago(29),
            domain="routine",
        )
        result = sb.run_decay(domain="routine", now_utc=sb.now())
        sb.print_state(domain="routine", label="after")
        _expect(result["temporary_deprecated"] == [], "no temporaries deprecated")
        _expect(result["chronic_contested"] == [], "no chronics contested")


def scenario_temporary_at_threshold():
    """A temporary belief at exactly 30 days — boundary check (>30, not >=)."""
    print("\n=== scenario: temporary @ exactly 30d -> boundary ===")
    with BeliefSandbox() as sb:
        # Fix a single reference time so the seeded last_confirmed and the
        # decay-run now_utc resolve to the SAME instant — otherwise the
        # microsecond skew between two now() calls makes "exactly 30d"
        # flaky to test.
        ref_now = sb.now()
        sb.seed_belief(
            key="routine.coffee",
            scope="temporary",
            last_confirmed=(ref_now - timedelta(days=30)).isoformat(),
            domain="routine",
        )
        result = sb.run_decay(domain="routine", now_utc=ref_now)
        # last_confirmed == cutoff. Strict less-than → not decayed.
        _expect(result["temporary_deprecated"] == [],
                "boundary case (exactly 30d) not decayed (strict <)")


def scenario_chronic_181d_contested():
    """A chronic belief unconfirmed for 181 days should be contested."""
    print("\n=== scenario: chronic @ 181d -> contested ===")
    with BeliefSandbox() as sb:
        sb.seed_belief(
            key="routine.morning_walk",
            scope="chronic",
            last_confirmed=sb.days_ago(181),
            domain="routine",
        )
        sb.print_state(domain="routine", label="before")
        result = sb.run_decay(domain="routine", now_utc=sb.now())
        sb.print_state(domain="routine", label="after")
        _expect("routine.morning_walk" in result["chronic_contested"],
                "routine.morning_walk was contested")
        _expect(result["temporary_deprecated"] == [], "no temporaries deprecated")

        evs = sb.list_evidence(belief_key="routine.morning_walk")
        _expect(any(e["source_type"] == "decay_review" for e in evs),
                "decay_review evidence row recorded")


def scenario_chronic_179d_unchanged():
    """A chronic belief unconfirmed for 179 days should NOT be contested."""
    print("\n=== scenario: chronic @ 179d -> unchanged ===")
    with BeliefSandbox() as sb:
        sb.seed_belief(
            key="routine.morning_walk",
            scope="chronic",
            last_confirmed=sb.days_ago(179),
            domain="routine",
        )
        result = sb.run_decay(domain="routine", now_utc=sb.now())
        _expect(result["temporary_deprecated"] == [], "no temporaries deprecated")
        _expect(result["chronic_contested"] == [], "no chronics contested")


def scenario_already_deprecated_unchanged():
    """A belief already deprecated should not be re-deprecated (idempotent)."""
    print("\n=== scenario: already-deprecated -> no-op ===")
    with BeliefSandbox() as sb:
        sb.seed_belief(
            key="routine.coffee",
            scope="temporary",
            last_confirmed=sb.days_ago(99),
            domain="routine",
            status="deprecated",
        )
        result = sb.run_decay(domain="routine", now_utc=sb.now())
        _expect(result["temporary_deprecated"] == [],
                "already-deprecated belief not touched")

        # No new evidence row since the belief never transitioned.
        evs = sb.list_evidence(belief_key="routine.coffee")
        _expect(all(e["source_type"] != "deprecation" for e in evs),
                "no deprecation evidence row added (no transition)")


def scenario_already_contested_unchanged():
    """A chronic belief already contested should not be re-contested."""
    print("\n=== scenario: already-contested chronic -> no-op ===")
    with BeliefSandbox() as sb:
        sb.seed_belief(
            key="routine.morning_walk",
            scope="chronic",
            last_confirmed=sb.days_ago(200),
            domain="routine",
            status="contested",
        )
        result = sb.run_decay(domain="routine", now_utc=sb.now())
        _expect(result["chronic_contested"] == [],
                "already-contested belief not re-flagged")


def scenario_domain_isolation():
    """Decay run on one domain must not touch another domain's beliefs."""
    print("\n=== scenario: domain isolation ===")
    with BeliefSandbox() as sb:
        sb.seed_belief(
            key="routine.coffee",
            scope="temporary",
            last_confirmed=sb.days_ago(60),
            domain="routine",
        )
        sb.seed_belief(
            key="health.sleep",
            scope="temporary",
            last_confirmed=sb.days_ago(60),
            domain="health",
        )
        result = sb.run_decay(domain="routine", now_utc=sb.now())
        _expect("routine.coffee" in result["temporary_deprecated"],
                "routine.coffee deprecated")
        _expect("health.sleep" not in result["temporary_deprecated"],
                "health.sleep NOT touched (different domain)")

        # Verify in DB
        all_b = sb.list_beliefs()
        health = [b for b in all_b if b["key"] == "health.sleep"][0]
        _expect(health["status"] == "active", "health.sleep still active")


def scenario_mixed_cohort():
    """Mixed cohort, run decay, count outcomes."""
    print("\n=== scenario: mixed cohort ===")
    with BeliefSandbox() as sb:
        # 3 should decay
        sb.seed_belief(key="r.t1", scope="temporary", last_confirmed=sb.days_ago(31), domain="routine")
        sb.seed_belief(key="r.t2", scope="temporary", last_confirmed=sb.days_ago(45), domain="routine")
        sb.seed_belief(key="r.t3", scope="temporary", last_confirmed=sb.days_ago(60), domain="routine")
        # 2 should not (recent or chronic-young)
        sb.seed_belief(key="r.t4", scope="temporary", last_confirmed=sb.days_ago(5), domain="routine")
        sb.seed_belief(key="r.c1", scope="chronic", last_confirmed=sb.days_ago(30), domain="routine")
        # 2 should be contested
        sb.seed_belief(key="r.c2", scope="chronic", last_confirmed=sb.days_ago(200), domain="routine")
        sb.seed_belief(key="r.c3", scope="chronic", last_confirmed=sb.days_ago(365), domain="routine")
        sb.print_state(domain="routine", label="before")

        result = sb.run_decay(domain="routine", now_utc=sb.now())
        sb.print_state(domain="routine", label="after")

        _expect(set(result["temporary_deprecated"]) == {"r.t1", "r.t2", "r.t3"},
                "exactly 3 temporaries deprecated")
        _expect(set(result["chronic_contested"]) == {"r.c2", "r.c3"},
                "exactly 2 chronics contested")


def scenario_synthetic_time_advance():
    """Seed at 'today', then run decay 60 days from now — temporaries fall off."""
    print("\n=== scenario: synthetic time advance (60 days forward) ===")
    with BeliefSandbox() as sb:
        # Seed with last_confirmed = today
        today = sb.now()
        sb.seed_belief(
            key="routine.coffee",
            scope="temporary",
            last_confirmed=today.isoformat(),
            domain="routine",
        )
        # Right now: not stale
        result_now = sb.run_decay(domain="routine", now_utc=today)
        _expect(result_now["temporary_deprecated"] == [],
                "fresh belief not decayed at t=0")

        # Fast-forward 60 days: should decay
        from datetime import timedelta
        future = today + timedelta(days=60)
        result_future = sb.run_decay(domain="routine", now_utc=future)
        _expect("routine.coffee" in result_future["temporary_deprecated"],
                "belief decayed at t=+60d")


def scenario_handoff_to_reevaluator():
    """Contested chronics should be appended to ctx.belief_update_result.contested_keys."""
    print("\n=== scenario: contested keys handed to reevaluator via ctx ===")
    with BeliefSandbox() as sb:
        sb.seed_belief(
            key="routine.morning_walk",
            scope="chronic",
            last_confirmed=sb.days_ago(200),
            domain="routine",
        )

        # Lightweight ctx that already has prior contested from update_beliefs
        from belief_engine.pipeline.steps.decay_stale_beliefs import DecayStaleBeliefsStep

        class _Ctx:
            belief_update_result = {"contested_keys": ["routine.from_updater"]}
            decay_result = None

        ctx = _Ctx()
        step = DecayStaleBeliefsStep("routine", now_utc=sb.now())
        step.run(ctx)

        merged = ctx.belief_update_result["contested_keys"]
        _expect("routine.from_updater" in merged, "prior contested preserved")
        _expect("routine.morning_walk" in merged, "decay-contested appended")


# ──────────────────────────────────────────────────────────────────────
# Driver
# ──────────────────────────────────────────────────────────────────────

ALL_SCENARIOS = [
    scenario_temporary_31d_deprecated,
    scenario_temporary_29d_unchanged,
    scenario_temporary_at_threshold,
    scenario_chronic_181d_contested,
    scenario_chronic_179d_unchanged,
    scenario_already_deprecated_unchanged,
    scenario_already_contested_unchanged,
    scenario_domain_isolation,
    scenario_mixed_cohort,
    scenario_synthetic_time_advance,
    scenario_handoff_to_reevaluator,
]


def main():
    if len(sys.argv) > 1:
        wanted = sys.argv[1]
        for fn in ALL_SCENARIOS:
            if fn.__name__ == wanted or fn.__name__ == f"scenario_{wanted}":
                fn()
                return
        print(f"Unknown scenario: {wanted}")
        print("Available:")
        for fn in ALL_SCENARIOS:
            print(f"  {fn.__name__}")
        sys.exit(1)

    failures = []
    for fn in ALL_SCENARIOS:
        try:
            fn()
        except AssertionError as e:
            failures.append((fn.__name__, str(e)))
        except Exception as e:
            failures.append((fn.__name__, f"unexpected: {type(e).__name__}: {e}"))

    print("\n" + "=" * 70)
    if failures:
        print(f"FAILED: {len(failures)} of {len(ALL_SCENARIOS)} scenarios")
        for name, err in failures:
            print(f"  {name}: {err}")
        sys.exit(1)
    else:
        print(f"ALL {len(ALL_SCENARIOS)} SCENARIOS PASSED")


if __name__ == "__main__":
    main()
