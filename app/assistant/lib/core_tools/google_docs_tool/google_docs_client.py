"""
Google Docs API client using OAuth2 credentials.

Provides read and structured-write operations on Google Docs documents.
All methods raise on auth failures (FileNotFoundError / PermissionError)
so callers get a clear signal to surface re-auth instructions to the user.
"""
from __future__ import annotations

from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.assistant.lib.google_auth.account_ids import DEFAULT_GOOGLE_ACCOUNT_ID
from app.assistant.lib.google_auth.google_credentials import load_google_credentials
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

DOCS_SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
]

DOCS_WRITE_SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
]


def _auth_error(status: int, context: str, exc: HttpError) -> FileNotFoundError | None:
    """Return a structured FileNotFoundError for auth failures, else None."""
    if status == 401:
        return FileNotFoundError(
            f"Google OAuth token expired during {context}. "
            "Re-authenticate via the app Google OAuth flow."
        )
    if status == 403 and "insufficient" in str(exc).lower():
        return FileNotFoundError(
            f"Google OAuth token missing required scopes during {context}. "
            "Re-authenticate and grant Docs + Drive permissions."
        )
    return None


class GoogleDocsClient:
    """
    Thin wrapper around the Google Docs v1 and Drive v3 APIs.

    Instantiate once per request; do not cache across LLM calls.
    """

    def __init__(self, *, account_id: str | None = None, readonly: bool = False):
        self.account_id = str(account_id or "").strip() or DEFAULT_GOOGLE_ACCOUNT_ID
        scopes = DOCS_SCOPES if readonly else DOCS_WRITE_SCOPES
        creds = load_google_credentials(account_id=self.account_id, required_scopes=scopes)
        self._docs = build("docs", "v1", credentials=creds)
        self._drive = build("drive", "v3", credentials=creds)
        self._creds = creds

        # Resolve the human-readable email for this account so callers can surface it.
        try:
            from app.assistant.lib.google_auth.oauth_account_store import get_google_oauth_account_store
            stored = get_google_oauth_account_store().get_credentials(account_id=self.account_id)
            self.principal_email: str = str((stored or {}).get("principal_email") or "").strip()
        except Exception as e:
            logger.debug("GoogleDocsClient could not resolve principal_email: %s", e)
            self.principal_email = ""

        logger.info(
            "GoogleDocsClient initialized account_id=%s email=%s readonly=%s",
            self.account_id,
            self.principal_email or "(unknown)",
            readonly,
        )

    # ------------------------------------------------------------------
    # READ
    # ------------------------------------------------------------------

    def get_document(self, document_id: str) -> dict[str, Any]:
        """
        Fetch the full document resource from the Docs API.

        Returns the raw API response dict (title, body, revisionId, etc.).
        """
        document_id = str(document_id or "").strip()
        if not document_id:
            raise ValueError("document_id is required.")
        try:
            doc = self._docs.documents().get(documentId=document_id).execute()
            logger.debug(
                "GoogleDocsClient.get_document document_id=%s title=%r",
                document_id,
                doc.get("title"),
            )
            return doc
        except HttpError as e:
            err = _auth_error(e.resp.status, "get_document", e)
            if err:
                raise err
            if e.resp.status == 404:
                logger.debug("get_document: doc not found document_id=%s", document_id)
            else:
                logger.error("get_document HTTP error %s for doc %s: %s", e.resp.status, document_id, e)
                logger.debug("get_document HTTP exception details", exc_info=True)
            raise

    def extract_plain_text(self, doc: dict[str, Any]) -> str:
        """
        Extract all plain text from a Docs API document resource.

        Concatenates paragraph text runs in reading order.
        """
        parts: list[str] = []
        body = doc.get("body") if isinstance(doc.get("body"), dict) else {}
        content = body.get("content") if isinstance(body.get("content"), list) else []
        for structural_element in content:
            if not isinstance(structural_element, dict):
                continue
            paragraph = structural_element.get("paragraph")
            if not isinstance(paragraph, dict):
                continue
            for elem in paragraph.get("elements") or []:
                if not isinstance(elem, dict):
                    continue
                text_run = elem.get("textRun")
                if not isinstance(text_run, dict):
                    continue
                chunk = str(text_run.get("content") or "")
                parts.append(chunk)
        return "".join(parts)

    def get_document_metadata(self, document_id: str) -> dict[str, Any]:
        """
        Fetch lightweight document metadata via the Drive API (name, mimeType,
        createdTime, modifiedTime, owners, size).
        """
        document_id = str(document_id or "").strip()
        if not document_id:
            raise ValueError("document_id is required.")
        try:
            meta = self._drive.files().get(
                fileId=document_id,
                fields="id,name,mimeType,createdTime,modifiedTime,owners,size,webViewLink",
            ).execute()
            return meta
        except HttpError as e:
            err = _auth_error(e.resp.status, "get_document_metadata", e)
            if err:
                raise err
            logger.error(
                "get_document_metadata HTTP error %s for doc %s: %s",
                e.resp.status,
                document_id,
                e,
            )
            logger.debug("get_document_metadata HTTP exception details", exc_info=True)
            raise

    def find_documents(
        self,
        *,
        name_contains: str | None = None,
        max_results: int = 20,
    ) -> list[dict[str, Any]]:
        """
        Search for Google Docs documents visible to the authenticated user.

        Args:
            name_contains: Optional substring to filter by document name.
            max_results:   Max files to return (capped at 100).

        Returns list of {id, name, modifiedTime, webViewLink}.
        """
        bounded = max(1, min(int(max_results), 100))
        q_parts = ["mimeType='application/vnd.google-apps.document'", "trashed=false"]
        if name_contains:
            safe = name_contains.replace("'", "\\'")
            q_parts.append(f"name contains '{safe}'")
        q = " and ".join(q_parts)
        try:
            resp = self._drive.files().list(
                q=q,
                pageSize=bounded,
                fields="files(id,name,modifiedTime,webViewLink)",
                orderBy="modifiedTime desc",
            ).execute()
            return resp.get("files") or []
        except HttpError as e:
            err = _auth_error(e.resp.status, "find_documents", e)
            if err:
                raise err
            logger.error("find_documents HTTP error %s: %s", e.resp.status, e)
            logger.debug("find_documents HTTP exception details", exc_info=True)
            raise

    # ------------------------------------------------------------------
    # CREATE
    # ------------------------------------------------------------------

    def create_document(self, title: str) -> dict[str, Any]:
        """Create a new blank Google Doc and return its metadata.

        Returns a dict with ``document_id``, ``title``, and ``web_view_link``.
        """
        title = str(title or "").strip()
        if not title:
            raise ValueError("title is required to create a document.")
        try:
            doc = self._docs.documents().create(body={"title": title}).execute()
            document_id = doc.get("documentId", "")
            logger.info(
                "GoogleDocsClient.create_document title=%r document_id=%s",
                title,
                document_id,
            )
            web_view_link = f"https://docs.google.com/document/d/{document_id}/edit"
            return {
                "document_id": document_id,
                "title": doc.get("title", title),
                "web_view_link": web_view_link,
            }
        except HttpError as e:
            err = _auth_error(e.resp.status, "create_document", e)
            if err:
                raise err
            logger.error("create_document HTTP error %s: %s", e.resp.status, e)
            logger.debug("create_document HTTP exception details", exc_info=True)
            raise

    # ------------------------------------------------------------------
    # WRITE
    # ------------------------------------------------------------------

    def append_text(
        self,
        document_id: str,
        text: str,
        *,
        ensure_newline: bool = True,
    ) -> dict[str, Any]:
        """
        Append text to the end of a document.

        Args:
            document_id:    Target document.
            text:           Text to append.
            ensure_newline: Prepend a newline before the text if the document
                            does not already end with one.

        Returns the batchUpdate response.
        """
        document_id = str(document_id or "").strip()
        if not document_id:
            raise ValueError("document_id is required.")
        if not text:
            raise ValueError("text to append must be non-empty.")

        doc = self.get_document(document_id)
        end_index = self._doc_end_index(doc)

        insert_text = ("\n" + text) if ensure_newline else text

        requests = [
            {
                "insertText": {
                    "location": {"index": end_index},
                    "text": insert_text,
                }
            }
        ]
        return self._batch_update(document_id, requests)

    def replace_all_text(
        self,
        document_id: str,
        find: str,
        replace_with: str,
        *,
        match_case: bool = True,
    ) -> dict[str, Any]:
        """
        Find-and-replace all occurrences of a string in the document.

        Returns the batchUpdate response (includes occurrencesChanged count).
        """
        document_id = str(document_id or "").strip()
        if not document_id:
            raise ValueError("document_id is required.")
        if not find:
            raise ValueError("find string must be non-empty.")
        requests = [
            {
                "replaceAllText": {
                    "containsText": {"text": find, "matchCase": match_case},
                    "replaceText": replace_with,
                }
            }
        ]
        return self._batch_update(document_id, requests)

    def insert_text_at_index(
        self,
        document_id: str,
        text: str,
        index: int,
    ) -> dict[str, Any]:
        """
        Insert text at a specific character index in the document.

        Use get_document() first to inspect the document structure and determine
        the correct index.  Index 1 is the start of the document body.
        """
        document_id = str(document_id or "").strip()
        if not document_id:
            raise ValueError("document_id is required.")
        if not text:
            raise ValueError("text must be non-empty.")
        if index < 1:
            raise ValueError("index must be >= 1.")
        requests = [
            {"insertText": {"location": {"index": index}, "text": text}}
        ]
        return self._batch_update(document_id, requests)

    def delete_content_range(
        self,
        document_id: str,
        start_index: int,
        end_index: int,
    ) -> dict[str, Any]:
        """
        Delete a range of content by character index (startIndex inclusive,
        endIndex exclusive).
        """
        document_id = str(document_id or "").strip()
        if not document_id:
            raise ValueError("document_id is required.")
        if start_index < 1:
            raise ValueError("start_index must be >= 1.")
        if end_index <= start_index:
            raise ValueError("end_index must be > start_index.")
        requests = [
            {
                "deleteContentRange": {
                    "range": {"startIndex": start_index, "endIndex": end_index}
                }
            }
        ]
        return self._batch_update(document_id, requests)

    def replace_entire_body(
        self,
        document_id: str,
        new_text: str,
    ) -> dict[str, Any]:
        """Atomically replace the entire document body with new_text.

        Google Docs API's replaceAllText caps the search text at ~2048 chars,
        so it silently no-ops on larger wholesale replacements. This method
        uses deleteContentRange + insertText, which has no length limit.
        """
        document_id = str(document_id or "").strip()
        if not document_id:
            raise ValueError("document_id is required.")

        doc = self.get_document(document_id)
        body = doc.get("body") if isinstance(doc, dict) else None
        content = body.get("content") if isinstance(body, dict) else None
        if not isinstance(content, list) or not content:
            # Empty doc — just insert.
            if new_text:
                return self.insert_text_at_index(document_id, new_text, 1)
            return {}

        # Last element's endIndex is the doc-length marker. The body always
        # ends with a trailing newline at endIndex-1, which can't be deleted.
        doc_end_index = max(
            int(el.get("endIndex", 0) or 0) for el in content if isinstance(el, dict)
        )
        # Range is [start, end) with end exclusive; must stop at end-1 to keep
        # the final newline Google Docs requires.
        delete_end = max(2, doc_end_index - 1)

        requests: list[dict] = []
        if delete_end > 1:
            requests.append(
                {
                    "deleteContentRange": {
                        "range": {"startIndex": 1, "endIndex": delete_end}
                    }
                }
            )
        if new_text:
            requests.append(
                {"insertText": {"location": {"index": 1}, "text": new_text}}
            )
        if not requests:
            return {}
        return self._batch_update(document_id, requests)

    # ------------------------------------------------------------------
    # INTERNAL
    # ------------------------------------------------------------------

    def _batch_update(self, document_id: str, requests: list[dict]) -> dict[str, Any]:
        try:
            resp = self._docs.documents().batchUpdate(
                documentId=document_id,
                body={"requests": requests},
            ).execute()
            logger.debug(
                "GoogleDocsClient.batchUpdate document_id=%s requests=%d",
                document_id,
                len(requests),
            )
            return resp
        except HttpError as e:
            err = _auth_error(e.resp.status, "batchUpdate", e)
            if err:
                raise err
            logger.error(
                "batchUpdate HTTP error %s for doc %s: %s", e.resp.status, document_id, e
            )
            logger.debug("batchUpdate HTTP exception details", exc_info=True)
            raise

    @staticmethod
    def _doc_end_index(doc: dict[str, Any]) -> int:
        """Return the last valid insert index in the document body."""
        body = doc.get("body") if isinstance(doc.get("body"), dict) else {}
        content = body.get("content") if isinstance(body.get("content"), list) else []
        if not content:
            return 1
        last = content[-1]
        if not isinstance(last, dict):
            return 1
        end_idx = last.get("endIndex")
        # endIndex is exclusive and includes the trailing newline; insert before it
        return max(1, int(end_idx or 1) - 1)
