import base64
from email.mime.text import MIMEText

from src.utils.google_auth import get_gmail_service, not_connected


def send_email(phone: str, to: str, subject: str, body: str) -> str:
    # Sends through the Gmail API with this user's own OAuth token (the
    # gmail.send scope is requested alongside gmail.readonly/calendar for
    # this). This used to go over SMTP with a separate app password, which is
    # a second credential to keep valid and can't be per user.
    service = get_gmail_service(phone)
    if not service:
        return not_connected("Gmail", phone)

    try:
        msg = MIMEText(body)
        msg["To"] = to
        msg["Subject"] = subject
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

        service.users().messages().send(userId="me", body={"raw": raw}).execute()

        return f"Email sent to {to} with subject '{subject}'."

    except Exception as e:
        return f"Error sending email: {e}"


SEND_EMAIL_SCHEMA = {
    "type": "object",
    "properties": {
        "to": {"type": "string", "description": "Recipient email address."},
        "subject": {"type": "string", "description": "Email subject line."},
        "body": {"type": "string", "description": "Email body text."},
    },
    "required": ["to", "subject", "body"],
}
