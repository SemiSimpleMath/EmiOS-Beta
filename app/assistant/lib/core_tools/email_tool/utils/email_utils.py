# app/assistant/lib/core_tools/email_tool/utils/email_utils.py

import os
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from email import message_from_string

from app.assistant.lib.core_tools.email_tool.utils.gmail_api_client import GmailAPIClient
from app.assistant.lib.core_tools.email_tool.utils.email_processor import EmailProcessor
from app.assistant.utils.pydantic_classes import (
    Message,
    ScopeContext,
    ScopeResourcePolicy,
)
from app.assistant.event_repository.event_repository import EventRepositoryManager
from app.assistant.ServiceLocator.service_locator import DI
from sqlalchemy import select

from app.assistant.database.db_handler import UnifiedLog2026
from app.assistant.maintenance_manager.save_to_unified_db import save_to_unified_db
from app.assistant.utils.logging_config import get_logger
from app.models.base import get_session

logger = get_logger(__name__)


# Scope for the email_parser agent when invoked from the email ingest /
# triage paths. These calls are background work (no chain room, no
# upstream manager), so we mint a system-actor scope that lets the
# agent resolve its declared resources (resource_email_user_prefs).
_EMAIL_PARSER_SCOPE = ScopeContext(
    scope_id="email_tool::email_parser",
    owner_id="system",
    actor_id="email_tool",
    surface="internal",
    resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
)

