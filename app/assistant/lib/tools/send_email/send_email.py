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
            attachment_paths: list = []
            missing_pods: list = []
            unbacked_pods: list = []
            if pod_ids:
                from pathlib import Path
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
                    if not rel:
                        unbacked_pods.append(pid)
                        continue
                    abs_path = (repo_root / rel).resolve()
                    if not abs_path.is_file():
                        unbacked_pods.append(pid)
                        continue
                    attachment_paths.append(str(abs_path))

                if missing_pods or unbacked_pods:
                    err = []
                    if missing_pods:
                        err.append(f"missing pods: {missing_pods}")
                    if unbacked_pods:
                        err.append(f"pods without backing files: {unbacked_pods}")
                    hint = (
                        " Pod IDs must be the EXACT full URI returned by pod_search "
                        "or mint_pod_from_path (e.g. 'datapod:image:abc...'). Don't "
                        "strip the 'datapod:<kind>:' prefix. If the pod truly doesn't "
                        "exist, mint a fresh one via bash_manager + mint_pod_from_path."
                    )
                    return self.publish_msg(make_tool_error(
                        error_code="pod_attachment_failed",
                        message=f"Cannot attach: {'; '.join(err)}.{hint}",
                        abort_policy="abort_tool",
                        retryable=True,
                        details={
                            "missing_pod_ids": list(missing_pods),
                            "unbacked_pod_ids": list(unbacked_pods),
                            "received_pod_ids": list(pod_ids),
                        },
                    ))

            gmail_client = GmailAPIClient()
            sent = gmail_client.send_email(
                to=to,
                subject=subject,
                body=body,
                attachment_paths=attachment_paths or None,
            )
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