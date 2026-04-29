# app/assistant/lib/core_tools/email_tool/utils/gmail_api_client.py
"""
Gmail API client using OAuth2 credentials
"""
import base64
import mimetypes
import os
import time
from email import encoders
from email.mime.audio import MIMEAudio
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List, Optional, Sequence

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from app.assistant.utils.logging_config import get_logger
from app.assistant.lib.google_auth.account_ids import GMAIL_GOOGLE_ACCOUNT_ID
from app.assistant.lib.google_auth.google_credentials import load_google_credentials

logger = get_logger(__name__)

# Gmail API scopes
SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.send',
    'https://www.googleapis.com/auth/gmail.modify'  # Required to mark emails as read
]

class GmailAPIClient:
    """Gmail API client using OAuth2"""
    
    def __init__(self, *, account_id: str | None = None):
        self.service = None
        self.account_id = str(account_id or "").strip() or GMAIL_GOOGLE_ACCOUNT_ID
        self._authenticate()
    
    def _authenticate(self):
        """Authenticate with Gmail API using OAuth2"""
        creds = load_google_credentials(
            account_id=self.account_id,
            required_scopes=SCOPES,
        )
        
        try:
            self.service = build('gmail', 'v1', credentials=creds)
            logger.info("Gmail API service initialized successfully for account_id=%s.", self.account_id)
        except Exception as e:
            logger.error(f"Failed to create Gmail service: {e}")
            raise
    
    def search_emails(self, query, max_results=100):
        """
        Search emails using Gmail query syntax
        
        Args:
            query: Gmail query string (e.g., "is:unread after:2024/01/01")
            max_results: Maximum number of emails to return
            
        Returns:
            List of email metadata dicts with keys: uid, subject, from, date
        """
        try:
            logger.debug(
                "Gmail API call users.messages.list userId='me' query=%r maxResults=%s",
                query,
                max_results,
            )
            
            results = self.service.users().messages().list(
                userId='me',
                q=query,
                maxResults=max_results
            ).execute()
            
            messages = results.get('messages', [])
            
            logger.debug(
                "Gmail API response users.messages.list count=%d",
                len(messages),
            )
            
            email_list = []
            for msg in messages:
                email_list.append({
                    'uid': msg['id'],
                    'threadId': msg.get('threadId', '')
                })
            
            return email_list
            
        except HttpError as e:
            if e.resp.status == 401:
                logger.error("❌ Gmail authentication failed (401 UNAUTHENTICATED)")
                logger.error(f"   Error details: {e.error_details if hasattr(e, 'error_details') else e}")
                logger.error("   ⚠️  Please re-authenticate through the Features settings page")
                raise FileNotFoundError(
                    "Gmail authentication expired or invalid. Please re-authenticate through the Features settings page."
                )
            elif e.resp.status == 403 and "insufficient" in str(e).lower():
                logger.error("❌ Gmail authentication has insufficient scopes (403)")
                logger.error(f"   Error details: {e}")
                logger.error("   ⚠️  The token doesn't have required permissions (gmail.readonly, gmail.send, gmail.modify)")
                logger.error("   ⚠️  Deleting token - please re-authenticate through the Features settings page")
                raise FileNotFoundError(
                    "Gmail token has insufficient permissions. Please re-authenticate through the Features settings page to grant all required scopes."
                )
            else:
                logger.error(f"Gmail API HTTP error ({e.resp.status}): {e}")
                return []
        except Exception as e:
            logger.error(f"Error searching Gmail: {e}")
            return []
    
    def fetch_full_email(self, message_id):
        """
        Fetch full email content by message ID
        
        Args:
            message_id: Gmail message ID
            
        Returns:
            Dict with keys: uid, raw_email (full RFC822 format)
        """
        try:
            message = self.service.users().messages().get(
                userId='me',
                id=message_id,
                format='raw'
            ).execute()
            
            # Decode the raw message
            raw_email = base64.urlsafe_b64decode(message['raw']).decode('utf-8')
            
            return {
                'uid': message_id,
                'raw_email': raw_email
            }
            
        except HttpError as e:
            if e.resp.status == 401:
                logger.error("❌ Gmail authentication failed (401 UNAUTHENTICATED) while fetching email")
                raise FileNotFoundError(
                    "Gmail authentication expired or invalid. Please re-authenticate through the Features settings page."
                )
            elif e.resp.status == 403 and "insufficient" in str(e).lower():
                logger.error("❌ Gmail token has insufficient scopes (403) while fetching email")
                raise FileNotFoundError(
                    "Gmail token has insufficient permissions. Please re-authenticate through the Features settings page."
                )
            else:
                logger.error(f"Gmail API HTTP error ({e.resp.status}) fetching email {message_id}: {e}")
                logger.debug("Gmail fetch_full_email HTTP exception details", exc_info=True)
                return None
        except Exception as e:
            logger.error(f"Error fetching email {message_id}: {e}")
            logger.debug("Gmail fetch_full_email exception details", exc_info=True)
            return None

    def get_thread_id_for_message(self, message_id: str) -> str:
        """
        Resolve Gmail threadId for a message id.
        Raises on auth failures; returns empty string when not found.
        """
        try:
            message = self.service.users().messages().get(
                userId='me',
                id=message_id,
                format='minimal',
            ).execute()
            return str(message.get('threadId') or '').strip()
        except HttpError as e:
            if e.resp.status == 401:
                logger.error("❌ Gmail authentication failed (401 UNAUTHENTICATED) while resolving thread id")
                logger.debug("Gmail get_thread_id_for_message HTTP exception details", exc_info=True)
                raise FileNotFoundError(
                    "Gmail authentication expired or invalid. Please re-authenticate through the Features settings page."
                )
            if e.resp.status == 403 and "insufficient" in str(e).lower():
                logger.error("❌ Gmail token has insufficient scopes (403) while resolving thread id")
                logger.debug("Gmail get_thread_id_for_message HTTP exception details", exc_info=True)
                raise FileNotFoundError(
                    "Gmail token has insufficient permissions. Please re-authenticate through the Features settings page."
                )
            logger.error("Gmail API HTTP error (%s) resolving thread for message %s: %s", e.resp.status, message_id, e)
            logger.debug("Gmail get_thread_id_for_message HTTP exception details", exc_info=True)
            return ""
        except Exception as e:
            logger.error("Error resolving thread for message %s: %s", message_id, e)
            logger.debug("Gmail get_thread_id_for_message exception details", exc_info=True)
            return ""

    def fetch_thread_messages(self, thread_id: str, *, max_messages: int = 200) -> list[dict]:
        """
        Fetch a Gmail thread and return ordered lightweight message metadata.
        """
        try:
            thread = self.service.users().threads().get(
                userId='me',
                id=thread_id,
                format='metadata',
                metadataHeaders=[
                    'From',
                    'To',
                    'Cc',
                    'Bcc',
                    'Subject',
                    'Date',
                    'Message-ID',
                    'In-Reply-To',
                    'References',
                ],
            ).execute()
            raw_messages = thread.get('messages', [])
            if not isinstance(raw_messages, list):
                return []
            out: list[dict] = []
            for msg in raw_messages:
                if not isinstance(msg, dict):
                    continue
                payload = msg.get('payload') if isinstance(msg.get('payload'), dict) else {}
                headers = payload.get('headers') if isinstance(payload.get('headers'), list) else []
                header_map: dict[str, str] = {}
                for h in headers:
                    if not isinstance(h, dict):
                        continue
                    name = str(h.get('name') or '').strip()
                    value = str(h.get('value') or '').strip()
                    if name:
                        header_map[name.lower()] = value
                out.append(
                    {
                        'id': str(msg.get('id') or '').strip(),
                        'threadId': str(msg.get('threadId') or '').strip(),
                        'internalDate': str(msg.get('internalDate') or '').strip(),
                        'snippet': str(msg.get('snippet') or ''),
                        'headers': header_map,
                    }
                )
            out.sort(key=lambda m: int(m.get('internalDate') or 0))
            if max_messages > 0 and len(out) > max_messages:
                out = out[-max_messages:]
            return out
        except HttpError as e:
            if e.resp.status == 401:
                logger.error("❌ Gmail authentication failed (401 UNAUTHENTICATED) while fetching thread")
                logger.debug("Gmail fetch_thread_messages HTTP exception details", exc_info=True)
                raise FileNotFoundError(
                    "Gmail authentication expired or invalid. Please re-authenticate through the Features settings page."
                )
            if e.resp.status == 403 and "insufficient" in str(e).lower():
                logger.error("❌ Gmail token has insufficient scopes (403) while fetching thread")
                logger.debug("Gmail fetch_thread_messages HTTP exception details", exc_info=True)
                raise FileNotFoundError(
                    "Gmail token has insufficient permissions. Please re-authenticate through the Features settings page."
                )
            logger.error("Gmail API HTTP error (%s) fetching thread %s: %s", e.resp.status, thread_id, e)
            logger.debug("Gmail fetch_thread_messages HTTP exception details", exc_info=True)
            return []
        except Exception as e:
            logger.error("Error fetching thread %s: %s", thread_id, e)
            logger.debug("Gmail fetch_thread_messages exception details", exc_info=True)
            return []
    
    def search_emails_metadata(
        self,
        query: str,
        max_results: int = 5000,
        *,
        metadata_sample_size: int = 500,
    ) -> dict:
        """
        Search emails: count ALL matching IDs, fetch metadata for a sample.

        Paginates ``messages.list`` to collect up to *max_results* message IDs
        (cheap — only IDs returned).  Then fetches full headers for the first
        *metadata_sample_size* messages using batched ``messages.get`` calls.

        Args:
            query:                Gmail query string.
            max_results:          Cap on IDs collected (for total count).
            metadata_sample_size: How many messages to fetch headers for
                                  (default 500, keeps API calls <=5 batches).

        Returns:
            ``{"total": int, "total_threads": int, "rows": list[dict], "sampled": int}``
            where *total* is the true match count (up to max_results),
            *rows* has metadata only for the sampled subset.
        """
        try:
            bounded = max(1, min(int(max_results), 50_000))
            sample_cap = max(1, min(int(metadata_sample_size), 1000))
            logger.debug(
                "Gmail search_emails_metadata query=%r max_results=%s sample=%s",
                query,
                bounded,
                sample_cap,
            )

            all_ids: list[tuple[str, str]] = []
            thread_ids: set[str] = set()
            page_token: str | None = None
            while len(all_ids) < bounded:
                page_size = min(500, bounded - len(all_ids))
                req = self.service.users().messages().list(
                    userId="me",
                    q=query,
                    maxResults=page_size,
                    **({"pageToken": page_token} if page_token else {}),
                )
                list_resp = req.execute()
                page_msgs = list_resp.get("messages") or []
                for m in page_msgs:
                    if not isinstance(m, dict):
                        continue
                    msg_id = str(m.get("id") or "").strip()
                    tid = str(m.get("threadId") or "").strip()
                    if msg_id:
                        all_ids.append((msg_id, tid))
                        if tid:
                            thread_ids.add(tid)
                page_token = list_resp.get("nextPageToken")
                if not page_token or not page_msgs:
                    break

            total = len(all_ids)
            sample_ids = all_ids[:sample_cap]

            rows: list[dict] = []
            detail_by_id: dict[str, dict] = {}
            # Google internally parallelizes sub-requests within a BatchHttpRequest,
            # so even a batch of 25 still fires ~25 concurrent fetches and trips the
            # "Too many concurrent requests for user" (429) cap at roughly 10-20% of
            # sub-requests. The per-user concurrent limit sits near 10, so we stay
            # under it: batch=10, and a 300ms pause between batches lets the in-flight
            # ones drain.
            _BATCH_SIZE = 10
            _INTER_BATCH_SLEEP_S = 0.3

            def _batch_callback(request_id, response, exception):
                if exception is not None:
                    logger.error(
                        "search_emails_metadata: batch fetch error for %s: %s",
                        request_id,
                        exception,
                    )
                    return
                detail_by_id[request_id] = response

            total_chunks = (len(sample_ids) + _BATCH_SIZE - 1) // _BATCH_SIZE
            for chunk_idx, chunk_start in enumerate(range(0, len(sample_ids), _BATCH_SIZE)):
                chunk = sample_ids[chunk_start : chunk_start + _BATCH_SIZE]

                # BatchHttpRequest.execute() does NOT accept num_retries (only
                # HttpRequest.execute() does). We handle transient 429/5xx at the
                # batch level with a small retry-with-backoff loop.
                max_attempts = 5
                backoff_s = 0.5
                for attempt in range(1, max_attempts + 1):
                    batch = self.service.new_batch_http_request(callback=_batch_callback)
                    for msg_id, _ in chunk:
                        batch.add(
                            self.service.users().messages().get(
                                userId="me",
                                id=msg_id,
                                format="metadata",
                                metadataHeaders=["From", "To", "Subject", "Date"],
                            ),
                            request_id=msg_id,
                        )
                    try:
                        batch.execute()
                        break
                    except HttpError as e:
                        status = getattr(getattr(e, "resp", None), "status", None)
                        transient = status in (429, 500, 502, 503, 504)
                        if attempt >= max_attempts or not transient:
                            logger.error(
                                "search_emails_metadata: batch.execute failed (status=%s, attempt=%d/%d): %s",
                                status, attempt, max_attempts, e,
                            )
                            raise
                        logger.warning(
                            "search_emails_metadata: batch.execute transient error (status=%s, attempt=%d/%d); retrying in %.2fs",
                            status, attempt, max_attempts, backoff_s,
                        )
                        time.sleep(backoff_s)
                        backoff_s *= 2

                # Small pause between batches to stay well under Google's per-user
                # concurrency ceiling — cheap insurance against sustained bursts.
                if chunk_idx + 1 < total_chunks:
                    time.sleep(_INTER_BATCH_SLEEP_S)

            for msg_id, thread_id in sample_ids:
                detail = detail_by_id.get(msg_id)
                if detail is None:
                    continue
                payload = detail.get("payload") if isinstance(detail.get("payload"), dict) else {}
                headers = payload.get("headers") if isinstance(payload.get("headers"), list) else []
                hmap: dict[str, str] = {}
                for h in headers:
                    if not isinstance(h, dict):
                        continue
                    name = str(h.get("name") or "").strip().lower()
                    value = str(h.get("value") or "").strip()
                    if name:
                        hmap[name] = value
                rows.append(
                    {
                        "uid": msg_id,
                        "threadId": thread_id,
                        "internalDate": str(detail.get("internalDate") or ""),
                        "subject": hmap.get("subject", ""),
                        "from": hmap.get("from", ""),
                        "to": hmap.get("to", ""),
                        "date": hmap.get("date", ""),
                    }
                )
            logger.info(
                "search_emails_metadata: total=%d sampled=%d rows=%d query=%r",
                total,
                len(sample_ids),
                len(rows),
                query,
            )
            return {
                "total": total,
                "total_threads": len(thread_ids),
                "rows": rows,
                "sampled": len(rows),
            }
        except HttpError as e:
            if e.resp.status == 401:
                logger.error(
                    "Gmail authentication failed (401) in search_emails_metadata"
                )
                logger.debug("search_emails_metadata 401 exception details", exc_info=True)
                raise FileNotFoundError(
                    "Gmail authentication expired. Re-authenticate via the app."
                )
            if e.resp.status == 403 and "insufficient" in str(e).lower():
                logger.error(
                    "Gmail token insufficient scopes (403) in search_emails_metadata"
                )
                logger.debug("search_emails_metadata 403 exception details", exc_info=True)
                raise FileNotFoundError(
                    "Gmail token has insufficient permissions. Re-authenticate via the app."
                )
            logger.error("Gmail API HTTP error (%s) in search_emails_metadata: %s", e.resp.status, e)
            logger.debug("search_emails_metadata HTTP exception details", exc_info=True)
            raise
        except Exception as e:
            logger.error("search_emails_metadata failed: %s", e)
            logger.debug("search_emails_metadata exception details", exc_info=True)
            raise

    def send_email(self, to, subject, body, attachment_paths: Optional[Sequence[str]] = None):
        """
        Send an email via Gmail API. Supports optional file attachments.

        Args:
            to: Recipient email address
            subject: Email subject
            body: Email body (plain text)
            attachment_paths: Optional list of absolute paths to files to
                attach. Each path must exist; missing/unreadable files
                cause the whole send to fail (callers should resolve
                pod_ids → paths upstream and pre-validate). MIME type is
                inferred from extension.

        Returns:
            Dict with message ID if successful, None otherwise
        """
        try:
            paths: List[str] = [p for p in (attachment_paths or []) if p]
            if not paths:
                # Plain text path — single MIMEText, smaller wire size.
                message = MIMEText(body)
            else:
                # Multipart with attachments.
                message = MIMEMultipart()
                message.attach(MIMEText(body, "plain"))
                for path in paths:
                    if not os.path.isfile(path):
                        raise FileNotFoundError(f"attachment not found: {path}")
                    ctype, encoding = mimetypes.guess_type(path)
                    if ctype is None or encoding is not None:
                        ctype = "application/octet-stream"
                    maintype, subtype = ctype.split("/", 1)
                    with open(path, "rb") as fh:
                        data = fh.read()
                    if maintype == "image":
                        part = MIMEImage(data, _subtype=subtype)
                    elif maintype == "audio":
                        part = MIMEAudio(data, _subtype=subtype)
                    elif maintype == "text":
                        part = MIMEText(data.decode("utf-8", errors="replace"), _subtype=subtype)
                    else:
                        part = MIMEBase(maintype, subtype)
                        part.set_payload(data)
                        encoders.encode_base64(part)
                    filename = os.path.basename(path)
                    part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
                    message.attach(part)

            message['to'] = to
            message['subject'] = subject

            raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode('utf-8')

            sent_message = self.service.users().messages().send(
                userId='me',
                body={'raw': raw_message}
            ).execute()

            n_atts = len(paths)
            logger.info(
                "Email sent successfully to %s (attachments=%d). Message ID: %s",
                to, n_atts, sent_message['id'],
            )
            return {'id': sent_message['id']}
            
        except HttpError as e:
            if e.resp.status == 401:
                logger.error("❌ Gmail authentication failed (401 UNAUTHENTICATED) while sending email")
                raise FileNotFoundError(
                    "Gmail authentication expired or invalid. Please re-authenticate through the Features settings page."
                )
            elif e.resp.status == 403 and "insufficient" in str(e).lower():
                logger.error("❌ Gmail token has insufficient scopes (403) while sending email")
                raise FileNotFoundError(
                    "Gmail token has insufficient permissions. Please re-authenticate through the Features settings page."
                )
            else:
                logger.error(f"Gmail API HTTP error ({e.resp.status}) sending email: {e}")
                return None
        except Exception as e:
            logger.error(f"Error sending email: {e}")
            return None
    
    def mark_as_read(self, message_id):
        """Mark an email as read"""
        logger.debug("mark_as_read called message_id=%s", message_id)
        
        try:
            result = self.service.users().messages().modify(
                userId='me',
                id=message_id,
                body={'removeLabelIds': ['UNREAD']}
            ).execute()
            logger.debug("Successfully marked email %s as read. API response: %s", message_id, result)
        except HttpError as e:
            if e.resp.status == 401:
                logger.error("❌ Gmail authentication failed (401 UNAUTHENTICATED) while marking email as read")
                raise FileNotFoundError(
                    "Gmail authentication expired or invalid. Please re-authenticate through the Features settings page."
                )
            elif e.resp.status == 403 and "insufficient" in str(e).lower():
                logger.error("❌ Gmail token has insufficient scopes (403) while marking email as read")
                logger.error("   ⚠️  Missing 'gmail.modify' scope - token was created before this scope was added")
                raise FileNotFoundError(
                    "Gmail token has insufficient permissions (missing gmail.modify). Please re-authenticate through the Features settings page."
                )
            else:
                logger.error(f"❌ Gmail API HTTP error ({e.resp.status}) marking email {message_id} as read: {e}")
                logger.error(f"   Error type: {type(e).__name__}")
                logger.error(f"   Error details: {str(e)}")
                raise  # Re-raise so caller can handle it
        except Exception as e:
            logger.error(f"❌ Error marking email {message_id} as read: {e}")
            logger.error(f"   Error type: {type(e).__name__}")
            logger.error(f"   Error details: {str(e)}")
            raise  # Re-raise so caller can handle it
    
    def batch_trash_by_sender(
        self,
        *,
        sender_email: str,
        extra_query: str = "",
        max_results: int = 1000,
    ) -> dict:
        """
        Move all messages from a sender to Trash using the Gmail batchModify endpoint.

        Args:
            sender_email: The exact sender address to target.
            extra_query:  Optional additional Gmail query clauses (e.g. "before:2024/01/01").
            max_results:  Cap on the number of messages trashed per call (max 1000).

        Returns:
            {"trashed": int, "message_ids": [...]}
        """
        # Email addresses (contain @) and bare domains (e.g. "quora.com") are
        # passed as-is so Gmail resolves them natively. Everything else is a
        # display-name phrase and gets quoted.
        stripped = sender_email.strip().lstrip("*@")
        looks_like_domain = "." in stripped and " " not in stripped and "@" not in stripped
        if "@" in sender_email or looks_like_domain:
            sender_term = stripped if looks_like_domain else sender_email
        else:
            sender_term = f'"{sender_email}"'
        query_parts = [f"from:{sender_term}"]
        if extra_query.strip():
            query_parts.append(extra_query.strip())
        query = " ".join(query_parts)

        bounded = max(1, min(int(max_results), 5000))
        logger.debug("batch_trash_by_sender query=%r max_results=%s", query, bounded)

        messages: list[dict] = []
        page_token: str | None = None
        try:
            while len(messages) < bounded:
                page_size = min(500, bounded - len(messages))
                req = self.service.users().messages().list(
                    userId="me",
                    q=query,
                    maxResults=page_size,
                    **({"pageToken": page_token} if page_token else {}),
                )
                list_resp = req.execute()
                page_msgs = list_resp.get("messages") or []
                messages.extend(page_msgs)
                page_token = list_resp.get("nextPageToken")
                if not page_token or not page_msgs:
                    break
        except HttpError as e:
            logger.error("batch_trash_by_sender: list failed: %s", e)
            logger.debug("batch_trash_by_sender list exception details", exc_info=True)
            raise

        if not messages:
            logger.info("batch_trash_by_sender: no messages found for query=%r", query)
            return {"trashed": 0, "message_ids": []}

        ids = [str(m.get("id") or "").strip() for m in messages if m.get("id")]
        logger.info(
            "batch_trash_by_sender: trashing %d message(s) from %s",
            len(ids),
            sender_email,
        )

        BATCH_MODIFY_LIMIT = 1000
        try:
            for i in range(0, len(ids), BATCH_MODIFY_LIMIT):
                chunk = ids[i : i + BATCH_MODIFY_LIMIT]
                self.service.users().messages().batchModify(
                    userId="me",
                    body={"ids": chunk, "addLabelIds": ["TRASH"], "removeLabelIds": ["INBOX"]},
                ).execute(num_retries=5)
        except HttpError as e:
            logger.error("batch_trash_by_sender: batchModify failed: %s", e)
            logger.debug("batch_trash_by_sender batchModify exception details", exc_info=True)
            raise

        logger.info("batch_trash_by_sender: successfully trashed %d message(s)", len(ids))
        return {"trashed": len(ids), "message_ids": ids}

    def build_query(self, start_date=None, end_date=None, start_timestamp=None, end_timestamp=None, unseen=False, search_string=None):
        """
        Build Gmail search query from parameters
        
        Args:
            start_date: Start date in format YYYY/MM/DD (deprecated, use start_timestamp)
            end_date: End date in format YYYY/MM/DD (deprecated, use end_timestamp)
            start_timestamp: Unix timestamp for start (preferred)
            end_timestamp: Unix timestamp for end (preferred)
            unseen: If True, search only unread emails
            search_string: Additional search terms
            
        Returns:
            Gmail query string
        """
        query_parts = []
        
        # Prefer unix timestamps over date strings (more precise)
        if start_timestamp:
            query_parts.append(f"after:{int(start_timestamp)}")
        elif start_date:
            query_parts.append(f"after:{start_date}")
        
        if end_timestamp:
            query_parts.append(f"before:{int(end_timestamp)}")
        elif end_date:
            query_parts.append(f"before:{end_date}")
        
        if unseen:
            query_parts.append("is:unread")
        
        if search_string:
            query_parts.append(search_string)
        
        query = " ".join(query_parts)
        logger.debug(f"Built Gmail query: {query}")
        return query

