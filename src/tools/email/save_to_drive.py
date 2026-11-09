"""Save an email attachment into the user's Google Drive: Gmail's bytes go
straight to Drive's create call; nothing is kept on our side."""
from src.tools.drive.files import upload_to_drive
from src.tools.email.read_attachment import download_attachment, find_attachment, part_mime
from src.utils.google_auth import get_gmail_service, not_connected


def save_attachment_to_drive(phone: str, message_id: str, filename: str | None, folder_name: str | None) -> str:
    service = get_gmail_service(phone)
    if not service:
        return not_connected("Gmail", phone)
    try:
        part, err = find_attachment(service, message_id, filename)
        if err:
            return err
        data = download_attachment(service, message_id, part)
    except Exception as e:
        return f"Error fetching the attachment: {e}"
    return upload_to_drive(phone, part["filename"], part_mime(part), data, folder_name)


SAVE_ATTACHMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "message_id": {"type": "string", "description": "The [id: …] value from read_emails."},
        "filename": {"type": "string", "description": "Which attachment, by name. Optional when the email has exactly one."},
        "folder_name": {"type": "string", "description": "Existing Drive folder to put it in. Omit for My Drive."},
    },
    "required": ["message_id"],
}
