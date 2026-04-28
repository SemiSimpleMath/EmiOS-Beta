# send_email.py

from app.assistant.utils.logging_config import get_logger
logger = get_logger(__name__)

from app.assistant.lib.core_tools.email_tool.utils.gmail_api_client import GmailAPIClient

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
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
            account_id = str(arguments.get('account_id') or "").strip() or None

            if not to:
                error_msg = "Missing required email argument: 'to'."
                logger.error(f"Error: {error_msg}")
                error_result = ToolResult(result_type="error", content="Missing required email argument: 'to'.")
                return self.publish_msg(error_result)

            gmail_client = GmailAPIClient(account_id=account_id)
            sent = gmail_client.send_email(
                to=to,
                subject=subject,
                body=body,
            )
            if not isinstance(sent, dict) or not str(sent.get("id") or "").strip():
                error_result = ToolResult(result_type="error", content="failed to send email.")
                return self.publish_msg(error_result)
            else:
                logger.info("Email sent successfully to %s via account_id=%s", to, account_id or "default")

                tool_result_msg = ToolResult(
                    result_type="send_email",
                    content=f"Email successfully sent to {to}",
                    data={
                        "message_id": str(sent.get("id") or ""),
                        "account_id": (account_id or "default"),
                    },
                )

                return self.publish_msg(tool_result_msg)


        except Exception as e:
            logger.error("Error in SendEmailTool: %s", e)
            logger.debug("error in SendEmailTool exception details", exc_info=True)
            tool_result_msg = ToolResult(
                result_type="error",
                content=f"Email could not be sent to {to}"
            )

            return self.publish_msg(tool_result_msg)

    def publish_msg(self, msg):
            return msg


def get_tool_class():
    """
    Returns an instance of the tool.
    This function is required by the tool registry.
    """
    return SendEmail