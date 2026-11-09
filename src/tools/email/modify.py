"""Inbox housekeeping: archive, read/unread, star, label, trash. Deletion is
always Gmail's trash (30-day undo); there is deliberately no permanent
delete, so nothing the model does here is irreversible."""
from src.utils.google_auth import get_gmail_modify_service, scope_not_granted

ACTIONS = ("archive", "mark_read", "mark_unread", "star", "unstar", "trash", "untrash", "label")


def _label_id(service, name: str) -> str:
    wanted = name.strip().lower()
    for label in service.users().labels().list(userId="me").execute().get("labels", []):
        if (label.get("name") or "").lower() == wanted:
            return label["id"]
    created = service.users().labels().create(
        userId="me",
        body={"name": name.strip(), "labelListVisibility": "labelShow", "messageListVisibility": "show"},
    ).execute()
    return created["id"]


def modify_email(phone: str, message_id: str, action: str, label: str | None) -> str:
    service = get_gmail_modify_service(phone)
    if not service:
        return scope_not_granted(phone, "Gmail organising (archive, labels, trash)")
    action = (action or "").strip().lower()
    if action not in ACTIONS:
        return f"Unknown action {action!r}. Use one of: {', '.join(ACTIONS)}."
    if action == "label" and not (label or "").strip():
        return "Give the label name to apply."
    try:
        meta = service.users().messages().get(
            userId="me", id=message_id, format="metadata", metadataHeaders=["Subject", "From"]
        ).execute()
        headers = {h["name"].lower(): h["value"] for h in meta.get("payload", {}).get("headers", [])}
        subject = headers.get("subject", "(no subject)")
        msgs = service.users().messages()
        if action == "trash":
            msgs.trash(userId="me", id=message_id).execute()
            done = "moved to Trash (recoverable for 30 days)"
        elif action == "untrash":
            msgs.untrash(userId="me", id=message_id).execute()
            done = "restored from Trash"
        else:
            add, remove = [], []
            if action == "archive":
                remove = ["INBOX"]
            elif action == "mark_read":
                remove = ["UNREAD"]
            elif action == "mark_unread":
                add = ["UNREAD"]
            elif action == "star":
                add = ["STARRED"]
            elif action == "unstar":
                remove = ["STARRED"]
            elif action == "label":
                add = [_label_id(service, label)]
            msgs.modify(userId="me", id=message_id, body={"addLabelIds": add, "removeLabelIds": remove}).execute()
            done = {
                "archive": "archived (removed from Inbox)", "mark_read": "marked as read",
                "mark_unread": "marked as unread", "star": "starred", "unstar": "unstarred",
                "label": f"labelled '{(label or '').strip()}'",
            }[action]
    except Exception as e:
        return f"Error updating the email: {e}"
    return f"Email '{subject}' from {headers.get('from', '?')} {done}."


MODIFY_EMAIL_SCHEMA = {
    "type": "object",
    "properties": {
        "message_id": {"type": "string", "description": "The [id: …] value from read_emails."},
        "action": {"type": "string", "enum": list(ACTIONS), "description": "What to do with the message."},
        "label": {"type": "string", "description": "Label name, only for action 'label' (created if it doesn't exist)."},
    },
    "required": ["message_id", "action"],
}
