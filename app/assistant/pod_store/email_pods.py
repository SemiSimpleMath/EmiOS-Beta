"""Emails live in the pod store only (2026-09-29).

The email fetch (lib/core_tools/email_tool/utils/email_utils.py) runs each email through
email_parser and writes the ones it keeps (importance >= 5) as `kind="email"` pods: the full body
verbatim, and every header and parser field in metadata. Nothing else stores emails; the event
repository no longer holds them. Readers — dayflow intake, the UI email widget, the ingest source
that feeds the signal router — go through this module.

Pod ids are deterministic from the Gmail account and uid, derived from the key the ingest copy step
used ("repo_email::<account>::<uid>"), so every email pod keeps the id it already had and every
reference to one (work objects, evidence) still resolves.

`migrate_repository_emails` is the one-time move, run at app startup: it rewrites existing email
pods' source refs to the Gmail message, backfills their parser fields from the event-repository
rows, mints a pod for any row that has none, then deletes the rows.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.assistant.pod_store.contracts import Pod, PodSourceRef
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

MIN_IMPORTANCE = 5
FETCH_CREATOR = "email_fetch"
MIGRATION_CREATOR = "email_pods_migration"
# Fields that belong on the pod row itself, not in metadata.
_BODY_KEY = "body"


def email_key(account_id: str, uid: str) -> str:
    return f"repo_email::{(account_id or '').strip() or 'unknown_account'}::{uid}"


def email_pod_id(account_id: str, uid: str) -> str:
    """The email's pod id: datapod:email:<first 24 hex of sha256(email_key)>."""
    digest = hashlib.sha256(email_key(account_id, uid).encode("utf-8")).hexdigest()[:24]
    return f"datapod:email:{digest}"


