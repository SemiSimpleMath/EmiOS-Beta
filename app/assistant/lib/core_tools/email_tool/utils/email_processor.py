# app/assistant/lib/core_tools/email_tool/utils/email_processor.py
from app.assistant.utils.logging_config import get_logger
from email.message import EmailMessage
from bs4 import BeautifulSoup
from email.utils import parseaddr

logger = get_logger(__name__)

class EmailProcessor:
    @staticmethod
    def extract_metadata(email_message):
        """
        Extracts sender, subject, and date metadata.
        (This is YOUR original, correct function)
        """
        from_raw = email_message.get("From")
        display_name, email_address = parseaddr(from_raw)
        subject = email_message.get("Subject", "[No Subject]")
        date_received = email_message.get("Date", "[No Date]")

        return {
            "sender": display_name or email_address, # <-- Uses the "sender" key
            "email_address": email_address,
            "subject": subject,
            "date_received": date_received,
        }

    @staticmethod
    def extract_email_body(email_message: EmailMessage) -> str:
        """
        Robustly extracts the best possible text body from an email.
        Handles multipart/alternative by preferring plain text,
        but falls back to cleaned HTML if plain text is lacking.
        """
        plain_text = ""
        html_text = ""

        # 1. Walk through all parts of the email
        if email_message.is_multipart():
            for part in email_message.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition"))

                # Skip attachments
                if "attachment" in content_disposition:
                    continue

                try:
                    # Get the payload and decode it to unicode
                    payload = part.get_payload(decode=True)
                    if not payload:
                        continue

                    # Handle standard text parts
                    if content_type == "text/plain":
                        charset = part.get_content_charset() or 'utf-8'
                        plain_text += payload.decode(charset, errors="replace")

                    # Handle HTML parts
                    elif content_type == "text/html":
                        charset = part.get_content_charset() or 'utf-8'
                        html_text += payload.decode(charset, errors="replace")

                except Exception as e:
                    logger.warning(f"Error decoding part {content_type}: {e}")
                    continue
        else:
            # Not multipart, just grab the payload
            payload = email_message.get_payload(decode=True)
            charset = email_message.get_content_charset() or 'utf-8'
            if payload:
                text = payload.decode(charset, errors="replace")
                if email_message.get_content_type() == "text/html":
                    html_text = text
                else:
                    plain_text = text

        # 2. Decision Logic: Choose the best content.
        # Prefer HTML — it's the authoritative body the user sees in their
        # email client.  Plaintext parts are often auto-generated, outdated,
        # or abbreviated (e.g. "View in browser" stubs).  Marketing emails
        # frequently have plaintext/HTML mismatches with different dates or
        # content.  Clean HTML with BeautifulSoup produces good LLM input.
        if html_text:
            return EmailProcessor._clean_html(html_text)
        return plain_text.strip() if plain_text else "[Empty Email Body]"

    @staticmethod
    def _clean_html(html_content: str) -> str:
        """
        Converts HTML to reasonably clean text for LLM consumption.
        Requires: pip install beautifulsoup4
        """
        try:
            soup = BeautifulSoup(html_content, "html.parser")

            # Remove script and style elements which confuse LLMs
            for script in soup(["script", "style", "head", "title", "meta", "[document]"]):
                script.extract()

            # Get text
            text = soup.get_text()

            # Break into lines and remove leading and trailing space on each
            lines = (line.strip() for line in text.splitlines())
            # Break multi-headlines into a line each
            chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
            # Drop blank lines
            text = '\n'.join(chunk for chunk in chunks if chunk)

            return text
        except Exception as e:
            logger.error(f"HTML cleaning failed: {e}")
            # Fallback if BS4 fails: simplistic tag stripping
            import re
            return re.sub('<[^<]+?>', '', html_content).strip()