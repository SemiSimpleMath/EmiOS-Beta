from __future__ import annotations

import threading
from pathlib import Path

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.identity_names import get_required_assistant_name
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root

logger = get_logger(__name__)


class DocCreateFinalRouterNode(ControlNode):
    """
    Deterministic router for doc-creation mode.

    Every turn:
      - Reads doc_markdown from the blackboard and emits a doc_draft_update
        WebSocket event so the split-panel preview stays live.
      - Auto-saves the markdown to docs/drafts/<session_id>.md (for md type).
      - Packs chat_response into the final_answer result.

    When doc_creation_done_tf is True:
      - For md type: writes the finalized doc to docs/<session_id>.md.
      - For gdoc type: pushes markdown to Google Docs (create or replace).
      - Emits a doc_created event with the doc_id and URL.
    """

    _DOC_WRITER_AGENT = "doc_editor::editor"
    _MAX_EDIT_RETRIES = 2

    # Shown on turn 1 when the doc writer no-ops (user typed /doc create with no description).
    _BLANK_DOC_PLACEHOLDER = "# New Document\n\n[Start describing your document and it will appear here…]"

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        cfg = self._cfg()

        final_node = self._required_str(cfg, "final_node")
        chat_response_key = self._optional_str(cfg, "chat_response_key", "chat_response")
        doc_done_key = self._optional_str(cfg, "doc_done_key", "doc_creation_done_tf")

        _ACCEPTED_PRIOR = frozenset({self._DOC_WRITER_AGENT, "doc_writer_pre_node"})
        last_agent = self.blackboard.get_state_value("last_agent", None)
        if not isinstance(last_agent, str) or last_agent not in _ACCEPTED_PRIOR:
            raise ValueError(
                f"[{self.name}] expected last_agent in {_ACCEPTED_PRIOR!r} before doc-create route, got: {last_agent!r}"
            )

        chat_response = str(self.blackboard.get_state_value(chat_response_key, "") or "").strip()
        if not chat_response:
            raise ValueError(
                f"[{self.name}] {chat_response_key!r} must be non-empty for doc-create response."
            )

        doc_action = str(self.blackboard.get_state_value("doc_action", "no_op") or "no_op").strip()
        raw_edits = self.blackboard.get_state_value("edits", []) or []

        current_doc = str(self.blackboard.get_state_value("doc_markdown_last_emitted", "") or "").strip()

        if doc_action == "update" and raw_edits:
            from app.assistant.utils.search_replace_editor import apply_edits
            result = apply_edits(current_doc, raw_edits)

            # Retry loop: on any edit failure, route back to the editor with the
            # failure reasons so it can correct its own old_text drift. Capped.
            retry_count = int(self.blackboard.get_state_value("edit_retry_count", 0) or 0)
            if result.failed > 0 and retry_count < self._MAX_EDIT_RETRIES:
                retry_count += 1
                self.blackboard.update_state_value("edit_retry_count", retry_count)
                self.blackboard.update_state_value("edit_retry_failures", list(result.failures))
                logger.warning(
                    "[%s] RETRY %d/%d: %d edit(s) failed, %d applied. Routing back to editor. failures=%s",
                    self.name, retry_count, self._MAX_EDIT_RETRIES,
                    result.failed, result.applied, result.failures,
                )
                self.blackboard.update_state_value("last_agent", self.name)
                self.blackboard.update_state_value("next_agent", self._DOC_WRITER_AGENT)
                return

            if result.failed > 0:
                logger.error(
                    "[%s] Retry EXHAUSTED (%d/%d): %d edit(s) still failing, %d applied. Proceeding with best-effort doc. failures=%s",
                    self.name, retry_count, self._MAX_EDIT_RETRIES,
                    result.failed, result.applied, result.failures,
                )

            # Clear retry state after success or exhaustion.
            self.blackboard.update_state_value("edit_retry_count", 0)
            self.blackboard.update_state_value("edit_retry_failures", None)

            doc_markdown = result.text
            if result.failed == 0:
                logger.debug(
                    "[%s] doc_action=update: %d edit(s) applied — %d chars (retries used: %d)",
                    self.name, result.applied, len(doc_markdown), retry_count,
                )
            # Advance watermark only after a successful edit — so the writer
            # accumulates conversation context across no-op turns until it acts.
            recent_history = str(self.blackboard.get_state_value("recent_history", "") or "").strip()
            if recent_history:
                line_count = len(recent_history.splitlines())
                self.blackboard.update_state_value("doc_writer_watermark_line_idx", line_count)
                session_id = str(self.blackboard.get_state_value("doc_creation_session_id", "") or "").strip()
                if session_id:
                    try:
                        from app.assistant.lib.doc_utils.doc_store import save_doc_draft
                        save_doc_draft(
                            session_id,
                            watermark=line_count,
                            caller="doc_create_final_router_node.advance_watermark",
                        )
                    except Exception as e:
                        logger.debug("[%s] Failed to persist watermark: %s", self.name, e)
        else:
            if raw_edits and doc_action != "update":
                logger.info(
                    "[%s] Agent emitted edits but doc_action=%r — discarding, preserving current doc.",
                    self.name,
                    doc_action,
                )
            doc_markdown = current_doc

        doc_creation_done_tf = bool(self.blackboard.get_state_value(doc_done_key, False))
        doc_type = str(self.blackboard.get_state_value("doc_type", "md") or "md").strip().lower()
        room_id = str(self.blackboard.get_state_value("room_id", "") or "").strip()
        session_id = str(self.blackboard.get_state_value("doc_creation_session_id", "") or "").strip()
        doc_name = str(self.blackboard.get_state_value("doc_name", "") or "").strip()

        # On the first turn there may be no doc yet — emit blank placeholder to force panel open.
        is_first_turn = not current_doc
        if is_first_turn and not doc_markdown and room_id:
            self._emit_doc_update(
                room_id=room_id,
                doc_markdown=self._BLANK_DOC_PLACEHOLDER,
                doc_type=doc_type,
                doc_id=None,
                doc_name=doc_name,
            )
            logger.debug("[%s] First turn no-op: emitted blank placeholder to open panel.", self.name)

        doc_actually_changed = doc_markdown and doc_markdown != current_doc
        doc_id = str(self.blackboard.get_state_value("doc_id", "") or "").strip()

        if doc_actually_changed and room_id:
            self._emit_doc_update(room_id=room_id, doc_markdown=doc_markdown, doc_type=doc_type, doc_id=doc_id or None, doc_name=doc_name)

        if doc_actually_changed:
            self.blackboard.update_state_value("doc_markdown_last_emitted", doc_markdown)
            DI.global_blackboard.update_state_value("doc_markdown_last_emitted", doc_markdown)
            # Auto-save draft to disk for all doc types (gdoc sessions get a local .md backup)
            if session_id:
                self._autosave_md(session_id=session_id, doc_name=doc_name, doc_markdown=doc_markdown)
            # Persist the edited markdown to unified_log doc_store.
            if session_id:
                try:
                    from app.assistant.lib.doc_utils.doc_store import save_doc_draft
                    save_doc_draft(
                        session_id,
                        doc_markdown=doc_markdown,
                        caller="doc_create_final_router_node.edit_apply",
                    )
                except Exception as e:
                    logger.error("[%s] Failed updating doc markdown on doc_store %s: %s", self.name, session_id, e)
                    logger.debug("[%s] doc markdown update exception", self.name, exc_info=True)
                    raise

            # Draft is saved to doc_store above. Push to Google Docs happens on
            # explicit save (doc_creation_done_tf=True → _run_finalize), not every edit.

        logger.info(
            "[%s] doc_creation_done_tf=%s doc_markdown=%s session_id=%s doc_type=%s",
            self.name,
            doc_creation_done_tf,
            bool(doc_markdown),
            session_id or "(none)",
            doc_type,
        )

        if doc_creation_done_tf and doc_markdown:
            self._fire_finalize_async(
                session_id=session_id,
                doc_name=doc_name,
                doc_markdown=doc_markdown,
                doc_type=doc_type,
                room_id=room_id,
            )

        self.blackboard.update_state_value(
            "result",
            {
                "final_answer_task": str(self.blackboard.get_state_value("task", "") or ""),
                "final_answer_answer": chat_response,
                "final_answer_what_was_done": "Doc creation mode turn.",
                "final_answer_interesting_info": "",
                "final_answer_sources": [],
                "doc_creation_mode_done_tf": doc_creation_done_tf,
                "doc_markdown": doc_markdown,
            },
        )
        self.blackboard.update_state_value("next_agent", final_node)
        self.blackboard.update_state_value("last_agent", self.name)

    def _emit_doc_update(
        self,
        *,
        room_id: str,
        doc_markdown: str,
        doc_type: str,
        doc_id: str | None,
        doc_name: str = "",
    ) -> None:
        try:
            from app.assistant.utils.pydantic_classes import UserMessage, UserMessageData
            assistant_name = get_required_assistant_name()
            payload: dict = {
                "data_type": "doc_draft_update",
                "doc_markdown": doc_markdown,
                "doc_type": doc_type,
            }
            if doc_id:
                payload["doc_id"] = doc_id
            if doc_name:
                payload["doc_name"] = doc_name
            msg = UserMessage(
                data_type="user_msg",
                sender=assistant_name,
                receiver=None,
                role="assistant",
                metadata={"reply_to": {"type": "socketio", "room_id": room_id}},
                user_message_data=UserMessageData(widget_data=[payload]),
            )
            msg.event_topic = "socket_emit"
            DI.event_hub.publish(msg)
            logger.debug("[%s] Emitted doc_draft_update to room_id %s", self.name, room_id)
        except Exception as e:
            logger.error("[%s] Failed emitting doc_draft_update: %s", self.name, e)
            logger.debug("[%s] doc_draft_update emit exception", self.name, exc_info=True)
            raise

    def _autosave_md(self, *, session_id: str, doc_name: str, doc_markdown: str) -> None:
        try:
            drafts_dir = Path(get_repo_root()) / "docs" / "drafts"
            drafts_dir.mkdir(parents=True, exist_ok=True)
            if doc_name:
                # Sanitise for filesystem: replace spaces with underscores, strip unsafe chars
                import re as _re
                safe_name = _re.sub(r"[^\w\-]", "_", doc_name).strip("_")
                filename = f"{safe_name}.md" if safe_name else f"{session_id}.md"
            else:
                filename = f"{session_id}.md"
            draft_path = drafts_dir / filename
            draft_path.write_text(doc_markdown, encoding="utf-8")
            logger.debug("[%s] Auto-saved draft to %s", self.name, draft_path)
        except Exception as e:
            logger.error("[%s] Failed auto-saving markdown draft for session %s: %s", self.name, session_id, e)
            logger.debug("[%s] autosave exception", self.name, exc_info=True)
            raise

    def _fire_finalize_async(
        self,
        *,
        session_id: str,
        doc_name: str,
        doc_markdown: str,
        doc_type: str,
        room_id: str,
    ) -> None:
        thread = threading.Thread(
            target=self._run_finalize,
            args=(session_id, doc_name, doc_markdown, doc_type, room_id),
            daemon=True,
            name=f"doc-finalize-{session_id}",
        )
        thread.start()
        logger.info("[%s] Fired async finalize thread for session %s doc_type=%s", self.name, session_id, doc_type)

    @staticmethod
    def _run_finalize(session_id: str, doc_name: str, doc_markdown: str, doc_type: str, room_id: str) -> None:
        try:
            doc_id: str | None = None
            doc_url: str | None = None
            account_id: str | None = None

            if doc_type == "md":
                # Write finalized doc to docs/<doc_name>.md (or session_id if no name)
                docs_dir = Path(get_repo_root()) / "docs"
                docs_dir.mkdir(parents=True, exist_ok=True)
                filename = f"{doc_name}.md" if doc_name else f"{session_id}.md"
                final_path = docs_dir / filename
                final_path.write_text(doc_markdown, encoding="utf-8")
                doc_id = doc_name or session_id
                doc_url = str(final_path)
                logger.info("Finalized md doc session=%s name=%s path=%s", session_id, doc_name, final_path)

            elif doc_type == "gdoc":
                from app.assistant.lib.core_tools.google_docs_tool.google_docs_client import GoogleDocsClient
                client = GoogleDocsClient(readonly=False)
                account_id = getattr(client, "account_id", None)

                # Check if a Google Doc already exists for this session via doc_store.
                existing_doc_id: str | None = None
                try:
                    from app.assistant.lib.doc_utils.doc_store import load_doc_draft
                    draft = load_doc_draft(session_id)
                    if draft is not None:
                        existing_doc_id = str(draft.get("google_doc_id") or "").strip() or None
                except Exception as e:
                    logger.error("Failed reading existing doc_id for session %s: %s", session_id, e)
                    logger.debug("doc_id lookup exception", exc_info=True)
                    raise

                if existing_doc_id:
                    client.replace_entire_body(existing_doc_id, doc_markdown)
                    doc_id = existing_doc_id
                    meta = client.get_document_metadata(existing_doc_id)
                    doc_url = meta.get("webViewLink")
                    logger.info("Updated gdoc session=%s doc_id=%s", session_id, doc_id)
                else:
                    # Create a new Google Doc via Drive API
                    from googleapiclient.discovery import build as _build
                    creds = client._creds  # reuse authenticated credentials
                    drive_svc = _build("drive", "v3", credentials=creds)
                    file_meta = {
                        "name": doc_name or f"Document {session_id}",
                        "mimeType": "application/vnd.google-apps.document",
                    }
                    created = drive_svc.files().create(body=file_meta, fields="id,webViewLink").execute()
                    doc_id = created.get("id")
                    doc_url = created.get("webViewLink")
                    if doc_id:
                        client.append_text(doc_id, doc_markdown)
                    logger.info("Created new gdoc session=%s doc_id=%s", session_id, doc_id)

                # Persist google_doc_id back to doc_store.
                if doc_id:
                    try:
                        from app.assistant.lib.doc_utils.doc_store import save_doc_draft
                        save_doc_draft(
                            session_id,
                            google_doc_id=doc_id,
                            caller="doc_create_final_router_node.finalize_gdoc",
                        )
                    except Exception as e:
                        logger.error("Failed persisting doc_id for session %s: %s", session_id, e)
                        logger.debug("doc_id persist exception", exc_info=True)
                        raise

            # Emit doc_created socket event
            if room_id:
                try:
                    from app.assistant.utils.pydantic_classes import UserMessage, UserMessageData
                    assistant_name = get_required_assistant_name()
                    payload_data: dict = {
                        "data_type": "doc_created",
                        "doc_id": doc_id or "",
                        "doc_type": doc_type,
                        "doc_url": doc_url or "",
                    }
                    if account_id:
                        payload_data["account_id"] = account_id
                    msg = UserMessage(
                        data_type="user_msg",
                        sender=assistant_name,
                        receiver=None,
                        role="assistant",
                        metadata={"reply_to": {"type": "socketio", "room_id": room_id}},
                        user_message_data=UserMessageData(widget_data=[payload_data]),
                    )
                    msg.event_topic = "socket_emit"
                    DI.event_hub.publish(msg)
                    logger.info("Emitted doc_created to room_id %s doc_id=%s", room_id, doc_id)
                except Exception as e:
                    logger.error("Failed emitting doc_created notice: %s", e)
                    logger.debug("doc_created emit exception", exc_info=True)
                    raise

        except Exception as e:
            logger.error("Doc finalize failed for session %s: %s", session_id, e)
            logger.debug("doc finalize exception", exc_info=True)
            if room_id:
                try:
                    from app.assistant.utils.pydantic_classes import UserMessage, UserMessageData
                    assistant_name = get_required_assistant_name()
                    msg = UserMessage(
                        data_type="user_msg",
                        sender=assistant_name,
                        receiver=None,
                        role="assistant",
                        metadata={"reply_to": {"type": "socketio", "room_id": room_id}},
                        user_message_data=UserMessageData(
                            widget_data=[{"data_type": "doc_save_failed", "error": str(e)[:200]}]
                        ),
                    )
                    msg.event_topic = "socket_emit"
                    DI.event_hub.publish(msg)
                except Exception as emit_err:
                    logger.error("Failed emitting doc_save_failed: %s", emit_err)
                    logger.debug("doc_save_failed emit exception", exc_info=True)

    def _cfg(self) -> dict:
        flow_cfg = self.blackboard.get_state_value("manager_flow_config", None)
        if not isinstance(flow_cfg, dict):
            raise ValueError("manager_flow_config must be a dict.")
        doc_mode = flow_cfg.get("doc_creation_mode")
        if not isinstance(doc_mode, dict):
            raise ValueError("manager_flow_config.doc_creation_mode must be a dict.")
        return doc_mode

    @staticmethod
    def _required_str(cfg: dict, key: str) -> str:
        raw = cfg.get(key)
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"doc create final router requires non-empty '{key}'.")
        return raw.strip()

    @staticmethod
    def _optional_str(cfg: dict, key: str, default: str) -> str:
        raw = cfg.get(key, default)
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"doc create final router key '{key}' must be non-empty when provided.")
        return raw.strip()