def _received_utc(email_data: Dict[str, Any]) -> Optional[str]:
    from email.utils import parsedate_to_datetime
    raw = str(email_data.get("date_received") or "").strip()
    if not raw or raw == "[No Date]":
        return None
    dt = parsedate_to_datetime(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def build_email_pod(email_data: Dict[str, Any]) -> Pod:
    """One parsed email (headers + email_parser fields + body) as its pod."""
    uid = str(email_data.get("uid") or "").strip()
    if not uid:
        raise ValueError("email has no uid")
    account_id = str(email_data.get("account_id") or "").strip()
    subject = str(email_data.get("subject") or "").strip()
    sender = str(email_data.get("sender") or "").strip()
    sender_email = str(email_data.get("email_address") or "").strip().lower()
    if not sender_email and "@" in sender:
        sender_email = sender.lower()
    body = str(email_data.get(_BODY_KEY) or "").strip()
    summary = str(email_data.get("summary") or "").strip()
    importance = email_data.get("importance")

    if sender and subject:
        one_liner = f"{sender}: {subject}"
    else:
        one_liner = subject or (f"email from {sender}" if sender else "email (no subject)")

    metadata = {k: v for k, v in email_data.items() if k != _BODY_KEY}
    metadata.update({
        "sender_display": sender,
        "sender_email": sender_email,
        "account_id": account_id,
        "uid": uid,
        "received_at_utc": _received_utc(email_data),
    })
    return Pod(
        pod_id=email_pod_id(account_id, uid),
        kind="email",
        tags=[],
        one_liner=one_liner,
        body=body or summary,
        source_refs=[PodSourceRef(kind="gmail", id=f"{account_id or 'unknown_account'}:{uid}")],
        for_agents=[],
        scope_id=account_id or None,
        created_by=FETCH_CREATOR,
        metadata=metadata,
        importance=float(importance) if importance is not None else None,
    )


def put_email_pod(email_data: Dict[str, Any], *, store=None) -> Tuple[str, bool]:
    """Store one parsed email as its pod. Returns (pod_id, created). An email already stored is
    left as it is: the fetch can see the same message again."""
    from app.assistant.pod_store.pod_store import PodStore
    store = store or PodStore()
    pod = build_email_pod(email_data)
    if store.get(pod.pod_id) is not None:
        return pod.pod_id, False
    store.put(pod)
    return pod.pod_id, True


def email_record(pod: Pod) -> Dict[str, Any]:
    """The pod as the email dict readers use: every header and parser field, plus the body."""
    record = dict(pod.metadata or {})
    record[_BODY_KEY] = pod.body or ""
    record["pod_id"] = pod.pod_id
    return record


def recent_emails(*, received_since: datetime, min_importance: int = MIN_IMPORTANCE, store=None) -> List[Dict[str, Any]]:
    """Emails RECEIVED since `received_since`, newest first, whose email_parser importance is at
    least `min_importance`.

    Filtered on the email's own received time, not the pod's creation time: a pod minted late (a
    backlog after downtime, the one-time move from the repository) is not a recent email. A pod is
    never created before its email was received, so the creation-time query is the superset."""
    from app.assistant.pod_store.pod_store import PodStore
    store = store or PodStore()
    since = received_since if received_since.tzinfo else received_since.replace(tzinfo=timezone.utc)
    out = []
    for pod in store.query(kind="email", since_utc=since, limit=None):
        record = email_record(pod)
        importance = record.get("importance")
        received = record.get("received_at_utc")
        if importance is None or int(importance) < min_importance or not received:
            continue
        if datetime.fromisoformat(received) < since:
            continue
        out.append(record)
    out.sort(key=lambda r: r["received_at_utc"], reverse=True)
    return out


# ── one-time move from the event repository (app startup) ────────────────────

def migrate_repository_emails() -> Dict[str, int]:
    """Move every trace of email out of the event repository. Idempotent; a no-op once done."""
    from app.models.db_manager import get_db_manager
    counts = {"refs_rewritten": 0, "backfilled": 0, "minted": 0, "rows_deleted": 0}
    mgr = get_db_manager()

    # 1. Source refs: event_repository:email -> gmail. Raw SQL, before any Pod is loaded: the model
    #    no longer accepts the old ref kind.
    with mgr.transaction(op="email_pods_migrate") as session:
        raw = session.connection().connection.driver_connection
        rows = raw.execute("SELECT pod_id, source_refs_json FROM pod_store WHERE kind='email' "
                           "AND source_refs_json LIKE '%event_repository:email%'").fetchall()
        for pod_id, refs_json in rows:
            refs = json.loads(refs_json) if isinstance(refs_json, str) else (refs_json or [])
            new_refs = []
            for ref in refs:
                if ref.get("kind") == "event_repository:email":
                    parts = str(ref.get("id") or "").split("::")
                    if len(parts) != 3:
                        # Not a Gmail message (the live store held one: a June test pod titled
                        # "sale", ref "email-xyz"). The pod is kept; the ref, which pointed into
                        # the repository and names no message, is dropped.
                        logger.warning("[email_pods] %s: source ref %r is not a Gmail message; dropped",
                                       pod_id, ref)
                        continue
                    ref = {"kind": "gmail", "id": f"{parts[1]}:{parts[2]}"}
                new_refs.append(ref)
            raw.execute("UPDATE pod_store SET source_refs_json=? WHERE pod_id=?", (json.dumps(new_refs), pod_id))
            counts["refs_rewritten"] += 1

    # 2. Repository rows: backfill the pod's parser fields, or mint the pod; then delete the rows.
    from app.assistant.pod_store.pod_store import PodStore
    store = PodStore()
    with mgr.read_session() as session:
        raw = session.connection().connection.driver_connection
        exists = raw.execute("SELECT name FROM sqlite_master WHERE name='event_repository'").fetchone()
        repo_rows = raw.execute("SELECT id, data FROM event_repository WHERE data_type='email'").fetchall() if exists else []
    for row_id, data in repo_rows:
        email_data = json.loads(data) if isinstance(data, str) else (data or {})
        if not email_data.get("uid"):
            raise ValueError(f"event_repository email row {row_id} has no uid")
        fresh = build_email_pod(email_data)
        held = store.get(fresh.pod_id)
        if held is None:
            # Marked as the move's, not the fetch's: the ingest source forwards only freshly
            # fetched email, so the backlog does not replay into the signal router.
            fresh.created_by = MIGRATION_CREATOR
            store.put(fresh)
            counts["minted"] += 1
        else:
            # The pod's own values win; the row adds the parser fields the old pod lacked.
            held.metadata = {**fresh.metadata, **(held.metadata or {})}
            if held.importance is None:
                held.importance = fresh.importance
            if not held.body:
                held.body = fresh.body
            store.put(held)
            counts["backfilled"] += 1
    if repo_rows:
        with mgr.transaction(op="email_pods_migrate") as session:
            raw = session.connection().connection.driver_connection
            counts["rows_deleted"] = raw.execute("DELETE FROM event_repository WHERE data_type='email'").rowcount
    if any(counts.values()):
        logger.info("[email_pods] moved email out of the event repository: %s", counts)
    return counts
