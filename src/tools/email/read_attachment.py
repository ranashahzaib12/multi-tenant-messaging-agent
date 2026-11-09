"""Read the text of an email attachment (PDF, Word, plain text) so the
agent can answer from a policy document or invoice someone emailed the
user, using the same extractor as WhatsApp document uploads. Nothing is
stored; the bytes live only for the duration of the call."""
import base64

from src.utils.documents import DocumentError, MAX_BYTES, SUPPORTED_TYPES, extract_text
from src.utils.google_auth import get_gmail_service, not_connected

MAX_TEXT_CHARS = 12_000


def _attachments(part: dict, found: list[dict]) -> None:
    if part.get("filename") and part.get("body", {}).get("attachmentId"):
        found.append(part)
    for child in part.get("parts", []) or []:
        _attachments(child, found)


def find_attachment(service, message_id: str, filename: str | None) -> tuple[dict | None, str | None]:
    """The attachment part the user means, or (None, why-not)."""
    msg = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    found: list[dict] = []
    _attachments(msg.get("payload", {}), found)
    if not found:
        return None, "That email has no attachments."
    names = [p["filename"] for p in found]
    if filename:
        part = next((p for p in found if p["filename"].lower() == filename.lower()), None)
        if not part:
            return None, f"No attachment called {filename!r}. This email has: {', '.join(names)}"
        return part, None
    if len(found) == 1:
        return found[0], None
    return None, f"Which attachment? This email has: {', '.join(names)}"


def download_attachment(service, message_id: str, part: dict) -> bytes:
    blob = service.users().messages().attachments().get(
        userId="me", messageId=message_id, id=part["body"]["attachmentId"]
    ).execute()
    return base64.urlsafe_b64decode(blob["data"] + "=" * (-len(blob["data"]) % 4))


def part_mime(part: dict) -> str:
    return (part.get("mimeType") or "").split(";")[0].strip().lower()


def read_attachment(phone: str, message_id: str, filename: str | None) -> str:
    service = get_gmail_service(phone)
    if not service:
        return not_connected("Gmail", phone)
    try:
        part, err = find_attachment(service, message_id, filename)
        if err:
            return err
        mime = part_mime(part)
        if mime not in SUPPORTED_TYPES:
            return f"{part['filename']} is {mime or 'an unknown type'}; I can read PDF, Word (.docx) and plain-text attachments (it can still be saved to Drive)."
        size = part.get("body", {}).get("size") or 0
        if size > MAX_BYTES:
            return f"{part['filename']} is too large to read ({size // 1_000_000} MB; the limit is 10 MB)."
        data = download_attachment(service, message_id, part)
        text = extract_text(data, mime)
    except DocumentError as e:
        return str(e)
    except Exception as e:
        return f"Error reading attachment: {e}"

    truncated = len(text) > MAX_TEXT_CHARS
    text = text[:MAX_TEXT_CHARS].strip()
    out = f"Attachment: {part['filename']} ({mime}, {len(data):,} bytes)\n\n{text or '(no readable text)'}"
    if truncated:
        out += f"\n\n[Text truncated at {MAX_TEXT_CHARS:,} characters]"
    return out


READ_ATTACHMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "message_id": {"type": "string", "description": "The [id: …] value from read_emails."},
        "filename": {
            "type": "string",
            "description": "Which attachment, by the name read_email listed. Optional when the email has exactly one.",
        },
    },
    "required": ["message_id"],
}
