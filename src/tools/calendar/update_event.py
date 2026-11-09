from src.tools.calendar.create_event import describe_event
from src.utils.google_auth import get_calendar_service, not_connected


def update_calendar_event(
    phone: str,
    event_id: str,
    summary: str | None,
    start_time: str | None,
    end_time: str | None,
    add_attendees: list[str] | None,
    remove_attendees: list[str] | None,
    location: str | None,
) -> str:
    service = get_calendar_service(phone)
    if not service:
        return not_connected("Google Calendar", phone)
    try:
        # Patching a rescheduled call keeps its attendees and Meet link and
        # lets Google email everyone the change, which delete-and-recreate
        # would not.
        current = service.events().get(calendarId="primary", eventId=event_id).execute()
        patch: dict = {}
        if summary:
            patch["summary"] = summary
        if start_time:
            patch["start"] = {"dateTime": start_time}
        if end_time:
            patch["end"] = {"dateTime": end_time}
        if location is not None:
            patch["location"] = location
        if add_attendees or remove_attendees:
            keep = {a["email"].lower(): a for a in current.get("attendees", []) if a.get("email")}
            for email in remove_attendees or []:
                keep.pop(email.strip().lower(), None)
            for email in add_attendees or []:
                if "@" in email:
                    keep.setdefault(email.strip().lower(), {"email": email.strip()})
            patch["attendees"] = list(keep.values())
        if not patch:
            return "Nothing to change."
        notify = bool(current.get("attendees") or patch.get("attendees"))
        event = (
            service.events()
            .patch(calendarId="primary", eventId=event_id, body=patch, sendUpdates="all" if notify else "none")
            .execute()
        )
        return describe_event(event, prefix="Event updated")
    except Exception as e:
        return f"Error updating calendar event: {e}"


UPDATE_EVENT_SCHEMA = {
    "type": "object",
    "properties": {
        "event_id": {"type": "string", "description": "The event id from get_calendar_events."},
        "summary": {"type": "string", "description": "New title."},
        "start_time": {"type": "string", "description": "New start, ISO 8601 with offset."},
        "end_time": {"type": "string", "description": "New end, ISO 8601 with offset."},
        "add_attendees": {"type": "array", "items": {"type": "string"}, "description": "Emails to invite."},
        "remove_attendees": {"type": "array", "items": {"type": "string"}, "description": "Emails to uninvite."},
        "location": {"type": "string", "description": "New location."},
    },
    "required": ["event_id"],
}
