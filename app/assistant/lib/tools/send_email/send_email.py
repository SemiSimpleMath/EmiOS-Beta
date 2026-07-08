# send_email.py

from app.assistant.utils.logging_config import get_logger
logger = get_logger(__name__)

from app.assistant.lib.core_tools.email_tool.utils.gmail_api_client import GmailAPIClient

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.utils.pydantic_classes import (
    ToolMessage,
    ToolResult,
)

from app.assistant.ServiceLocator.service_locator import DI


class SendEmail(BaseTool):
    """
    Tool to send emails based on provided arguments.
    """
    requires_approval = True  # Require user approval before sending

    def __init__(self):
        super().__init__('send_email')

    def compute_approval_reduction(
        self,
        tool_message,
        caller_authority: int,
    ):
        """Soften the approval bar when sending to an allowlisted recipient.

        Allowlist lives in `configs/email_allowlist.yaml`. Recipients on
        the list can be emailed at authority 95 (dayflow surface) without
        firing a confirmation ticket. Recipients NOT on the list fall
        through to the standard approval gate (currently:
        approval_min_authority=99 — master_room only).

        Only single-recipient sends are softened. If `to` is a comma-
        separated list of addresses, every address must be allowlisted
        for the softening to apply — otherwise revert to the standard
        approval flow. (Better to over-prompt than to leak a bcc-style
        send to an arbitrary recipient because the allowlist matched
        the first address in a list.)
        """
        from app.assistant.lib.tools.send_email.allowlist import is_allowlisted
        tool_data = tool_message.tool_data if isinstance(tool_message.tool_data, dict) else {}
        arguments = tool_data.get('arguments') if isinstance(tool_data.get('arguments'), dict) else {}
        to = arguments.get('to')
        if not isinstance(to, str) or not to.strip():
            return None
        recipients = [r.strip() for r in to.split(',') if r.strip()]
        if not recipients:
            return None
        if not all(is_allowlisted(r) for r in recipients):
            return None
        # All recipients are allowlisted — soften approval to chat-surface
        # authority. dayflow_orchestrator (95) clears this; master_room (99)
        # trivially does; any lower-authority surface still needs approval.
        return 95

    def describe_action(self, tool_message: 'ToolMessage') -> tuple:
        arguments = tool_message.tool_data.get('arguments', {})
        to = arguments.get('to', 'unknown')
        subject = arguments.get('subject', '(no subject)')
        body = arguments.get('body', '')
        body_preview = body[:150] + '...' if len(body) > 150 else body
        title = f"Send email to {to}?"
        message = f"**Subject:** {subject}\n\n**Preview:**\n{body_preview}"
        return title, message

    def execute(self, tool_message: 'ToolMessage') -> None:
        """
        Executes the SendEmailTool to send an email based on provided arguments.

        Parameters:
        - tool_message (ToolMessage): The message triggering the tool execution.

        Returns:
        - Message: The result of the email sending operation.
        """
        request_id = tool_message.request_id
        try:
            arguments = tool_message.tool_data.get('arguments', {})
            subject = arguments.get('subject') or ""
            body = arguments.get('body') or ""
            to = arguments.get('to')
            from_account_alias = arguments.get('from_account')
            raw_pod_ids = arguments.get('pod_ids') or []
            if isinstance(raw_pod_ids, str):
                raw_pod_ids = [raw_pod_ids]
            pod_ids = [str(p).strip() for p in raw_pod_ids if str(p).strip()]
            raw_inline_pod_ids = arguments.get('inline_pod_ids') or []
            if isinstance(raw_inline_pod_ids, str):
                raw_inline_pod_ids = [raw_inline_pod_ids]
            inline_pod_ids = [
                str(p).strip() for p in raw_inline_pod_ids if str(p).strip()
            ]

            if not to:
                error_msg = "Missing required email argument: 'to'."
                logger.error(f"Error: {error_msg}")
                return self.publish_msg(make_tool_error(
                    error_code="missing_recipient",
                    message=error_msg,
                    abort_policy="abort_tool",
                    retryable=True,
                ))

            # Resolve pod_ids to absolute file paths via the GATED pod read.
            # File-backed pods (image/document/audio/video) attach as their
            # stored file. Body-only pods (email/text/transcript/note) get
            # rendered to a tempfile here and attached — agent never reads
            # the body, render happens outside any LLM context. See
            # ``pod_render`` for the rendering rules.
            #
            # Attaching IS dereferencing on the agent's behalf, so it goes
            # through the same walls as pod_fetch (get_pod_gated: scope wall
            # + the pod's min_authority floor). Out-of-scope reads as missing
            # (no existence oracle); an authority denial is reported as
            # denied. Without this gate an agent could EMAIL a pod it isn't
            # allowed to pod_fetch (2026-07-08 pod audit R2).
            attachment_paths: list = []
            missing_pods: list = []   # no row in PodStore OR out of scope
            empty_pods: list = []     # pod exists but has no file AND no body
            denied_pods: list = []    # in scope, but authority below the pod's floor
            tempdir: str = ""
            import shutil
            from app.assistant.pod_store.authority import PodAuthorityError
            from app.assistant.pod_store.pod_utils import PodNotFound, get_pod_gated
            caller_scope = getattr(tool_message, "scope_context", None)
            if pod_ids:
                import tempfile
                from app.assistant.lib.tools.send_email.pod_render import (
                    render_pod_to_attachment,
                )
                from app.assistant.utils.path_utils import get_repo_root
                repo_root = get_repo_root()
                for pid in pod_ids:
                    try:
                        pod = get_pod_gated(pid, caller_scope)
                    except PodNotFound:
                        missing_pods.append(pid)
                        continue
                    except PodAuthorityError:
                        denied_pods.append(pid)
                        continue
                    md = pod.metadata or {}
                    rel = (md.get("stored_path") or "").strip()
                    if rel:
                        abs_path = (repo_root / rel).resolve()
                        if abs_path.is_file():
                            attachment_paths.append(str(abs_path))
                            continue
                        # stored_path was set but file is gone — fall through
                        # and try rendering the body if present.
                    if pod.body:
                        if not tempdir:
                            tempdir = tempfile.mkdtemp(prefix="emi_send_email_")
                        rendered = render_pod_to_attachment(pod, tempdir)
                        attachment_paths.append(rendered)
                        continue
                    empty_pods.append(pid)

                if missing_pods or empty_pods or denied_pods:
                    msgs = []
                    if missing_pods:
                        msgs.append(
                            f"Pods not found: {missing_pods}. Pod IDs must "
                            f"be the EXACT URI returned by pod_search or "
                            f"mint_pod_from_path (e.g. 'datapod:image:abc...') "
                            f"— don't strip the 'datapod:<kind>:' prefix. If "
                            f"the pod truly doesn't exist, mint one via "
                            f"bash_manager + mint_pod_from_path."
                        )
                    if empty_pods:
                        msgs.append(
                            f"Pods with no body and no backing file: "
                            f"{empty_pods}. These pods are empty and cannot "
                            f"be sent — check the pod_ids are correct."
                        )
                    if denied_pods:
                        msgs.append(
                            f"Pods requiring higher authority than this scope "
                            f"carries: {denied_pods}. They cannot be attached "
                            f"from here."
                        )
                    if tempdir:
                        shutil.rmtree(tempdir, ignore_errors=True)
                    return self.publish_msg(make_tool_error(
                        error_code="pod_attachment_failed",
                        message=" ".join(msgs),
                        abort_policy="abort_tool",
                        retryable=True,
                        details={
                            "missing_pod_ids": list(missing_pods),
                            "empty_pod_ids": list(empty_pods),
                            "denied_pod_ids": list(denied_pods),
                            "received_pod_ids": list(pod_ids),
                        },
                    ))

            # inline_pod_ids: fetch each pod's body and append to ``body`` as a
            # delimited block. Tool reads, agent doesn't — same privacy story
            # as the attachment path. The planner gets to choose attach vs
            # inline; both keep the body out of any LLM context.
            if inline_pod_ids:
                from app.assistant.lib.tools.send_email.pod_render import (
                    render_pod_as_inline_block,
                )
                inline_blocks = []
                inline_missing = []
                inline_empty = []
                inline_denied = []
                for pid in inline_pod_ids:
                    # Same gated dereference as the attachment path — inlining
                    # a body into an outbound email is a read on the agent's
                    # behalf and clears the same walls.
                    try:
                        pod = get_pod_gated(pid, caller_scope)
                    except PodNotFound:
                        inline_missing.append(pid)
                        continue
                    except PodAuthorityError:
                        inline_denied.append(pid)
                        continue
                    if not pod.body:
                        inline_empty.append(pid)
                        continue
                    inline_blocks.append(render_pod_as_inline_block(pod))
                if inline_missing or inline_empty or inline_denied:
                    msgs = []
                    if inline_missing:
                        msgs.append(
                            f"inline_pod_ids not found: {inline_missing}. "
                            f"Pod IDs must be exact URIs from pod_search."
                        )
                    if inline_empty:
                        msgs.append(
                            f"inline_pod_ids with empty body: {inline_empty}. "
                            f"Cannot inline a pod with no body."
                        )
                    if inline_denied:
                        msgs.append(
                            f"inline_pod_ids requiring higher authority than "
                            f"this scope carries: {inline_denied}."
                        )
                    if tempdir:
                        shutil.rmtree(tempdir, ignore_errors=True)
                    return self.publish_msg(make_tool_error(
                        error_code="inline_pod_failed",
                        message=" ".join(msgs),
                        abort_policy="abort_tool",
                        retryable=True,
                        details={
                            "inline_missing_pod_ids": list(inline_missing),
                            "inline_empty_pod_ids": list(inline_empty),
                            "inline_denied_pod_ids": list(inline_denied),
                            "received_inline_pod_ids": list(inline_pod_ids),
                        },
                    ))
                # Append the inline blocks to the body. Preserve the
                # planner-written body verbatim above, separator, then blocks.
                body = (body.rstrip() + "\n\n" + "\n\n".join(inline_blocks)).lstrip("\n")

            try:
                scope_acting_as = None
                scope_ctx = getattr(tool_message, "scope_context", None)
                if scope_ctx is not None:
                    scope_acting_as = getattr(scope_ctx, "acting_as", None)
                try:
                    resolved_account_id = DI.env_registry.resolve_gmail_account_id(
                        from_account_alias,
                        scope_acting_as=scope_acting_as,
                    )
                except ValueError as e:
                    if tempdir:
                        shutil.rmtree(tempdir, ignore_errors=True)
                    return self.publish_msg(make_tool_error(
                        error_code="invalid_from_account",
                        message=str(e),
                        abort_policy="abort_tool",
                        retryable=False,
                        details={"from_account": from_account_alias},
                    ))
                expected_email = DI.env_registry.expected_email_for_account_id(resolved_account_id)
                try:
                    gmail_client = GmailAPIClient(
                        account_id=resolved_account_id,
                        expected_principal_email=expected_email or None,
                    )
                except PermissionError as e:
                    if tempdir:
                        shutil.rmtree(tempdir, ignore_errors=True)
                    return self.publish_msg(make_tool_error(
                        error_code="account_identity_mismatch",
                        message=str(e),
                        abort_policy="abort_tool",
                        retryable=False,
                        details={"account_id": resolved_account_id, "expected": expected_email},
                    ))
                sent = gmail_client.send_email(
                    to=to,
                    subject=subject,
                    body=body,
                    attachment_paths=attachment_paths or None,
                )
            finally:
                if tempdir:
                    shutil.rmtree(tempdir, ignore_errors=True)
            if not isinstance(sent, dict) or not str(sent.get("id") or "").strip():
                return self.publish_msg(make_tool_error(
                    error_code="gmail_send_failed",
                    message="Gmail API did not return a message id; the send did not succeed.",
                    abort_policy="abort_tool",
                    retryable=True,
                    details={"to": str(to), "had_attachments": bool(attachment_paths)},
                ))
            else:
                logger.info("Email sent successfully to %s (attachments=%d)",
                            to, len(attachment_paths))

                summary = f"Email successfully sent to {to}"
                if attachment_paths:
                    summary += f" with {len(attachment_paths)} attachment(s)"

                tool_result_msg = ToolResult(
                    result_type="send_email",
                    content=summary,
                    data={
                        "message_id": str(sent.get("id") or ""),
                        "attachments_sent": len(attachment_paths),
                        "attachment_pod_ids": pod_ids,
                    },
                )

                return self.publish_msg(tool_result_msg)


        except Exception as e:
            logger.error("Error in SendEmailTool: %s", e)
            logger.debug("error in SendEmailTool exception details", exc_info=True)
            return self.publish_msg(make_tool_error(
                error_code="send_email_exception",
                message=f"Email to {to} failed: {e}",
                abort_policy="abort_tool",
                retryable=True,
                details={"exception_type": type(e).__name__, "exception_str": str(e)},
            ))

    def publish_msg(self, msg):
            return msg


def get_tool_class():
    """
    Returns an instance of the tool.
    This function is required by the tool registry.
    """
    return SendEmail