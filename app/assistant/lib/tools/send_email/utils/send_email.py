import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import os
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def send_email(subject, message, from_addr, to, smtp_server, smtp_port, username, password):
    """
    Send an email using SMTP.

    Args:
    - subject (str): Email subject.
    - message (str): Email message body.
    - from_addr (str): Sender's email address.
    - to (str): Recipient's email address.
    - smtp_server (str): SMTP server address.
    - smtp_port (int): SMTP server port.
    - username (str): SMTP login username (usually the sender's email address).
    - password (str): SMTP login password or app-specific password.

    Returns:
    - str: Result message indicating success or failure.
    - str: Error message if an error occurs.
    """
    try:
        # Create a multipart message
        msg = MIMEMultipart()
        msg['From'] = from_addr
        msg['To'] = to
        msg['Subject'] = subject

        # Attach the message body
        msg.attach(MIMEText(message, 'plain'))

        # Set up the SMTP server connection
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()  # Secure the connection
        server.login(username, password)  # Log in to the server

        # Send the email
        server.sendmail(from_addr, to, msg.as_string())
        server.quit()

        logger.info(f"Email sent successfully to {to}")
        return "Email sent successfully", None
    except Exception as e:
        logger.error(f"An error occurred while sending the email: {e}")
        return "Failed to send email", str(e)

# Tool entry-point function for setting email parameters and sending email
def set_email_parameters(json_data):
    """
    Set email parameters and send an email via SMTP.

    Args:
    - json_data (dict): Dictionary containing email parameters (subject, message, recipient).

    Returns:
    - str: Result message indicating success or failure.
    - str: Error message if an error occurs.
    """
    try:
        logger.debug(f"set_email {json_data}")

        subject = json_data['subject']
        message = json_data.get('body', ' ')

        primary_email = os.getenv("EMAIL_ADDR")
        if not primary_email:
            raise ValueError("EMAIL_ADDR env var is not set — cannot send email without a configured sender address.")

        from_addr = primary_email
        to = json_data['to']
        smtp_server = "smtp.gmail.com"
        smtp_port = 587
        username = primary_email
        password = os.getenv("GMAIL_APP_PASSWORD") or os.getenv("EMAIL_PASSWORD")
        if not password:
            raise ValueError("Neither GMAIL_APP_PASSWORD nor EMAIL_PASSWORD env var is set — cannot authenticate with SMTP.")

        result, error = send_email(subject, message, from_addr, to, smtp_server, smtp_port, username, password)
        return result, error
    except Exception as e:
        logger.debug("set_email_parameters failed", exc_info=True)
        raise

if __name__ == "__main__":
    import sys
    to_addr = os.getenv("EMAIL_ADDR") or sys.argv[1] if len(sys.argv) > 1 else None
    if not to_addr:
        print("Usage: EMAIL_ADDR=you@example.com python send_email.py")
        sys.exit(1)
    email_data = {
        'subject': 'Test Email',
        'body': 'This is a test email sent using SMTP.',
        'to': to_addr,
    }
    result, error = set_email_parameters(email_data)
    print(f"Result: {result}" if error is None else f"Error: {error}")
