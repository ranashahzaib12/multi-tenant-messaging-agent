from src.utils.google_auth import get_calendar_service, not_connected


def delete_calendar_event(phone: str, event_id: str) -> str:
    service = get_calendar_service(phone)
    if not service:
        return not_connected("Google Calendar", phone)

    try:
        # Invitees get the cancellation, the same way they got the invite.
        service.events().delete(calendarId="primary", eventId=event_id, sendUpdates="all").execute()
        return f"Event {event_id} deleted successfully."
    except Exception as e:
        return f"Error deleting event: {e}"


DELETE_EVENT_SCHEMA = {
    "type": "object",
    "properties": {
        "event_id": {"type": "string", "description": "The Google Calendar event ID to delete."},
    },
    "required": ["event_id"],
}