class EmailUtils:
    """Handles email fetching, processing, and storing using Gmail API."""

    def __init__(self):
        """Initialize Gmail API client"""
        self._gmail_clients: dict[str, GmailAPIClient] = {}
        self.gmail_client = self._get_gmail_client(None)
        self.repo_manager = EventRepositoryManager()

    def _get_gmail_client(self, account_id: str | None) -> GmailAPIClient:
        key = str(account_id or "").strip() or "__default__"
        client = self._gmail_clients.get(key)
        if client is None:
            client = GmailAPIClient(account_id=(None if key == "__default__" else key))
            self._gmail_clients[key] = client
        return client

    def get_gmail_client(self, account_id: str | None) -> GmailAPIClient:
        return self._get_gmail_client(account_id)

    def fetch_and_store_emails(
        self,
        start_date,
        unseen=True,
        search_string=None,
        repo_update=False,
        start_timestamp=None,
        end_timestamp=None,
        account_id: str | None = None,
    ):
        """
        Fetch emails using Gmail API, process them, and optionally update the repo.

        Args:
            start_date: Start date string in Gmail format (YYYY/MM/DD)
            unseen: Whether to search only unseen emails
            search_string: Optional text search string
            repo_update: If True, store emails in EventRepository
            start_timestamp: Optional datetime for client-side filtering (inclusive)
            end_timestamp: Optional datetime for client-side filtering (inclusive)

        For repo_update=True (scheduler):
          - Store all qualifying emails in EventRepository (data_type='email').
          - Call sync_events_with_server once with all seen UIDs.
          - sync_events_with_server will prune email events older than 10 hours
            that were not seen in this run.

        For repo_update=False (agent queries):
          - Do not touch the repo at all. Just return processed_emails.
        """
        # Build Gmail query using unix timestamps (more precise than date strings)
        start_ts = None
        end_ts = None
        gmail_start_date = None
        
        if start_timestamp:
            # Convert to unix timestamp
            start_ts = int(start_timestamp.timestamp())
            logger.debug(f"Using start timestamp: {start_ts} ({start_timestamp})")
        else:
            # Fallback to date string if no timestamp provided
            gmail_start_date = self._convert_to_gmail_date(start_date)
            logger.debug(f"Using start date string: {gmail_start_date}")
        
        if end_timestamp:
            # Gmail's "before:" is exclusive, so add 1 second to make it inclusive
            end_ts = int(end_timestamp.timestamp()) + 1
            logger.debug(f"Using end timestamp: {end_ts} ({end_timestamp} + 1 second)")
        
        gmail_client = self._get_gmail_client(account_id)
        resolved_account_id = str(getattr(gmail_client, "account_id", "") or "").strip() or "unknown_account"
        effective_search_string = search_string
        if repo_update:
            # Repository ingest is the watcher source of truth; constrain to inbound mailbox flow.
            inbox_scope = "in:inbox"
            effective_search_string = f"{search_string} {inbox_scope}".strip() if search_string else inbox_scope

        query = gmail_client.build_query(
            start_date=gmail_start_date,
            end_date=None,
            start_timestamp=start_ts,
            end_timestamp=end_ts,
            unseen=unseen,
            search_string=effective_search_string,
        )
        
        emails = gmail_client.search_emails(query, max_results=100)
        logger.info(
            "Email fetch summary: fetched=%d query=%r start=%s end=%s unseen=%s search_string=%r",
            len(emails),
            query,
            start_timestamp.isoformat() if start_timestamp else None,
            end_timestamp.isoformat() if end_timestamp else None,
            unseen,
            effective_search_string,
        )
        
        if not emails:
            logger.debug(f"📧 Email fetch complete: 0 candidates found, 0 stored")
            return []

        # Step 1: Filter by timestamp BEFORE parsing (to avoid wasting time on emails we'll discard)
        logger.debug("Email pre-filter count=%d", len(emails))
        
        filtered_emails = []
        skipped_outside_timerange = 0
        
        if start_timestamp or end_timestamp:
            for email_meta in emails:
                # Fetch full email
                full_email = gmail_client.fetch_full_email(email_meta["uid"])
                if not full_email:
                    continue
                
                # Parse raw email string into EmailMessage object
                email_message = message_from_string(full_email["raw_email"])
                email_metadata = EmailProcessor.extract_metadata(email_message)
                
                date_received_str = email_metadata.get("date_received", "")
                if date_received_str and date_received_str != "[No Date]":
                    try:
                        email_datetime = parsedate_to_datetime(date_received_str)
                        # Ensure timezone-aware (parsedate_to_datetime returns timezone-aware)
                        if email_datetime.tzinfo is None:
                            email_datetime = email_datetime.replace(tzinfo=timezone.utc)
                        
                        # Filter by start_timestamp (inclusive)
                        if start_timestamp and email_datetime < start_timestamp:
                            skipped_outside_timerange += 1
                            logger.debug(
                                f"Skipping email {email_meta['uid']} - date {email_datetime} "
                                f"is before start_timestamp {start_timestamp}"
                            )
                            continue
                        
                        # Filter by end_timestamp (inclusive)
                        if end_timestamp and email_datetime > end_timestamp:
                            skipped_outside_timerange += 1
                            logger.debug(
                                f"Skipping email {email_meta['uid']} - date {email_datetime} "
                                f"is after end_timestamp {end_timestamp}"
                            )
                            continue
                    except Exception as e:
                        logger.warning(
                            f"Could not parse date_received '{date_received_str}' for email {email_meta['uid']}: {e}. "
                            f"Including email anyway."
                        )
                        # If we can't parse the date, include it to be safe
                
                # Email passed timestamp filter - store it for processing
                filtered_emails.append((email_meta, full_email, email_metadata))
        else:
            # No timestamp filtering - fetch all emails
            for email_meta in emails:
                full_email = gmail_client.fetch_full_email(email_meta["uid"])
                if not full_email:
                    continue
                # Parse raw email string into EmailMessage object
                email_message = message_from_string(full_email["raw_email"])
                email_metadata = EmailProcessor.extract_metadata(email_message)
                filtered_emails.append((email_meta, full_email, email_metadata))
        
        logger.info(
            "Email filter summary: after_primary_filters=%d skipped_outside_range=%d",
            len(filtered_emails),
            skipped_outside_timerange,
        )
        
        # Step 2: Repository ingest path for watcher/runtime (background scheduler).
        # Run each email through email_parser so importance/summary/action_items are populated
        # before storing — these fields drive the UI email widget display.
        if repo_update:
            summary_agent = DI.agent_factory.create_agent("email_parser")
            processed_emails = []
            email_ids = []
            skipped_low_importance = 0
            skipped_missing_importance = 0

            for idx, (email_meta, full_email, email_metadata) in enumerate(filtered_emails):
                email_message = message_from_string(full_email["raw_email"])
                email_body = EmailProcessor.extract_email_body(email_message)

                agent_msg = Message(agent_input=email_body, scope_context=_EMAIL_PARSER_SCOPE)
                result_data = summary_agent.action_handler(agent_msg)
                summary_data = result_data.data if hasattr(result_data, "data") else result_data

                logger.debug("Repo ingest: parsed email %d/%d uid=%s", idx + 1, len(filtered_emails), email_meta["uid"])

                email_data = {
                    **email_metadata,
                    **summary_data,
                    "uid": email_meta["uid"],
                    "message_id": email_meta["uid"],
                    "thread_id": str(email_meta.get("threadId") or ""),
                    "data_type": "email",
                    "account_id": resolved_account_id,
                    "direction": "inbound",
                    "body": email_body,
                }

                importance_raw = email_data.get("importance")
                if importance_raw is None:
                    skipped_missing_importance += 1
                    logger.error(
                        "Repo ingest: email %s missing importance after parsing — skipping. subject=%r",
                        email_meta["uid"],
                        email_data.get("subject"),
                    )
                    continue

                try:
                    importance = int(importance_raw)
                except (ValueError, TypeError) as e:
                    skipped_missing_importance += 1
                    logger.error(
                        "Repo ingest: invalid importance %r for email %s — skipping. subject=%r",
                        importance_raw,
                        email_meta["uid"],
                        email_data.get("subject"),
                    )
                    logger.debug("Repo ingest importance parse exception", exc_info=True)
                    continue

                if importance < 5:
                    skipped_low_importance += 1
                    logger.debug(
                        "Repo ingest: skipping email %s importance=%d (<5) subject=%r",
                        email_meta["uid"],
                        importance,
                        email_data.get("subject"),
                    )
                    email_ids.append(email_meta["uid"])  # still mark as read
                    continue

                processed_emails.append(email_data)
                email_ids.append(email_meta["uid"])

            if email_ids:
                logger.debug("Batch writing %d inbound emails to repository.", len(processed_emails))
                recipient_source = str(os.getenv("EMAIL_ADDR") or "").strip().lower() or "email"
                unified_messages = []

                session = get_session()
                try:
                    existing_unified_ids = set()

                    candidate_ids = []
                    for email_data in processed_emails:
                        stable_email_id = f"email:{resolved_account_id}:{str(email_data.get('uid') or '').strip()}"
                        if stable_email_id:
                            candidate_ids.append(stable_email_id)

                    if candidate_ids:
                        existing_rows = session.execute(
                            select(UnifiedLog2026.id).where(UnifiedLog2026.id.in_(candidate_ids))
                        ).all()
                        existing_unified_ids = {str(row[0]) for row in existing_rows if row and row[0]}
                finally:
                    session.close()

                for email_data in processed_emails:
                    event_id = f"{resolved_account_id}:{email_data['uid']}"
                    self.repo_manager.store_event(
                        event_id,
                        event_data=email_data,
                        data_type="email",
                    )

                    stable_email_id = f"email:{resolved_account_id}:{str(email_data.get('uid') or '').strip()}"
                    if not stable_email_id:
                        logger.error("Skipping unified log email insert because stable email id is empty: %r", email_data)
                        continue

                    if stable_email_id in existing_unified_ids:
                        logger.debug(
                            "Skipping unified log insert for already-ingested email id=%s subject=%r",
                            stable_email_id,
                            email_data.get("subject"),
                        )
                        continue

                    date_received_raw = str(email_data.get("date_received") or "").strip()
                    email_timestamp = datetime.now(timezone.utc)
                    if date_received_raw and date_received_raw != "[No Date]":
                        try:
                            parsed_dt = parsedate_to_datetime(date_received_raw)
                            if parsed_dt.tzinfo is None:
                                parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)
                            email_timestamp = parsed_dt.astimezone(timezone.utc)
                        except Exception as e:
                            logger.warning(
                                "Could not parse email date_received %r for uid=%s, falling back to now(): %s",
                                date_received_raw,
                                email_data.get("uid"),
                                e,
                            )

                    sender_name = str(email_data.get("sender") or "").strip() or "Unknown Sender"
                    subject = str(email_data.get("subject") or "").strip() or "[No Subject]"
                    summary = str(email_data.get("summary") or "").strip()
                    importance = int(email_data.get("importance", 0) or 0)

                    action_items = email_data.get("action_items")
                    if not isinstance(action_items, list):
                        action_items = []

                    content = f"Important email from {sender_name}: {subject}."
                    if summary:
                        content += f" {summary}"
                    if action_items:
                        joined_items = "; ".join(str(x).strip() for x in action_items if str(x).strip())
                        if joined_items:
                            content += f" Mentioned items: {joined_items}."

                        unified_messages.append(
                            {
                                "id": stable_email_id,
                                "timestamp": email_timestamp,
                                "role": "system",
                                "message": content,
                                "source": recipient_source,
                                "processed": False,
                                "request_id": None,
                                "room_id": None,
                                "room_surface": None,
                                "room_context_id": None,
                                "direction": str(email_data.get("direction") or "inbound").strip(),
                                "speaker_id": None,
                                "speaker_name": sender_name or None,
                                "speaker_role": "email_sender",
                                "speaker_external_id": str(email_data.get("email_address") or "").strip() or None,
                                "transport_message_id": str(email_data.get("message_id") or "").strip() or None,
                                "transport_from": str(email_data.get("email_address") or "").strip() or None,
                                "transport_to": resolved_account_id,
                                "content_type": "text",
                                "media_items_json": None,
                                "link_items_json": None,
                                "metadata_json": {
                                    "data_type": "email",
                                    "sub_data_type": ["email_event"],
                                    "email_uid": str(email_data.get("uid") or "").strip(),
                                    "provider_message_id": str(email_data.get("message_id") or "").strip(),
                                    "thread_id": str(email_data.get("thread_id") or "").strip(),
                                    "account_id": resolved_account_id,
                                    "subject": subject,
                                    "date_received_raw": date_received_raw,
                                    "importance": importance,
                                    "email_summary": summary,
                                    "source_system": "email_parser",
                                    "email_ingested_tf": True,
                                },
                                "data_json": {
                                    "data_type": "email",
                                    "action_items": action_items,
                                },
                            }
                        )

                if unified_messages:
                    save_to_unified_db(unified_messages, recipient_source)
                logger.debug("Batch write complete.")

                for uid in email_ids:
                    try:
                        gmail_client.mark_as_read(uid)
                    except Exception as e:
                        logger.debug("Could not mark email %s as read: %s", uid, e)
                logger.debug("Marked %d ingested emails as read.", len(email_ids))

                logger.info(
                    "Inbound repo email ingest complete: fetched=%d stored=%d "
                    "skipped_low_importance=%d skipped_missing_importance=%d "
                    "skipped_outside_range=%d account_id=%s",
                    len(emails),
                    len(processed_emails),
                    skipped_low_importance,
                    skipped_missing_importance,
                    skipped_outside_timerange,
                    resolved_account_id,
                )
                return processed_emails

        # Step 3: Triage path (non-repo_update): summarize/filter for planner use.
        # PHASE A: Process all emails with LLM (no DB writes)
        processed_emails = []
        email_ids = []
        skipped_low_importance = 0
        skipped_missing_importance = 0
        
        summary_agent = DI.agent_factory.create_agent("email_parser")
        logger.debug("Starting LLM processing for %d filtered emails.", len(filtered_emails))

        for idx, (email_meta, full_email, email_metadata) in enumerate(filtered_emails):
            # Now extract body and parse (we already have metadata from filtering step)
            # Parse raw email string into EmailMessage object
            email_message = message_from_string(full_email["raw_email"])
            email_body = EmailProcessor.extract_email_body(email_message)

            agent_msg = Message(agent_input=email_body, scope_context=_EMAIL_PARSER_SCOPE)
            result_data = summary_agent.action_handler(agent_msg)
            summary_data = result_data.data if hasattr(result_data, "data") else result_data

            logger.debug(f"📧 Processed email {idx+1}/{len(filtered_emails)}")

            email_data = {
                **email_metadata,
                **summary_data,
                "uid": email_meta["uid"],
                "message_id": email_meta["uid"],
                "thread_id": str(email_meta.get("threadId") or ""),
                "data_type": "email",
                "account_id": resolved_account_id,
            }

            # Require a valid importance; if missing or invalid, skip
            importance_raw = email_data.get("importance", None)

            if importance_raw is None:
                skipped_missing_importance += 1
                logger.error(
                    f"Email {email_meta['uid']} missing 'importance'. "
                    f"Skipping. Subject: {email_data.get('subject', 'No Subject')}"
                )
                continue

            try:
                importance = int(importance_raw)
            except (ValueError, TypeError):
                skipped_missing_importance += 1
                logger.error(
                    f"Invalid importance value '{importance_raw}' for email {email_meta['uid']}. "
                    f"Skipping. Subject: {email_data.get('subject', 'No Subject')}"
                )
                continue

            # Filter by importance - low importance emails are already marked as read but not stored
            if importance < 5:
                skipped_low_importance += 1
                logger.debug(
                    "Skipping storage for email %s with importance %s (<5): %s",
                    email_meta["uid"],
                    importance,
                    email_data.get("subject", "No Subject"),
                )
                continue

            processed_emails.append(email_data)
            if repo_update:
                email_ids.append(email_meta["uid"])

        # PHASE B: Batch write to database (quick, single burst)
        # This ensures DB lock is held for minimal time
        if repo_update and email_ids:
            logger.debug("Batch writing %d emails to repository.", len(email_ids))
            for email_data in processed_emails:
                event_id = f"{resolved_account_id}:{email_data['uid']}"
                self.repo_manager.store_event(
                    event_id,
                    event_data=email_data,
                    data_type="email",
                )
            # Single sync call enforces 10 hour policy for emails
            self.repo_manager.sync_events_with_server(email_ids, "email")
            logger.debug("Batch write complete.")

        # Debug summary
        logger.info(
            f"📧 Email fetch complete: {len(emails)} candidates found, "
            f"{len(processed_emails)} stored (importance >= 5), "
            f"{skipped_low_importance} skipped (importance < 5), "
            f"{skipped_missing_importance} skipped (missing/invalid importance), "
            f"{skipped_outside_timerange} skipped (outside timestamp range)"
        )

        return processed_emails

    def fetch_and_filter_emails(
        self,
        *,
        start_timestamp: datetime,
        end_timestamp: datetime | None = None,
        unseen: bool = False,
        account_id: str | None = None,
    ) -> list[dict]:
        """
        Triage-path email fetch: inbox only, no repo write, LLM importance filter applied.

        Fetches emails for the given window, runs each through the email_parser agent,
        and returns only messages with importance >= 5.  Nothing is written to the
        EventRepository or any file.  This is the correct method to call from an agent
        that wants the user's filtered daily email — as opposed to fetch_and_store_emails
        which is the scheduler/repo-ingest path.

        Args:
            start_timestamp: Inclusive lower bound (timezone-aware UTC datetime).
            end_timestamp:   Optional inclusive upper bound (timezone-aware UTC datetime).
            unseen:          If True restrict to unread mail (default False for daily sweep).
            account_id:      Optional Gmail OAuth account selector.

        Returns:
            List of email dicts each containing metadata + summary/action_items/importance
            from the email_parser agent.  Only importance >= 5 messages are returned.
        """
        start_ts = int(start_timestamp.timestamp())
        end_ts = int(end_timestamp.timestamp()) + 1 if end_timestamp else None

        gmail_client = self._get_gmail_client(account_id)
        resolved_account_id = str(getattr(gmail_client, "account_id", "") or "").strip() or "unknown_account"

        query = gmail_client.build_query(
            start_date=None,
            end_date=None,
            start_timestamp=start_ts,
            end_timestamp=end_ts,
            unseen=unseen,
            search_string="in:inbox",
        )

        hits = gmail_client.search_emails(query, max_results=100)
        logger.info(
            "[fetch_and_filter_emails] query=%r hits=%d start=%s end=%s unseen=%s",
            query,
            len(hits),
            start_timestamp.isoformat(),
            end_timestamp.isoformat() if end_timestamp else None,
            unseen,
        )

        if not hits:
            return []

        # Parse and timestamp-filter before LLM calls.
        candidates: list[tuple[dict, dict, dict]] = []
        skipped_outside_range = 0
        for hit in hits:
            full_email = gmail_client.fetch_full_email(hit["uid"])
            if not full_email:
                continue
            email_message = message_from_string(full_email["raw_email"])
            email_metadata = EmailProcessor.extract_metadata(email_message)

            date_received_str = email_metadata.get("date_received", "")
            if date_received_str and date_received_str != "[No Date]":
                try:
                    email_dt = parsedate_to_datetime(date_received_str)
                    if email_dt.tzinfo is None:
                        email_dt = email_dt.replace(tzinfo=timezone.utc)
                    if email_dt < start_timestamp:
                        skipped_outside_range += 1
                        continue
                    if end_timestamp and email_dt > end_timestamp:
                        skipped_outside_range += 1
                        continue
                except Exception as e:
                    logger.warning(
                        "[fetch_and_filter_emails] Could not parse date for %s: %s — including anyway.",
                        hit["uid"],
                        e,
                    )
            candidates.append((hit, full_email, email_metadata))

        logger.debug(
            "[fetch_and_filter_emails] after_timestamp_filter=%d skipped_outside_range=%d",
            len(candidates),
            skipped_outside_range,
        )

        # LLM triage — no session open during these calls.
        summary_agent = DI.agent_factory.create_agent("email_parser")
        results: list[dict] = []
        skipped_low_importance = 0
        skipped_missing_importance = 0

        for hit, full_email, email_metadata in candidates:
            email_message = message_from_string(full_email["raw_email"])
            email_body = EmailProcessor.extract_email_body(email_message)

            agent_result = summary_agent.action_handler(Message(agent_input=email_body, scope_context=_EMAIL_PARSER_SCOPE))
            summary_data = agent_result.data if hasattr(agent_result, "data") else agent_result

            email_data = {
                **email_metadata,
                **summary_data,
                "uid": hit["uid"],
                "message_id": hit["uid"],
                "thread_id": str(hit.get("threadId") or ""),
                "data_type": "email",
                "account_id": resolved_account_id,
            }

            importance_raw = email_data.get("importance")
            if importance_raw is None:
                skipped_missing_importance += 1
                logger.error(
                    "[fetch_and_filter_emails] Missing importance for %s subject=%r — skipping.",
                    hit["uid"],
                    email_data.get("subject", ""),
                )
                continue

            try:
                importance = int(importance_raw)
            except (ValueError, TypeError) as e:
                skipped_missing_importance += 1
                logger.error(
                    "[fetch_and_filter_emails] Invalid importance %r for %s: %s — skipping.",
                    importance_raw,
                    hit["uid"],
                    e,
                )
                logger.debug("[fetch_and_filter_emails] importance parse exception details", exc_info=True)
                continue

            if importance < 5:
                skipped_low_importance += 1
                logger.debug(
                    "[fetch_and_filter_emails] Skipping %s importance=%d subject=%r",
                    hit["uid"],
                    importance,
                    email_data.get("subject", ""),
                )
                continue

            results.append(email_data)

        logger.info(
            "[fetch_and_filter_emails] returned=%d skipped_low_importance=%d "
            "skipped_missing_importance=%d skipped_outside_range=%d account_id=%s",
            len(results),
            skipped_low_importance,
            skipped_missing_importance,
            skipped_outside_range,
            resolved_account_id,
        )
        return results

    def fetch_full_thread(
        self,
        *,
        thread_id: str,
        max_messages: int = 100,
        include_body: bool = True,
        account_id: str | None = None,
    ) -> list[dict]:
        """
        Fetch an ordered full email thread by Gmail thread id.
        """
        thread_id = str(thread_id or "").strip()
        if not thread_id:
            raise ValueError("thread_id is required")

        bounded_max = max(1, min(int(max_messages), 500))
        gmail_client = self._get_gmail_client(account_id)
        items = gmail_client.fetch_thread_messages(thread_id, max_messages=bounded_max)
        if not items:
            return []

        out: list[dict] = []
        for item in items:
            msg_id = str(item.get("id") or "").strip()
            headers = item.get("headers") if isinstance(item.get("headers"), dict) else {}
            row = {
                "message_id": msg_id,
                "thread_id": str(item.get("threadId") or thread_id),
                "internal_date_ms": str(item.get("internalDate") or ""),
                "sender_raw": str(headers.get("from") or ""),
                "to_raw": str(headers.get("to") or ""),
                "cc_raw": str(headers.get("cc") or ""),
                "bcc_raw": str(headers.get("bcc") or ""),
                "subject": str(headers.get("subject") or ""),
                "date_received": str(headers.get("date") or ""),
                "internet_message_id": str(headers.get("message-id") or ""),
                "in_reply_to": str(headers.get("in-reply-to") or ""),
                "references": str(headers.get("references") or ""),
                "snippet": str(item.get("snippet") or ""),
            }
            if include_body and msg_id:
                full_email = gmail_client.fetch_full_email(msg_id)
                if full_email and isinstance(full_email.get("raw_email"), str):
                    try:
                        email_message = message_from_string(full_email["raw_email"])
                        body = EmailProcessor.extract_email_body(email_message)
                        metadata = EmailProcessor.extract_metadata(email_message)
                        row["body"] = body
                        row["sender"] = metadata.get("sender", "")
                        row["email_address"] = metadata.get("email_address", "")
                    except Exception as e:
                        logger.error("Failed parsing full email body for message %s: %s", msg_id, e)
                        logger.debug("fetch_full_thread parse exception details", exc_info=True)
                        row["body"] = ""
                else:
                    row["body"] = ""
            out.append(row)
        return out
    
    def _convert_to_gmail_date(self, imap_date_str):
        """
        Convert IMAP date format (DD-MMM-YYYY) to Gmail format (YYYY/MM/DD)
        
        Args:
            imap_date_str: Date string in IMAP format (e.g., "15-Nov-2024")
            
        Returns:
            Date string in Gmail format (e.g., "2024/11/15")
        """
        try:
            dt = datetime.strptime(imap_date_str, "%d-%b-%Y")
            return dt.strftime("%Y/%m/%d")
        except Exception as e:
            logger.warning(f"Could not convert date '{imap_date_str}': {e}. Using as-is.")
            return imap_date_str

