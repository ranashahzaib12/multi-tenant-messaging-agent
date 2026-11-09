"""Read one email in full. The list tool only carries Gmail's snippet (about
200 characters), which once cut a date in half; anything that needs the
actual content goes through here."""
import base64
import html
import re

from src.utils.google_auth import get_gmail_service, not_connected

MAX_BODY_CHARS = 12_000


def _decode(part: dict) -> str:
    data = part.get("body", {}).get("data")
    if not data:
        return ""
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def _html_to_text(markup: str) -> str:
    # Comments first: marketing mail is full of Outlook conditionals
    # (<!--[if mso]> … <![endif]-->) that otherwise leak into the text.
    markup = re.sub(r"(?s)<!--.*?-->", "", markup)
    markup = re.sub(r"(?is)<(script|style|head).*?</\1>", "", markup)
    markup = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>|</h\d>", "\n", markup)
    text = re.sub(r"<[^>]+>", "", markup)
    text = html.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


def _walk(part: dict, plain: list[str], rich: list[str], attachments: list[str]) -> None:
    mime = part.get("mimeType", "")
    if part.get("filename"):
        size = part.get("body", {}).get("size", 0)
        attachments.append(f"{part['filename']} ({mime}, {size} bytes)")
    elif mime == "text/plain":
        plain.append(_decode(part))
    elif mime == "text/html":
        rich.append(_decode(part))
    for child in part.get("parts", []) or []:
        _walk(child, plain, rich, attachments)


def read_email(phone: str, message_id: str) -> str:
    service = get_gmail_service(phone)
    if not service:
        return not_connected("Gmail", phone)
    try:
        msg = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    except Exception as e:
        return f"Error reading email {message_id}: {e}"

    payload = msg.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    plain: list[str] = []
    rich: list[str] = []
    attachments: list[str] = []
    _walk(payload, plain, rich, attachments)
    body = "\n\n".join(p for p in plain if p.strip()) or _html_to_text("\n".join(rich)) or msg.get("snippet", "")
    truncated = len(body) > MAX_BODY_CHARS
    body = body[:MAX_BODY_CHARS]

    lines = [
        f"From: {headers.get('from', '?')}",
        f"To: {headers.get('to', '?')}",
        f"Date: {headers.get('date', '?')}",
        f"Subject: {headers.get('subject', '(no subject)')}",
    ]
    if attachments:
        lines.append("Attachments: " + "; ".join(attachments))
    lines.append("")
    lines.append(body.strip() or "(empty body)")
    if truncated:
        lines.append(f"\n[Body truncated at {MAX_BODY_CHARS:,} characters]")
    return "\n".join(lines)


READ_EMAIL_SCHEMA = {
    "type": "object",
    "properties": {
        "message_id": {"type": "string", "description": "The [id: …] value from read_emails."},
    },
    "required": ["message_id"],
}
