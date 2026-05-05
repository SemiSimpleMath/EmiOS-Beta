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

            # Resolve pod_ids to absolute file paths via PodStore + metadata.
            # File-backed pods (image/document/audio/video) attach as their
            # stored file. Body-only pods (email/text/transcript/note) get
            # rendered to a tempfile here and attached — agent never reads
            # the body, render happens outside any LLM context. See
            # ``pod_render`` for the rendering rules.
            attachment_paths: list = []
            missing_pods: list = []   # pod_id has no row in PodStore
            empty_pods: list = []     # pod exists but has no file AND no body
            tempdir: str = ""
            import shutil
            if pod_ids:
                import tempfile
                from app.assistant.lib.tools.send_email.pod_render import (
                    render_pod_to_attachment,
                )
                from app.assistant.pod_store.pod_store import PodStore
                from app.assistant.utils.path_utils import get_repo_root
                store = PodStore()
                repo_root = get_repo_root()
                for pid in pod_ids:
                    pod = store.get(pid)
                    if pod is None:
                        missing_pods.append(pid)
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

                if missing_pods or empty_pods:
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
                from app.assistant.pod_store.pod_store import PodStore as _PS
                inline_store = _PS()
                inline_blocks = []
                inline_missing = []
                inline_empty = []
                for pid in inline_pod_ids:
                    pod = inline_store.get(pid)
                    if pod is None:
                        inline_missing.append(pid)
                        continue
                    if not pod.body:
                        inline_empty.append(pid)
                        continue
                    inline_blocks.append(render_pod_as_inline_block(pod))
                if inline_missing or inline_empty:
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
                            "received_inline_pod_ids": list(inline_pod_ids),
                        },
                    ))
                # Append the inline blocks to the body. Preserve the
                # planner-written body verbatim above, separator, then blocks.
                body = (body.rstrip() + "\n\n" + "\n\n".join(inline_blocks)).lstrip("\n")

            try:
                gmail_client = GmailAPIClient()
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