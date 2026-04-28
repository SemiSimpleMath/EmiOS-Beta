"""
One-time cleanup: consolidate google_oauth_accounts rows so every row's
account_id exists in configs/oauth_accounts.json (the registry).

Why: historical inconsistencies created dup rows for the same physical Google
account under different account_id labels (e.g. 'primary', 'default',
'semisimplemath@gmail.com', 'google_emi'). After upsert_credentials now
validates against the registry, those non-registry rows can never be refreshed
and just sit in the DB. This cleans them up and promotes any fresher
credentials they hold onto the registry row whose scopes they match.

Bucketing strategy: principal_email isn't reliably populated (modern OAuth
tokens don't carry an embedded principal), so we group by granted_scopes —
each non-registry row is matched to the registry account whose configured
scopes overlap most. The freshest creds in each bucket are promoted to the
registry account; remaining non-registry rows in that bucket are deleted.
Rows with no scope overlap to any registry account are deleted as orphans.

Dry-run by default. Pass --apply to actually write.

Usage:
    .venv/Scripts/python.exe scripts/consolidate_oauth_accounts.py          # dry run
    .venv/Scripts/python.exe scripts/consolidate_oauth_accounts.py --apply  # write changes
"""
import sys
import os
import json
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.assistant.tests.test_setup  # noqa: F401 — bootstrap DI

from app.models.base import get_session
from app.assistant.lib.google_auth import oauth_registry
from app.assistant.lib.google_auth.oauth_account_store import GoogleOAuthAccount

DRY_RUN = "--apply" not in sys.argv


def _ts(dt):
    if dt is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _scopes_of(row) -> set[str]:
    raw = json.loads(row.granted_scopes_json or "[]")
    return {str(s) for s in raw if isinstance(s, str)}


def _bucket_for(nr_scopes: set[str], registry_rows) -> str | None:
    """Return registry account_id whose configured scopes overlap most with nr_scopes."""
    best_id = None
    best_score = 0
    for kr in registry_rows:
        try:
            kr_scopes = set(oauth_registry.get_scopes(kr.account_id))
        except Exception:
            continue
        score = len(nr_scopes & kr_scopes)
        if score > best_score:
            best_id = kr.account_id
            best_score = score
    return best_id


def main():
    session = get_session()
    try:
        rows = session.query(GoogleOAuthAccount).all()
        registry_ids = set(oauth_registry.list_accounts().keys())

        keep = [r for r in rows if r.account_id in registry_ids]
        drop = [r for r in rows if r.account_id not in registry_ids]

        print(f"\n{'=' * 60}")
        print(f"OAuth account consolidation — {'DRY RUN' if DRY_RUN else 'APPLY'}")
        print(f"{'=' * 60}")
        print(f"Total rows: {len(rows)}")
        print(f"Registry accounts: {sorted(registry_ids)}")

        print(f"\nKEEP ({len(keep)} rows in registry):")
        for r in keep:
            print(
                f"  - {r.account_id:30s} | updated={r.updated_at} "
                f"| active={r.is_active} | scopes={sorted(_scopes_of(r))}"
            )

        print(f"\nNON-REGISTRY ({len(drop)} rows):")
        for r in drop:
            print(
                f"  - {r.account_id:30s} | updated={r.updated_at} "
                f"| active={r.is_active} | scopes={sorted(_scopes_of(r))}"
            )

        # Bucket non-registry rows by best-overlap registry account
        buckets: dict[str | None, list] = {}
        for nr in drop:
            target_id = _bucket_for(_scopes_of(nr), keep)
            buckets.setdefault(target_id, []).append(nr)

        # Build plan: per bucket, pick freshest, promote/delete-dup rest; orphans delete
        plan = []
        for target_id, nr_list in buckets.items():
            if target_id is None:
                for nr in nr_list:
                    plan.append((nr, None, "DELETE_ORPHAN", "no scope overlap with any registry account"))
                continue
            target = next(r for r in keep if r.account_id == target_id)
            nr_list.sort(key=lambda r: _ts(r.updated_at), reverse=True)
            freshest = nr_list[0]
            if _ts(freshest.updated_at) > _ts(target.updated_at):
                plan.append((freshest, target, "PROMOTE_CREDS_THEN_DELETE", f"fresher than {target.account_id}"))
            else:
                plan.append((freshest, target, "DELETE_DUP", f"{target.account_id} has equal/fresher creds"))
            for other in nr_list[1:]:
                plan.append((other, target, "DELETE_DUP", f"older than {freshest.account_id} in same bucket"))

        print(f"\nPLAN ({len(plan)} actions):")
        for nr, target, action, detail in plan:
            tgt = target.account_id if target else "—"
            print(f"  [{action}] {nr.account_id} -> {tgt}: {detail}")

        if DRY_RUN:
            print("\nDRY RUN — no changes written. Re-run with --apply to commit.")
            return

        promoted = 0
        deleted = 0
        for nr, target, action, _ in plan:
            if action == "PROMOTE_CREDS_THEN_DELETE":
                target.credentials_encrypted = nr.credentials_encrypted
                target.granted_scopes_json = nr.granted_scopes_json
                target.principal_email = nr.principal_email or target.principal_email
                target.last_refreshed_at = nr.last_refreshed_at
                target.updated_at = nr.updated_at
                target.is_active = True
                target.revoked_at = None
                promoted += 1
            session.delete(nr)
            deleted += 1
        session.commit()
        print(f"\nApplied: promoted {promoted} cred set(s), deleted {deleted} row(s).")
    finally:
        session.close()


if __name__ == "__main__":
    main()
